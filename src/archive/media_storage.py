from __future__ import annotations

from collections.abc import Iterable, Iterator

from django.core.files.storage import storages
from django.db import transaction
from django.db.models import Q

ARCHIVE_MEDIA_STORAGE_ALIAS = "archive_media"
ARCHIVE_MEDIA_PATH_FIELDS = ("archived_audio_path", "archived_video_path")


def item_archive_media_paths(item) -> tuple[str, ...]:
    return tuple(
        _normalized_unique_paths(
            getattr(item, field_name) for field_name in ARCHIVE_MEDIA_PATH_FIELDS
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
    from archive.models import Item, PendingArchiveMediaUpload

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
            if (
                Item.objects.using(using)
                .select_for_update()
                .filter(Q(archived_audio_path=path) | Q(archived_video_path=path))
                .exists()
            ):
                continue
            storage.delete(path)
            if owns_reservation:
                reservations.delete()
        deleted_paths.append(path)
    return deleted_paths


def referenced_archive_media_paths() -> set[str]:
    from archive.models import Item

    referenced_paths: set[str] = set()
    for field_name in ARCHIVE_MEDIA_PATH_FIELDS:
        referenced_paths.update(
            _normalized_unique_paths(Item.objects.values_list(field_name, flat=True).iterator())
        )
    return referenced_paths


def iter_archive_media_object_names(prefix: str = "") -> Iterator[str]:
    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    yield from _iter_storage_object_names(storage=storage, prefix=prefix.strip("/"))


def _iter_storage_object_names(*, storage, prefix: str) -> Iterator[str]:
    try:
        directories, files = storage.listdir(prefix)
    except (FileNotFoundError, NotADirectoryError):
        return

    normalized_prefix = prefix.strip("/")
    for file_name in files:
        object_name = "/".join(part for part in (normalized_prefix, file_name) if part)
        if object_name:
            yield object_name

    for directory_name in directories:
        child_prefix = "/".join(part for part in (normalized_prefix, directory_name) if part)
        yield from _iter_storage_object_names(storage=storage, prefix=child_prefix)


def _normalized_unique_paths(paths: Iterable[str]) -> Iterator[str]:
    seen_paths: set[str] = set()
    for raw_path in paths:
        normalized_path = raw_path.strip()
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
