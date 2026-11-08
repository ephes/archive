from __future__ import annotations

from collections.abc import Iterable, Iterator

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
                .filter(
                    Q(archived_audio_path=path)
                    | Q(archived_video_path=path)
                    | Q(article_audio_file=path)
                )
                .exists()
            ):
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
