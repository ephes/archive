from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError
from django.core.files.storage import storages
from django.db import transaction
from django.db.models import Q

ARCHIVE_MEDIA_STORAGE_ALIAS = "archive_media"
ARCHIVE_MEDIA_PATH_FIELDS = ("archived_audio_path", "archived_video_path", "article_audio_file")


def item_archive_media_paths(item) -> tuple[str, ...]:
    return tuple(
        _normalized_unique_paths(
            str(getattr(item, field_name) or "") for field_name in ARCHIVE_MEDIA_PATH_FIELDS
        )
    )


def delete_unreferenced_archive_media_paths(
    paths: Iterable[str],
    *,
    using: str = "default",
    owns_reservation: bool = False,
) -> list[str]:
    """Delete objects that no committed item references; keep everything else.

    This is the only archive media deletion path. Each object is verified in its
    own durable transaction, so the checks never observe a caller's uncommitted
    changes and a caller's later rollback cannot revive a reference to a deleted
    object. The upload reservation is examined before the item references:
    publication removes the reservation in the same transaction that commits the
    reference, so an object seen without a reservation already has its committed
    reference visible to the second check. Callers that created the reservation
    pass ``owns_reservation`` to discard their own unpublished upload.
    """
    from archive.models import PendingArchiveMediaUpload

    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    deleted_paths: list[str] = []
    for path in _normalized_unique_paths(paths):
        with transaction.atomic(using=using, durable=True):
            reservations = (
                PendingArchiveMediaUpload.objects.using(using)
                .select_for_update()
                .filter(prefix=_reservation_prefix(path))
            )
            if not owns_reservation and reservations.exists():
                continue
            if _is_referenced_archive_media_path(path, using=using):
                continue
            storage.delete(path)
            if owns_reservation:
                reservations.delete()
        deleted_paths.append(path)
    return deleted_paths


def referenced_archive_media_paths() -> set[str]:
    return set(iter_archive_media_reference_paths())


def iter_archive_media_reference_paths() -> Iterator[str]:
    """Stream normalized references without retaining paths or whole model rows."""
    from archive.models import Item

    rows = Item.objects.order_by().values_list(*ARCHIVE_MEDIA_PATH_FIELDS).iterator(chunk_size=128)
    for row in rows:
        for raw_path in row:
            path = str(raw_path or "").strip()
            if path:
                yield path


def iter_archive_media_object_names(prefix: str = "") -> Iterator[str]:
    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    yield from _iter_storage_object_names(storage=storage, prefix=prefix.strip("/"))


def _iter_storage_object_names(*, storage, prefix: str) -> Iterator[str]:
    pending = [prefix]
    while pending:
        current = pending.pop()
        try:
            directories, files = storage.listdir(current)
        except (FileNotFoundError, NotADirectoryError):
            if current == prefix:
                return
            raise ValueError("Incomplete archive media listing: subtree unavailable") from None

        for names in (directories, files):
            for name in names:
                if (
                    not isinstance(name, str)
                    or name in ("", ".", "..")
                    or any(char in name for char in ("/", "\\", "\0"))
                ):
                    raise ValueError("Invalid archive media listing: expected a child name")
        for name in sorted(files):
            yield f"{current}/{name}" if current else name
        for name in sorted(directories, reverse=True):
            pending.append(f"{current}/{name}" if current else name)


def _normalized_unique_paths(paths: Iterable[str]) -> Iterator[str]:
    seen_paths: set[str] = set()
    for raw_path in paths:
        normalized_path = (raw_path or "").strip()
        if not normalized_path or normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        yield normalized_path


def release_pending_archive_media_paths(paths: Iterable[str], *, using: str = "default") -> None:
    """Drop upload reservations; call inside the transaction publishing the paths."""
    from archive.models import PendingArchiveMediaUpload

    prefixes = {_reservation_prefix(path) for path in _normalized_unique_paths(paths)}
    PendingArchiveMediaUpload.objects.using(using).filter(prefix__in=prefixes).delete()


def _reservation_prefix(path: str) -> str:
    return path.rsplit("/", 1)[0] + "/"


MAX_RECONCILED_PREFIX_OBJECTS = 16


class ReservationStorageDeleteError(RuntimeError):
    """A reconciliation delete failed, with the count of earlier successful deletes."""

    def __init__(self, deleted_count: int):
        super().__init__("Storage deletion failed during reservation reconciliation")
        self.deleted_count = deleted_count


@dataclass(frozen=True)
class ReservationReconciliation:
    """Outcome for one upload reservation.

    ``state`` is ``retired`` (owner attempt provably over), ``live`` (owner attempt
    still current), ``unowned`` (no recorded owner attempt; manual recovery only),
    ``oversized`` (too many objects under the prefix to reconcile automatically)
    or ``gone`` (released concurrently).
    """

    state: str
    prefix: str
    owner_item_id: int | None = None
    deleted: tuple[str, ...] = ()
    kept_referenced: tuple[str, ...] = ()
    released: bool = False


def reconcile_upload_reservation(
    reservation_id: int, *, using: str = "default", apply: bool = False
) -> ReservationReconciliation:
    """Retire one reservation whose owning processing attempt is provably over.

    The owner attempt is the item's ``processing_started_at`` captured at upload,
    the same value every media publication fence compares. Once the item's current
    attempt differs (recovered, settled, re-claimed) or the item is gone, the upload
    can never be published, whether or not its process is still running. Age alone
    never retires a reservation, and reservations without a recorded owner attempt
    are left for manual recovery. With ``apply``, a durable transaction locks the
    rows while unreferenced objects are deleted, then releases the reservation.
    A storage deletion failure raises ReservationStorageDeleteError with the
    number of completed deletes and keeps the reservation protecting what remains.
    Dry runs use advisory reads without write transactions or row locks.
    """
    from archive.models import Item, PendingArchiveMediaUpload

    snapshot = (
        PendingArchiveMediaUpload.objects.using(using)
        .filter(pk=reservation_id)
        .values("prefix", "owner_item_id", "owner_attempt")
        .first()
    )
    if snapshot is None:
        return ReservationReconciliation(state="gone", prefix="")
    prefix = snapshot["prefix"]
    owner_item_id = snapshot["owner_item_id"]
    if owner_item_id is None or snapshot["owner_attempt"] is None:
        return ReservationReconciliation(state="unowned", prefix=prefix)

    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    with transaction.atomic(using=using, durable=True) if apply else nullcontext():
        owners = Item.objects.using(using)
        reservations = PendingArchiveMediaUpload.objects.using(using)
        if apply:
            # Acquire the owner row before the reservation, like publication does.
            owners = owners.select_for_update()
            reservations = reservations.select_for_update()
        current_attempts = list(
            owners.filter(pk=owner_item_id).values_list("processing_started_at", flat=True)
        )
        reservation = reservations.filter(
            pk=reservation_id, prefix=prefix, owner_item_id=owner_item_id
        ).first()
        if reservation is None or reservation.owner_attempt is None:
            return ReservationReconciliation(state="gone", prefix=prefix)
        if current_attempts and current_attempts[0] == reservation.owner_attempt:
            return ReservationReconciliation(
                state="live", prefix=prefix, owner_item_id=owner_item_id
            )

        names = []
        for name in iter_archive_media_object_names(prefix):
            names.append(name)
            if len(names) > MAX_RECONCILED_PREFIX_OBJECTS:
                return ReservationReconciliation(
                    state="oversized", prefix=prefix, owner_item_id=owner_item_id
                )
        deleted: list[str] = []
        kept: list[str] = []
        for name in names:
            if _is_referenced_archive_media_path(name, using=using, for_update=apply):
                kept.append(name)
                continue
            if apply:
                try:
                    storage.delete(name)
                except (OSError, BotoCoreError, ClientError) as exc:
                    raise ReservationStorageDeleteError(len(deleted)) from exc
            deleted.append(name)
        if apply:
            reservation.delete()
    return ReservationReconciliation(
        state="retired",
        prefix=prefix,
        owner_item_id=owner_item_id,
        deleted=tuple(deleted),
        kept_referenced=tuple(kept),
        released=apply,
    )


def _is_referenced_archive_media_path(path: str, *, using: str, for_update: bool = True) -> bool:
    """Whether any committed item references ``path`` after whitespace trimming.

    Reads and the orphan inventory strip stored references, so a reference with
    surrounding whitespace still protects its object. Containment narrows the rows
    in the database; exact stripped equality is decided here.
    """
    from archive.models import Item

    items = Item.objects.using(using)
    if for_update:
        items = items.select_for_update()
    candidates = items.filter(
        Q(archived_audio_path__contains=path)
        | Q(archived_video_path__contains=path)
        | Q(article_audio_file__contains=path)
    ).values_list(*ARCHIVE_MEDIA_PATH_FIELDS)
    return any(str(value or "").strip() == path for row in candidates.iterator() for value in row)
