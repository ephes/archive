from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import storages

from archive.metadata import REQUEST_HEADERS
from archive.models import Item, ItemKind

CHUNK_SIZE = 1024 * 1024
DEFAULT_AUDIO_CONTENT_TYPE = "audio/mpeg"
AUDIO_CONTENT_TYPE_SUFFIXES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mpga": ".mpga",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
}


class MediaArchivalError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchivedAudio:
    object_name: str
    content_type: str
    size_bytes: int


def can_archive_audio(item: Item) -> bool:
    return _select_audio_archive_source_url(item) is not None


def archive_item_audio(item: Item, timeout: int = 300) -> ArchivedAudio:
    source_url = _select_audio_archive_source_url(item)
    if source_url is None:
        raise MediaArchivalError("Item does not have an archivable audio source yet")

    request = Request(
        source_url,
        headers={
            **REQUEST_HEADERS,
            "Accept": "audio/*;q=1.0,application/octet-stream;q=0.8,*/*;q=0.1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            final_url = response.geturl() or source_url
            content_type = (response.headers.get_content_type() or "").lower()
            suffix = _detect_audio_suffix(url=final_url, content_type=content_type)
            if not suffix:
                raise MediaArchivalError(
                    f"Unsupported media type for archival: {content_type or 'unknown'}"
                )

            normalized_content_type = content_type or _content_type_for_suffix(suffix)
            total_bytes = 0
            spool = SpooledTemporaryFile(max_size=5 * CHUNK_SIZE)
            try:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES:
                        raise MediaArchivalError(
                            "Audio source exceeded the configured "
                            f"{settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES}-byte archive limit"
                        )
                    spool.write(chunk)

                spool.seek(0)
                object_name = f"items/{item.pk}/audio/source{suffix}"
                storage = storages["archive_media"]
                if item.archived_audio_path and item.archived_audio_path != object_name:
                    storage.delete(item.archived_audio_path)
                if storage.exists(object_name):
                    storage.delete(object_name)

                saved_name = storage.save(object_name, File(spool, name=f"source{suffix}"))
            finally:
                spool.close()
    except HTTPError as exc:
        raise MediaArchivalError(f"Audio archival download failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise MediaArchivalError(f"Audio archival download failed: {exc.reason}") from exc
    except OSError as exc:
        raise MediaArchivalError(f"Audio archival download failed: {exc}") from exc

    return ArchivedAudio(
        object_name=saved_name,
        content_type=normalized_content_type,
        size_bytes=total_bytes,
    )


def open_archived_audio(item: Item):
    if not item.archived_audio_path.strip():
        raise MediaArchivalError("Item does not have archived audio")
    try:
        return storages["archive_media"].open(item.archived_audio_path, mode="rb")
    except OSError as exc:
        raise MediaArchivalError(f"Archived audio open failed: {exc}") from exc


def _select_audio_archive_source_url(item: Item) -> str | None:
    candidates = [
        item.audio_url.strip(),
        item.media_url.strip(),
        item.original_url.strip(),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _looks_like_audio_url(candidate):
            return candidate

    if item.kind == ItemKind.PODCAST_EPISODE and item.audio_url.strip():
        return item.audio_url.strip()
    return None


def _looks_like_audio_url(url: str) -> bool:
    return bool(_detect_audio_suffix(url=url, content_type=""))


def _detect_audio_suffix(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for suffix in AUDIO_CONTENT_TYPE_SUFFIXES.values():
        if path.endswith(suffix):
            return suffix

    if content_type in AUDIO_CONTENT_TYPE_SUFFIXES:
        return AUDIO_CONTENT_TYPE_SUFFIXES[content_type]

    guessed_suffix = mimetypes.guess_extension(content_type or "", strict=False) or ""
    if guessed_suffix in AUDIO_CONTENT_TYPE_SUFFIXES.values():
        return guessed_suffix
    return ""


def _content_type_for_suffix(suffix: str) -> str:
    for content_type, known_suffix in AUDIO_CONTENT_TYPE_SUFFIXES.items():
        if known_suffix == suffix:
            return content_type
    return DEFAULT_AUDIO_CONTENT_TYPE
