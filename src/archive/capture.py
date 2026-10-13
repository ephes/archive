"""Shared API/form capture boundary for the application's SQLite database."""

from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from archive.models import Item
from archive.services import prepare_item_for_enrichment


def normalize_capture_url(url: str) -> str:
    """Compare URLs without changing paths, credentials, or any query bytes."""
    without_fragment = url.split("#", 1)[0]
    try:
        parts = urlsplit(without_fragment)
        userinfo, separator, host_port = parts.netloc.rpartition("@")
        if not separator:
            host_port = parts.netloc
        host_port = host_port.lower()
        if parts.port == {"http": 80, "https": 443}.get(parts.scheme):
            if parts.port is not None:
                host_port = host_port.rsplit(":", 1)[0]
        authority = (userinfo + separator if separator else "") + host_port
        # Keep even an empty query delimiter; urlunsplit would discard it.
        prefix_length = len(parts.scheme) + 3 + len(parts.netloc)
        return parts.scheme.lower() + "://" + authority + without_fragment[prefix_length:]
    except ValueError:
        # Legacy/operator-written malformed URLs must not prevent other captures.
        return without_fragment


def capture_item(item: Item) -> tuple[Item, bool]:
    """Return (saved item, created), leaving repeated captures entirely untouched.

    BEGIN IMMEDIATE acquires SQLite's writer lock before the lookup, serializing
    concurrent captures. The existing shared_at index bounds the comparison to
    recent URLs; no schema change or historical backfill is needed.
    """
    if item.pk is not None:
        raise ValueError("capture_item requires an unsaved item")
    with transaction.atomic():
        seconds = settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS
        if seconds > 0:
            cutoff = timezone.now() - timedelta(seconds=seconds)
            key = normalize_capture_url(item.original_url)
            candidates = (
                Item.objects.filter(shared_at__gte=cutoff)
                .order_by("-shared_at", "-pk")
                .values_list("pk", "original_url")
            )
            for pk, url in candidates.iterator():
                if normalize_capture_url(url) == key:
                    return Item.objects.get(pk=pk), False
        prepare_item_for_enrichment(item)
        item.save()
        return item, True
