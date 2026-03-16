import json

from django.contrib import admin

from archive.classification import (
    CURRENT_CLASSIFICATION_ENGINE_VERSION,
    classification_is_stale,
    podcast_feed_decision_for_item,
    selected_media_from_evidence,
)
from archive.forms import ItemForm
from archive.models import Item
from archive.services import describe_item_downstream_normalization, request_item_reprocess


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    form = ItemForm
    actions = ("reprocess_selected_items",)
    list_display = (
        "display_title",
        "kind",
        "classification_rule",
        "classification_engine_status",
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
        "classification_rule",
        "podcast_feed_policy",
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
        "classification_engine_status",
        "classification_rule",
        "selected_media_diagnostic",
        "classification_evidence_pretty",
        "podcast_feed_diagnostic",
        "downstream_state_diagnostic",
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

    @admin.display(description="Classification engine")
    def classification_engine_status(self, obj: Item) -> str:
        freshness = "stale" if classification_is_stale(obj) else "current"
        return (
            f"{freshness} "
            f"(stored v{obj.classification_engine_version}, "
            f"current v{CURRENT_CLASSIFICATION_ENGINE_VERSION})"
        )

    @admin.display(description="Selected media")
    def selected_media_diagnostic(self, obj: Item) -> str:
        selected_media = selected_media_from_evidence(obj.classification_evidence)
        audio = selected_media["audio"] or "none"
        video = selected_media["video"] or "none"
        return f"audio={audio}; video={video}"

    @admin.display(description="Classification evidence")
    def classification_evidence_pretty(self, obj: Item) -> str:
        return json.dumps(obj.classification_evidence, indent=2, sort_keys=True)

    @admin.display(description="Podcast feed diagnostic")
    def podcast_feed_diagnostic(self, obj: Item) -> str:
        decision = podcast_feed_decision_for_item(obj)
        return f"{decision.reason} ({decision.enclosure_source or 'no enclosure'})"

    @admin.display(description="Downstream state")
    def downstream_state_diagnostic(self, obj: Item) -> str:
        changes = describe_item_downstream_normalization(obj)
        return "clean" if not changes else "\n".join(changes)

    @admin.action(description="Reprocess selected items")
    def reprocess_selected_items(self, request, queryset) -> None:
        count = 0
        for item in queryset:
            request_item_reprocess(item)
            count += 1
        self.message_user(request, f"Queued {count} item(s) for reprocessing.")
