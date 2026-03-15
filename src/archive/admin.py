import json

from django.contrib import admin

from archive.classification import podcast_feed_decision_for_item
from archive.forms import ItemForm
from archive.models import Item
from archive.services import request_item_reprocess


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    form = ItemForm
    actions = ("reprocess_selected_items",)
    list_display = (
        "display_title",
        "kind",
        "classification_rule",
        "podcast_feed_policy",
        "podcast_feed_status",
        "is_public",
        "enrichment_status",
        "summary_status",
        "transcript_status",
        "media_archive_status",
        "article_audio_status",
        "source",
        "shared_at",
    )
    list_filter = (
        "kind",
        "is_public",
        "enrichment_status",
        "summary_status",
        "transcript_status",
        "media_archive_status",
        "article_audio_status",
    )
    search_fields = (
        "title",
        "original_url",
        "source",
        "author",
        "notes",
        "short_summary",
        "long_summary",
        "transcript",
        "tags",
    )
    readonly_fields = (
        "shared_at",
        "published_at",
        "classification_rule",
        "classification_evidence_pretty",
        "podcast_feed_diagnostic",
        "enrichment_error",
        "summary_error",
        "transcript_error",
        "media_archive_error",
        "article_audio_error",
        "summary_retry_count",
        "summary_retry_at",
        "media_archive_retry_count",
        "media_archive_retry_at",
    )

    @admin.display(description="Podcast feed")
    def podcast_feed_status(self, obj: Item) -> str:
        decision = podcast_feed_decision_for_item(obj)
        return "eligible" if decision.eligible else decision.reason

    @admin.display(description="Classification evidence")
    def classification_evidence_pretty(self, obj: Item) -> str:
        return json.dumps(obj.classification_evidence, indent=2, sort_keys=True)

    @admin.display(description="Podcast feed diagnostic")
    def podcast_feed_diagnostic(self, obj: Item) -> str:
        decision = podcast_feed_decision_for_item(obj)
        return f"{decision.reason} ({decision.enclosure_source or 'no enclosure'})"

    @admin.action(description="Reprocess selected items")
    def reprocess_selected_items(self, request, queryset) -> None:
        count = 0
        for item in queryset:
            request_item_reprocess(item)
            count += 1
        self.message_user(request, f"Queued {count} item(s) for reprocessing.")
