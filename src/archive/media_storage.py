from __future__ import annotations

from collections.abc import Iterable, Iterator

from django.core.files.storage import storages
from django.db.models import Q

ARCHIVE_MEDIA_STORAGE_ALIAS = "archive_media"
ARCHIVE_MEDIA_PATH_FIELDS = ("archived_audio_path", "archived_video_path")


def item_archive_media_paths(item) -> tuple[str, ...]:
    return tuple(
        _normalized_unique_paths(
            getattr(item, field_name) for field_name in ARCHIVE_MEDIA_PATH_FIELDS
        )
    )


def delete_archive_media_paths(paths: Iterable[str]) -> list[str]:
    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    deleted_paths: list[str] = []
    for path in _normalized_unique_paths(paths):
        storage.delete(path)
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


def archive_media_path_is_referenced(path: str, *, using: str = "default") -> bool:
    from archive.models import Item

    normalized_path = path.strip()
    if not normalized_path:
        return False
    return Item.objects.using(using).filter(
        Q(archived_audio_path=normalized_path) | Q(archived_video_path=normalized_path)
    ).exists()


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
