"""Capture deduplication.

Share sheets and HTTP clients retry, and some Shortcuts repeat the shared URL. Each new item
queues paid enrichment (summary, transcription, article audio), so repeated captures of the
same URL within ``ARCHIVE_CAPTURE_DEDUPE_SECONDS`` return the existing item instead.

Every item stores ``url_key``, the hash of its normalised URL. The newest item per ``url_key``
also holds ``capture_key`` (a partial unique index), and client ``Idempotency-Key`` values live
in ``CaptureIdempotencyKey`` (unique). The SQLite connection uses ``BEGIN IMMEDIATE``
(settings), so each capture transaction holds the write lock before it looks anything up and
concurrent captures run one after another; the unique indexes plus the retry loop keep other
databases from creating a second capture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from archive.models import CaptureIdempotencyKey, Item
from archive.urlkeys import capture_key_for_url, normalize_capture_url

__all__ = [
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "CaptureResult",
    "IdempotencyKeyConflict",
    "capture_dedupe_window",
    "capture_item",
    "capture_key_for_url",
    "find_recent_capture",
    "normalize_capture_url",
    "sync_capture_holder",
]

IDEMPOTENCY_KEY_MAX_LENGTH = 255
_CAPTURE_ATTEMPTS = 5
_RETRY_BACKOFF_SECONDS = 0.05


class IdempotencyKeyConflict(Exception):
    """The idempotency key was already used for a different URL."""


@dataclass(frozen=True)
class CaptureResult:
    item: Item
    duplicate: bool


def capture_dedupe_window() -> timedelta | None:
    seconds = int(getattr(settings, "ARCHIVE_CAPTURE_DEDUPE_SECONDS", 0) or 0)
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def _is_recent(item: Item) -> bool:
    window = capture_dedupe_window()
    return window is not None and item.shared_at >= timezone.now() - window


def find_recent_capture(url: str) -> Item | None:
    """Return the item a capture of ``url`` would be deduplicated against, if any."""
    holder = Item.objects.filter(capture_key=capture_key_for_url(url)).first()
    if holder is not None and _is_recent(holder):
        return holder
    return None


class _CaptureRetry(Exception):
    pass


def capture_item(
    item: Item,
    *,
    idempotency_key: str = "",
    allow_duplicate: bool = False,
) -> CaptureResult:
    """Save a newly captured ``item`` unless the same capture already exists.

    Returns the existing item with ``duplicate=True`` when ``idempotency_key`` was seen before
    or the normalised URL was captured within the dedupe window. ``allow_duplicate`` saves a
    new item regardless of the window (an owner saving on purpose); the new item then becomes
    the one later captures are compared against.
    """
    if item.pk is not None:
        raise ValueError("capture_item only saves new items")
    key = capture_key_for_url(item.original_url)
    last_error: Exception | None = None
    for attempt in range(_CAPTURE_ATTEMPTS):
        if last_error is not None:
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
        try:
            with transaction.atomic():
                return _capture_once(
                    item,
                    key=key,
                    idempotency_key=idempotency_key,
                    allow_duplicate=allow_duplicate,
                )
        except (IntegrityError, OperationalError, _CaptureRetry) as exc:
            # A concurrent capture won a unique index, or SQLite gave up waiting for the
            # write lock. Back off and retry so the lookup sees the winner.
            item.pk = None
            item._state.adding = True
            item.capture_key = None
            last_error = exc
    assert last_error is not None
    if isinstance(last_error, _CaptureRetry):
        raise RuntimeError("Capture did not settle after concurrent updates") from last_error
    raise last_error


def _capture_once(
    item: Item,
    *,
    key: str,
    idempotency_key: str,
    allow_duplicate: bool,
) -> CaptureResult:
    if idempotency_key:
        seen = (
            CaptureIdempotencyKey.objects.select_related("item").filter(key=idempotency_key).first()
        )
        if seen is not None:
            # Compare with the URL of the original keyed request, not the item's current
            # (editable) URL.
            if seen.url_key != key:
                raise IdempotencyKeyConflict(idempotency_key)
            return CaptureResult(item=seen.item, duplicate=True)

    holder = Item.objects.filter(capture_key=key).first()
    if holder is not None:
        if not allow_duplicate and _is_recent(holder):
            _remember_idempotency_key(idempotency_key, item=holder, key=key)
            return CaptureResult(item=holder, duplicate=True)
        released = Item.objects.filter(pk=holder.pk, capture_key=key).update(capture_key=None)
        if not released:
            raise _CaptureRetry

    item.capture_key = key
    item._capture_managed = True
    try:
        item.save()
    finally:
        item._capture_managed = False
    _remember_idempotency_key(idempotency_key, item=item, key=key)
    return CaptureResult(item=item, duplicate=False)


def _remember_idempotency_key(idempotency_key: str, *, item: Item, key: str) -> None:
    if idempotency_key:
        CaptureIdempotencyKey.objects.create(key=idempotency_key, item=item, url_key=key)


def sync_capture_holder(url_key: str) -> None:
    """Make the newest item with ``url_key`` the one later captures are compared against.

    Runs after an item's URL is edited, after an item is created outside ``capture_item`` and
    after an item is deleted, so the capture key follows the newest remaining copy.
    """
    if not url_key:
        return
    with transaction.atomic():
        newest = (
            Item.objects.filter(url_key=url_key)
            .order_by("-shared_at", "-id")
            .values_list("pk", flat=True)
            .first()
        )
        stale = Item.objects.filter(capture_key=url_key)
        if newest is not None:
            stale = stale.exclude(pk=newest)
        stale.update(capture_key=None)
        if newest is not None:
            Item.objects.filter(pk=newest).exclude(capture_key=url_key).update(capture_key=url_key)
