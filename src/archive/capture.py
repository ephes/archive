"""Shared API/form capture boundary for the application's SQLite database."""

from datetime import timedelta

from django.conf import settings
from django.db import router, transaction
from django.utils import timezone

from archive.capture_config import capture_dedupe_seconds
from archive.capture_identity import capture_url_digest, normalize_capture_url
from archive.models import Item
from archive.services import prepare_item_for_enrichment


def capture_item(item: Item) -> tuple[Item, bool]:
    """Return (saved item, created), leaving repeated captures entirely untouched.

    BEGIN IMMEDIATE acquires SQLite's writer lock before the lookup, serializing
    concurrent captures. The digest/time index restricts comparisons to matching
    recent identities; full normalized equality protects against digest collisions.
    """
    if item.pk is not None:
        raise ValueError("capture_item requires an unsaved item")
    seconds = capture_dedupe_seconds(settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS)
    using = item._state.db or router.db_for_write(Item, instance=item)
    with transaction.atomic(using=using):
        if seconds > 0:
            now = timezone.now()
            cutoff = now - timedelta(seconds=seconds)
            key = normalize_capture_url(item.original_url)
            candidates = (
                Item.objects.using(using)
                .filter(
                    capture_url_digest=capture_url_digest(item.original_url),
                    shared_at__range=(cutoff, now),
                )
                .order_by("-shared_at", "-pk")
                .values_list("pk", "original_url")
            )
            for pk, url in candidates.iterator():
                if normalize_capture_url(url) == key:
                    return Item.objects.using(using).get(pk=pk), False
        prepare_item_for_enrichment(item)
        item.save(using=using)
        return item, True
