from django.contrib import admin

from archive.forms import ItemForm
from archive.models import Item


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    form = ItemForm
    list_display = (
        "display_title",
        "kind",
        "is_public",
        "enrichment_status",
        "summary_status",
        "transcript_status",
        "source",
        "shared_at",
    )
    list_filter = (
        "kind",
        "is_public",
        "enrichment_status",
        "summary_status",
        "transcript_status",
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
        "enrichment_error",
        "summary_error",
        "transcript_error",
        "summary_retry_count",
        "summary_retry_at",
    )
