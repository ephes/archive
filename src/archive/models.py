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
    is_public = models.BooleanField(default=True)
    original_url = models.URLField()
    title = models.CharField(max_length=500, blank=True)
    shared_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(blank=True, null=True)
    short_summary = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    tags = models.TextField(blank=True)
    audio_url = models.URLField(blank=True)
    source = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-shared_at", "-id")

    def __str__(self) -> str:
        return self.display_title

    @property
    def display_title(self) -> str:
        return self.title or self.original_url

    def get_absolute_url(self) -> str:
        return reverse("archive:item-detail", kwargs={"pk": self.pk})

    def save(self, *args, **kwargs) -> None:
        if self.is_public and self.published_at is None:
            self.published_at = self.shared_at
        super().save(*args, **kwargs)
