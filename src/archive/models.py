from __future__ import annotations

from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from archive.urlkeys import capture_key_for_url


class ItemKind(models.TextChoices):
    PODCAST_EPISODE = "podcast_episode", "Podcast episode"
    VIDEO = "video", "Video"
    ARTICLE = "article", "Article"
    SOCIAL_POST = "social_post", "Social post"
    QUOTE = "quote", "Quote"
    LINK = "link", "Link"


class PodcastFeedPolicy(models.TextChoices):
    AUTO = "auto", "Automatic"
    INCLUDE = "include", "Include"
    EXCLUDE = "exclude", "Exclude"


class EnrichmentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"


class Item(models.Model):
    kind = models.CharField(
        max_length=32,
        choices=ItemKind.choices,
        default=ItemKind.LINK,
    )
    classification_engine_version = models.PositiveSmallIntegerField(default=1)
    classification_rule = models.CharField(max_length=64, blank=True)
    classification_evidence = models.JSONField(default=dict, blank=True)
    podcast_feed_policy = models.CharField(
        max_length=16,
        choices=PodcastFeedPolicy.choices,
        default=PodcastFeedPolicy.AUTO,
    )
    enrichment_status = models.CharField(
        max_length=16,
        choices=EnrichmentStatus.choices,
        default=EnrichmentStatus.PENDING,
    )
    summary_status = models.CharField(
        max_length=16,
        choices=EnrichmentStatus.choices,
        default=EnrichmentStatus.PENDING,
    )
    transcript_status = models.CharField(
        max_length=16,
        choices=EnrichmentStatus.choices,
        default=EnrichmentStatus.PENDING,
    )
    media_archive_status = models.CharField(
        max_length=16,
        choices=EnrichmentStatus.choices,
        default=EnrichmentStatus.COMPLETE,
    )
    article_audio_status = models.CharField(
        max_length=16,
        choices=EnrichmentStatus.choices,
        default=EnrichmentStatus.COMPLETE,
    )
    is_public = models.BooleanField(default=True)
    original_url = models.URLField()
    # Nullable so SQLite can ADD COLUMN instead of rebuilding archive_item (see constraints).
    url_key = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        db_index=True,
        editable=False,
        help_text="SHA-256 of the normalised URL, used to recognise repeated captures.",
    )
    capture_key = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        editable=False,
        help_text="Equal to url_key on the newest item for a URL; NULL on older copies.",
    )
    title = models.CharField(max_length=500, blank=True)
    shared_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(blank=True, null=True)
    short_summary = models.TextField(blank=True)
    long_summary = models.TextField(blank=True)
    transcript = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    tags = models.TextField(blank=True)
    audio_url = models.URLField(blank=True)
    media_url = models.URLField(blank=True)
    source = models.CharField(max_length=255, blank=True)
    author = models.CharField(max_length=255, blank=True)
    original_published_at = models.DateTimeField(blank=True, null=True)
    enrichment_error = models.TextField(blank=True)
    summary_error = models.TextField(blank=True)
    transcript_error = models.TextField(blank=True)
    media_archive_error = models.TextField(blank=True)
    article_audio_error = models.TextField(blank=True)
    processing_started_at = models.DateTimeField(blank=True, null=True, db_index=True)
    summary_retry_count = models.PositiveSmallIntegerField(default=0)
    summary_retry_at = models.DateTimeField(blank=True, null=True)
    media_archive_retry_count = models.PositiveSmallIntegerField(default=0)
    media_archive_retry_at = models.DateTimeField(blank=True, null=True)
    short_summary_generated = models.BooleanField(default=False)
    long_summary_generated = models.BooleanField(default=False)
    tags_generated = models.BooleanField(default=False)
    transcript_generated = models.BooleanField(default=False)
    archived_audio_path = models.CharField(max_length=500, blank=True)
    archived_audio_content_type = models.CharField(max_length=100, blank=True)
    archived_audio_size_bytes = models.PositiveBigIntegerField(default=0)
    archived_video_path = models.CharField(max_length=500, blank=True)
    archived_video_content_type = models.CharField(max_length=100, blank=True)
    archived_video_size_bytes = models.PositiveBigIntegerField(default=0)
    article_audio_generated = models.BooleanField(default=False)
    article_audio_job_id = models.CharField(max_length=64, blank=True)
    article_audio_artifact_path = models.CharField(max_length=500, blank=True)
    article_audio_poll_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-shared_at", "-id")
        constraints = [
            # Partial unique indexes: SQLite can add them with CREATE UNIQUE INDEX instead of
            # rebuilding archive_item, which would drop the full-text search triggers.
            models.UniqueConstraint(
                fields=("capture_key",),
                condition=models.Q(capture_key__isnull=False),
                name="archive_item_unique_capture_key",
            ),
        ]

    def __str__(self) -> str:
        return self.display_title

    @property
    def display_title(self) -> str:
        return self.title or self.original_url

    @property
    def feed_description(self) -> str:
        if self.short_summary.strip():
            return self.short_summary.strip()
        if self.notes.strip():
            return self.notes.strip()
        if self.source.strip():
            return f"Archived from {self.source.strip()}."
        return f"Archived {self.get_kind_display().lower()}: {self.original_url}"

    @property
    def feed_published_at(self):
        return self.published_at or self.shared_at

    @property
    def has_required_feed_metadata(self) -> bool:
        return bool(self.title.strip())

    def get_absolute_url(self) -> str:
        return reverse("archive:item-detail", kwargs={"pk": self.pk})

    @property
    def tag_list(self) -> list[str]:
        raw_tags = self.tags.replace(",", "\n").splitlines()
        return [tag.strip() for tag in raw_tags if tag.strip()]

    @property
    def has_transcript(self) -> bool:
        return bool(self.transcript.strip())

    @property
    def has_generated_article_audio(self) -> bool:
        return bool(self.article_audio_artifact_path.strip())

    @property
    def has_archived_audio(self) -> bool:
        return bool(self.archived_audio_path.strip())

    @property
    def has_archived_video(self) -> bool:
        return bool(self.archived_video_path.strip())

    @property
    def archived_audio_url(self) -> str:
        if not self.has_archived_audio:
            return ""
        return reverse("archive:item-archived-audio", kwargs={"pk": self.pk})

    @property
    def has_stable_audio_enclosure(self) -> bool:
        return self.has_archived_audio

    @property
    def stable_audio_enclosure_url(self) -> str:
        return self.archived_audio_url

    @property
    def stable_audio_content_type(self) -> str:
        if not self.has_stable_audio_enclosure:
            return ""
        return self.archived_audio_content_type or "audio/mpeg"

    @property
    def stable_audio_size_bytes(self) -> int:
        if not self.has_stable_audio_enclosure:
            return 0
        return self.archived_audio_size_bytes

    @property
    def playback_audio_url(self) -> str:
        if self.has_stable_audio_enclosure:
            return self.stable_audio_enclosure_url
        if self.has_generated_article_audio:
            return reverse("archive:item-article-audio", kwargs={"pk": self.pk})
        return self.audio_url

    @property
    def has_playable_audio(self) -> bool:
        return bool(self.playback_audio_url)

    @property
    def has_required_podcast_feed_metadata(self) -> bool:
        return bool(
            self.title.strip() and self.short_summary.strip() and self.has_stable_audio_enclosure
        )

    def save(self, *args, **kwargs) -> None:
        if self.is_public and self.published_at is None:
            self.published_at = self.shared_at

        if not getattr(self, "_capture_managed", False):
            # capture_key is owned by archive.capture: ordinary saves must not write a stale
            # in-memory value over it. New items get it from _save_and_sync_capture_holder.
            if self._state.adding:
                self.capture_key = None
            elif kwargs.get("update_fields") is None:
                deferred = self.get_deferred_fields()
                kwargs["update_fields"] = [
                    field.name
                    for field in self._meta.concrete_fields
                    if not field.primary_key
                    and field.attname not in deferred
                    and field.name != "capture_key"
                ]
            else:
                kwargs["update_fields"] = [
                    name for name in kwargs["update_fields"] if name != "capture_key"
                ]

        update_fields = kwargs.get("update_fields")
        target_processing_started_at = self._processing_started_at_value()
        if self.processing_started_at != target_processing_started_at:
            self.processing_started_at = target_processing_started_at
            if update_fields is not None:
                kwargs["update_fields"] = sorted(
                    {*(update_fields or ()), "processing_started_at"}
                )
                update_fields = kwargs["update_fields"]
        self.url_key = capture_key_for_url(self.original_url)
        if update_fields is not None and "original_url" in update_fields:
            kwargs["update_fields"] = sorted({*update_fields, "url_key"})
            update_fields = kwargs["update_fields"]
        writes_url = update_fields is None or "url_key" in update_fields
        if getattr(self, "_capture_managed", False) or not writes_url:
            super().save(*args, **kwargs)
            return
        self._save_and_sync_capture_holder(*args, **kwargs)

    def _save_and_sync_capture_holder(self, *args, **kwargs) -> None:
        """Persist a URL write and move capture holders in the same transaction.

        The previous fingerprint is read from the database (not from instance state), so
        partial saves, deferred fields and stale instances cannot skip the hand-over.
        """
        from archive.capture import sync_capture_holder, take_capture_write_lock

        with transaction.atomic():
            take_capture_write_lock()
            previous = None
            if not self._state.adding and self.pk is not None:
                previous = (
                    Item.objects.filter(pk=self.pk).values_list("url_key", flat=True).first()
                )
            super().save(*args, **kwargs)
            if previous and previous != self.url_key:
                sync_capture_holder(previous)
            sync_capture_holder(self.url_key)
            self.capture_key = (
                Item.objects.filter(pk=self.pk).values_list("capture_key", flat=True).first()
            )

    def _processing_started_at_value(self):
        if self._has_processing_status():
            return self.processing_started_at or timezone.now()
        return None

    def _has_processing_status(self) -> bool:
        return any(
            status == EnrichmentStatus.PROCESSING
            for status in (
                self.enrichment_status,
                self.summary_status,
                self.transcript_status,
                self.media_archive_status,
                self.article_audio_status,
            )
        )


class CaptureIdempotencyKey(models.Model):
    """A client ``Idempotency-Key`` and the item its first capture request resolved to."""

    key = models.CharField(max_length=255, unique=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="idempotency_keys")
    url_key = models.CharField(
        max_length=64,
        help_text="url_key of the URL sent with the first request using this key.",
    )
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.key
