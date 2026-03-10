from django.contrib import admin

from archive.models import Item


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = (
        "display_title",
        "kind",
        "is_public",
        "enrichment_status",
        "shared_at",
    )
    list_filter = ("kind", "is_public", "enrichment_status")
    search_fields = ("title", "original_url", "source", "notes", "short_summary")
    readonly_fields = ("shared_at", "published_at")
