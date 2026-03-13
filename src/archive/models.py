from __future__ import annotations

from django.db import models
from django.urls import reverse
from django.utils import timezone


class ItemKind(models.TextChoices):
    PODCAST_EPISODE = "podcast_episode", "Podcast episode"
    VIDEO = "video", "Video"
    ARTICLE = "article", "Article"
    SOCIAL_POST = "social_post", "Social post"
    LINK = "link", "Link"


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
    article_audio_status = models.CharField(
        max_length=16,
        choices=EnrichmentStatus.choices,
        default=EnrichmentStatus.COMPLETE,
    )
    is_public = models.BooleanField(default=True)
    original_url = models.URLField()
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
    article_audio_error = models.TextField(blank=True)
    summary_retry_count = models.PositiveSmallIntegerField(default=0)
    summary_retry_at = models.DateTimeField(blank=True, null=True)
    short_summary_generated = models.BooleanField(default=False)
    long_summary_generated = models.BooleanField(default=False)
    tags_generated = models.BooleanField(default=False)
    transcript_generated = models.BooleanField(default=False)
    article_audio_generated = models.BooleanField(default=False)
    article_audio_job_id = models.CharField(max_length=64, blank=True)
    article_audio_artifact_path = models.CharField(max_length=500, blank=True)
    article_audio_poll_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-shared_at", "-id")

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
    def playback_audio_url(self) -> str:
        if self.has_generated_article_audio:
            return reverse("archive:item-article-audio", kwargs={"pk": self.pk})
        return self.audio_url

    @property
    def has_playable_audio(self) -> bool:
        return bool(self.playback_audio_url)

    def save(self, *args, **kwargs) -> None:
        if self.is_public and self.published_at is None:
            self.published_at = self.shared_at
        super().save(*args, **kwargs)
