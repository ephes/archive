from django.forms import ModelForm, Textarea

from archive.media_archival import can_archive_audio
from archive.models import EnrichmentStatus, Item, ItemKind


class ItemForm(ModelForm):
    class Meta:
        model = Item
        fields = (
            "original_url",
            "title",
            "short_summary",
            "long_summary",
            "transcript",
            "tags",
            "notes",
            "kind",
            "source",
            "audio_url",
            "media_url",
            "podcast_feed_policy",
            "is_public",
        )
        widgets = {
            "short_summary": Textarea(attrs={"rows": 3}),
            "long_summary": Textarea(attrs={"rows": 6}),
            "transcript": Textarea(attrs={"rows": 16}),
            "tags": Textarea(attrs={"rows": 3}),
            "notes": Textarea(attrs={"rows": 5}),
        }

    def save(self, commit: bool = True) -> Item:
        item = super().save(commit=False)
        is_new = item.pk is None

        if "short_summary" in self.changed_data:
            item.short_summary_generated = False
        if "long_summary" in self.changed_data:
            item.long_summary_generated = False
        if "tags" in self.changed_data:
            item.tags_generated = False
        if "transcript" in self.changed_data:
            item.transcript_generated = False
            item.transcript_status = EnrichmentStatus.COMPLETE
            item.transcript_error = ""
        if "kind" in self.changed_data:
            item.classification_rule = "operator_override"
            item.classification_evidence = {
                **item.classification_evidence,
                "operator_override": {
                    "kind": item.kind,
                },
            }
        if is_new or "kind" in self.changed_data or "audio_url" in self.changed_data:
            _normalize_media_archive_status(item)
            _normalize_article_audio_status(item)

        if commit:
            item.save()
            self.save_m2m()
        return item


def _normalize_article_audio_status(item: Item) -> None:
    if item.has_generated_article_audio:
        item.article_audio_status = EnrichmentStatus.COMPLETE
        item.article_audio_error = ""
        item.article_audio_poll_at = None
        return

    if item.kind == ItemKind.ARTICLE:
        item.article_audio_status = EnrichmentStatus.PENDING
        item.article_audio_error = ""
        item.article_audio_poll_at = None
        return

    item.article_audio_status = EnrichmentStatus.COMPLETE
    item.article_audio_error = ""
    item.article_audio_poll_at = None


def _normalize_media_archive_status(item: Item) -> None:
    if item.has_archived_audio:
        item.media_archive_status = EnrichmentStatus.COMPLETE
        item.media_archive_error = ""
        item.media_archive_retry_count = 0
        item.media_archive_retry_at = None
        return

    if can_archive_audio(item):
        item.media_archive_status = EnrichmentStatus.PENDING
        item.media_archive_error = ""
        item.media_archive_retry_count = 0
        item.media_archive_retry_at = None
        return

    item.media_archive_status = EnrichmentStatus.COMPLETE
    item.media_archive_error = ""
    item.media_archive_retry_count = 0
    item.media_archive_retry_at = None
