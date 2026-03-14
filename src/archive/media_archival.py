from __future__ import annotations

import mimetypes
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import storages

from archive.metadata import REQUEST_HEADERS
from archive.models import Item, ItemKind

try:
    import yt_dlp
except ImportError:  # pragma: no cover - exercised via runtime error handling.
    yt_dlp = None

CHUNK_SIZE = 1024 * 1024
DEFAULT_AUDIO_CONTENT_TYPE = "audio/mpeg"
DEFAULT_EXTRACTED_AUDIO_SUFFIX = ".mp3"
AMBIGUOUS_AUDIO_SUFFIXES = {".webm"}
YOUTUBE_PAGE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
YTDLP_PROGRESSIVE_VIDEO_FORMAT = "best[ext=mp4]/best[ext=webm]/best"
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
VIDEO_CONTENT_TYPE_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-m4v": ".m4v",
}


class MediaArchivalError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchivedAudio:
    object_name: str
    content_type: str
    size_bytes: int
    source_object_name: str = ""
    source_content_type: str = ""
    source_size_bytes: int = 0


def can_archive_audio(item: Item) -> bool:
    return (
        _select_audio_archive_source_url(item) is not None
        or _select_video_archive_source_url(item) is not None
    )


def archive_item_audio(item: Item, timeout: int = 300) -> ArchivedAudio:
    audio_source_url = _select_audio_archive_source_url(item)
    if audio_source_url is not None:
        return _archive_direct_audio(item=item, source_url=audio_source_url, timeout=timeout)

    video_source_url = _select_video_archive_source_url(item)
    if video_source_url is not None:
        return _archive_video_audio(item=item, source_url=video_source_url, timeout=timeout)

    raise MediaArchivalError("Item does not have an archivable audio source yet")


def open_archived_audio(item: Item):
    if not item.archived_audio_path.strip():
        raise MediaArchivalError("Item does not have archived audio")
    try:
        return storages["archive_media"].open(item.archived_audio_path, mode="rb")
    except OSError as exc:
        raise MediaArchivalError(f"Archived audio open failed: {exc}") from exc


def _archive_direct_audio(item: Item, source_url: str, timeout: int) -> ArchivedAudio:
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

            normalized_content_type = content_type or _content_type_for_audio_suffix(suffix)
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
                saved_name = _save_storage_file(
                    target_name=object_name,
                    upload_name=f"source{suffix}",
                    file_handle=spool,
                    existing_name=item.archived_audio_path,
                )
                _delete_stored_object(item.archived_video_path)
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


def _archive_video_audio(item: Item, source_url: str, timeout: int) -> ArchivedAudio:
    try:
        with TemporaryDirectory(prefix="archive-media-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            source_path, source_content_type, source_size_bytes = _download_video_source(
                source_url=source_url,
                temp_dir=temp_dir_path,
                timeout=timeout,
            )

            extracted_audio_path = temp_dir_path / f"extracted{DEFAULT_EXTRACTED_AUDIO_SUFFIX}"
            _extract_audio_with_ffmpeg(
                source_path=source_path,
                output_path=extracted_audio_path,
                timeout=timeout,
            )
            if not extracted_audio_path.exists():
                raise MediaArchivalError("Audio extraction did not produce an output file")

            audio_size_bytes = extracted_audio_path.stat().st_size
            if audio_size_bytes <= 0:
                raise MediaArchivalError("Audio extraction produced an empty output file")

            source_object_name = f"items/{item.pk}/video/source{source_path.suffix}"
            audio_object_name = f"items/{item.pk}/audio/extracted{DEFAULT_EXTRACTED_AUDIO_SUFFIX}"
            saved_source_name = ""
            saved_audio_name = ""
            try:
                saved_source_name = _save_storage_path(
                    target_name=source_object_name,
                    file_path=source_path,
                    existing_name=item.archived_video_path,
                )
                saved_audio_name = _save_storage_path(
                    target_name=audio_object_name,
                    file_path=extracted_audio_path,
                    existing_name=item.archived_audio_path,
                )
            except Exception:
                _delete_stored_object(saved_audio_name)
                _delete_stored_object(saved_source_name)
                raise
    except HTTPError as exc:
        raise MediaArchivalError(f"Video archival download failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise MediaArchivalError(f"Video archival download failed: {exc.reason}") from exc
    except OSError as exc:
        raise MediaArchivalError(f"Video archival failed: {exc}") from exc

    return ArchivedAudio(
        object_name=saved_audio_name,
        content_type=DEFAULT_AUDIO_CONTENT_TYPE,
        size_bytes=audio_size_bytes,
        source_object_name=saved_source_name,
        source_content_type=source_content_type,
        source_size_bytes=source_size_bytes,
    )


def _download_video_source(
    *,
    source_url: str,
    temp_dir: Path,
    timeout: int,
) -> tuple[Path, str, int]:
    if _looks_like_direct_video_url(source_url):
        return _download_direct_video_source(
            source_url=source_url,
            temp_dir=temp_dir,
            timeout=timeout,
        )
    if _looks_like_supported_video_page_url(source_url):
        return _download_supported_video_page_source(
            source_url=source_url,
            temp_dir=temp_dir,
            timeout=timeout,
        )
    raise MediaArchivalError("Item does not have a supported downloadable video source yet")


def _download_direct_video_source(
    *,
    source_url: str,
    temp_dir: Path,
    timeout: int,
) -> tuple[Path, str, int]:
    request = Request(
        source_url,
        headers={
            **REQUEST_HEADERS,
            "Accept": "video/*;q=1.0,application/octet-stream;q=0.8,*/*;q=0.1",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        final_url = response.geturl() or source_url
        content_type = (response.headers.get_content_type() or "").lower()
        suffix = _detect_video_suffix(url=final_url, content_type=content_type)
        if not suffix:
            raise MediaArchivalError(
                f"Unsupported video media type for archival: {content_type or 'unknown'}"
            )

        normalized_content_type = content_type or _content_type_for_video_suffix(suffix)
        total_bytes = 0
        source_path = temp_dir / f"source{suffix}"
        with source_path.open("wb") as source_file:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES:
                    raise MediaArchivalError(
                        "Video source exceeded the configured "
                        f"{settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES}-byte archive limit"
                    )
                source_file.write(chunk)

    return source_path, normalized_content_type, total_bytes


def _download_supported_video_page_source(
    *,
    source_url: str,
    temp_dir: Path,
    timeout: int,
) -> tuple[Path, str, int]:
    if yt_dlp is None:
        raise MediaArchivalError(
            "Video page download requires yt-dlp; run uv sync or deploy the updated app"
        )

    output_template = str(temp_dir / "source.%(ext)s")
    options = {
        "format": YTDLP_PROGRESSIVE_VIDEO_FORMAT,
        "max_filesize": settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES,
        "noplaylist": True,
        "nopart": True,
        "no_warnings": True,
        "outtmpl": output_template,
        "quiet": True,
        "restrictfilenames": True,
        "socket_timeout": timeout,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.extract_info(source_url, download=True)
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise MediaArchivalError(f"Video page download failed: {detail}") from exc

    source_path = _find_downloaded_video_path(temp_dir)
    if source_path is None:
        raise MediaArchivalError("Video page download did not produce a supported media file")

    size_bytes = source_path.stat().st_size
    if size_bytes <= 0:
        raise MediaArchivalError("Video page download produced an empty media file")
    if size_bytes > settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES:
        raise MediaArchivalError(
            "Video source exceeded the configured "
            f"{settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES}-byte archive limit"
        )

    suffix = _detect_video_suffix(url=str(source_path), content_type="")
    if not suffix:
        raise MediaArchivalError("Video page download produced an unsupported media file")
    return source_path, _content_type_for_video_suffix(suffix), size_bytes


def _find_downloaded_video_path(temp_dir: Path) -> Path | None:
    candidates = sorted(
        path
        for path in temp_dir.iterdir()
        if path.is_file()
        and path.name.startswith("source.")
        and _looks_like_direct_video_url(path.name)
    )
    return candidates[0] if candidates else None


def _extract_audio_with_ffmpeg(*, source_path: Path, output_path: Path, timeout: int) -> None:
    command = [
        settings.ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN,
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MediaArchivalError(
            "Video audio extraction requires ffmpeg; configure "
            "ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN or install ffmpeg"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaArchivalError("Video audio extraction timed out") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        detail = stderr.splitlines()[-1] if stderr else str(exc)
        raise MediaArchivalError(f"Video audio extraction failed: {detail}") from exc


def _save_storage_path(*, target_name: str, file_path: Path, existing_name: str) -> str:
    with file_path.open("rb") as file_handle:
        return _save_storage_file(
            target_name=target_name,
            upload_name=file_path.name,
            file_handle=file_handle,
            existing_name=existing_name,
        )


def _save_storage_file(
    *,
    target_name: str,
    upload_name: str,
    file_handle,
    existing_name: str,
) -> str:
    storage = storages["archive_media"]
    if existing_name and existing_name != target_name:
        storage.delete(existing_name)
    if storage.exists(target_name):
        storage.delete(target_name)
    return storage.save(target_name, File(file_handle, name=upload_name))


def _delete_stored_object(object_name: str) -> None:
    if object_name.strip():
        storages["archive_media"].delete(object_name)


def _select_audio_archive_source_url(item: Item) -> str | None:
    explicit_audio_url = item.audio_url.strip()
    if explicit_audio_url:
        if _looks_like_audio_url(explicit_audio_url) or _looks_like_ambiguous_audio_url(
            explicit_audio_url
        ):
            return explicit_audio_url

    candidates = [item.media_url.strip(), item.original_url.strip()]
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


def _select_video_archive_source_url(item: Item) -> str | None:
    candidates = [
        item.media_url.strip(),
        item.original_url.strip(),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _looks_like_direct_video_url(candidate) or _looks_like_supported_video_page_url(
            candidate
        ):
            return candidate
    return None


def _looks_like_audio_url(url: str) -> bool:
    suffix = _detect_audio_suffix(url=url, content_type="")
    return bool(suffix and suffix not in AMBIGUOUS_AUDIO_SUFFIXES)


def _looks_like_ambiguous_audio_url(url: str) -> bool:
    return _detect_audio_suffix(url=url, content_type="") in AMBIGUOUS_AUDIO_SUFFIXES


def _looks_like_direct_video_url(url: str) -> bool:
    return bool(_detect_video_suffix(url=url, content_type=""))


def _looks_like_supported_video_page_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_PAGE_HOSTS:
        return False
    if _looks_like_direct_video_url(url):
        return False
    if host == "youtu.be":
        return bool(parsed.path.strip("/"))
    if parsed.path == "/watch":
        return bool(parse_qs(parsed.query).get("v"))
    return parsed.path.startswith(("/embed/", "/live/", "/shorts/"))


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


def _detect_video_suffix(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for suffix in VIDEO_CONTENT_TYPE_SUFFIXES.values():
        if path.endswith(suffix):
            return suffix

    if content_type in VIDEO_CONTENT_TYPE_SUFFIXES:
        return VIDEO_CONTENT_TYPE_SUFFIXES[content_type]

    guessed_suffix = mimetypes.guess_extension(content_type or "", strict=False) or ""
    if guessed_suffix in VIDEO_CONTENT_TYPE_SUFFIXES.values():
        return guessed_suffix
    return ""


def _content_type_for_audio_suffix(suffix: str) -> str:
    for content_type, known_suffix in AUDIO_CONTENT_TYPE_SUFFIXES.items():
        if known_suffix == suffix:
            return content_type
    return DEFAULT_AUDIO_CONTENT_TYPE


def _content_type_for_video_suffix(suffix: str) -> str:
    for content_type, known_suffix in VIDEO_CONTENT_TYPE_SUFFIXES.items():
        if known_suffix == suffix:
            return content_type
    return "video/mp4"
