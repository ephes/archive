from django.contrib import admin

from archive.models import Item


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = (
        "display_title",
        "kind",
        "is_public",
        "enrichment_status",
        "summary_status",
        "source",
        "shared_at",
    )
    list_filter = ("kind", "is_public", "enrichment_status", "summary_status")
    search_fields = (
        "title",
        "original_url",
        "source",
        "author",
        "notes",
        "short_summary",
        "long_summary",
        "tags",
    )
    readonly_fields = (
        "shared_at",
        "published_at",
        "enrichment_error",
        "summary_error",
        "summary_retry_count",
        "summary_retry_at",
    )
