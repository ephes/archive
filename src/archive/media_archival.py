from __future__ import annotations

import fcntl
import io
import json
import logging
import mimetypes
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from email.message import Message
from operator import index
from pathlib import Path
from subprocess import run as run_download
from tempfile import TemporaryDirectory, gettempdir
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request
from urllib.response import addinfourl
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import storages
from django.db import transaction
from storages.backends.s3 import S3Storage, _filter_download_params

from archive.api_responses import safe_api_error
from archive.classification import (
    _looks_like_supported_video_page_url as _classified_supported_video_page_url,
)
from archive.classification import resolve_media_sources_for_item
from archive.media_process import MediaProcessError, run_media_command
from archive.media_storage import (
    ARCHIVE_MEDIA_STORAGE_ALIAS,
    delete_unreferenced_archive_media_paths,
)
from archive.metadata import REQUEST_HEADERS
from archive.models import Item, PendingArchiveMediaUpload

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
DEFAULT_AUDIO_CONTENT_TYPE = "audio/mpeg"
DEFAULT_EXTRACTED_AUDIO_SUFFIX = ".mp3"
AMBIGUOUS_AUDIO_SUFFIXES = {".webm"}
YTDLP_AUDIO_ONLY_FORMAT = "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio[ext=mp4]/bestaudio"
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


DIRECT_MEDIA_BUDGET_EXCEEDED = "Media source exceeded the elapsed download budget"
# archive/video_page_download.py exits with this status when a downloaded source
# passed max_source_bytes (it runs isolated and cannot share the constant).
VIDEO_PAGE_SOURCE_LIMIT_EXIT_CODE = 3


class MediaArchivalError(RuntimeError):
    # Fixed-shape download diagnostic (no URL, host or upstream text), when known.
    safe_detail: str | None = None


class ArchiveMediaStorageKeyError(MediaArchivalError):
    """The storage backend saved outside the reserved upload namespace."""


class MediaSizeLimitError(MediaArchivalError):
    """A direct download stopped at its caller-supplied byte budget."""


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


def uses_supported_video_page_source(item: Item) -> bool:
    """Whether archival would download audio from a YouTube page through yt-dlp."""
    audio_source_url, video_source_url = resolve_media_sources_for_item(item)
    if audio_source_url is not None or video_source_url is None:
        return False
    return _looks_like_supported_video_page_url(video_source_url)


def archive_item_audio(item: Item, timeout: int = 300) -> ArchivedAudio:
    deadline = monotonic() + timeout
    limit = settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES
    audio_source_url = _select_audio_archive_source_url(item)
    if audio_source_url is not None:
        # One temporary file (uploaded to storage directly) and one stored copy.
        with temp_space_admission(
            (1 + _stored_copies_on_temp_volume(1)) * limit,
            purpose="Audio archival",
            timeout=_remaining_archive_time(deadline),
        ):
            return _archive_direct_audio(item=item, source_url=audio_source_url, deadline=deadline)

    video_source_url = _select_video_archive_source_url(item)
    if video_source_url is not None:
        # Two temporary files: the downloaded source and the extracted audio (a
        # YouTube page: the download and its MP3 conversion). A direct video
        # stores both; a YouTube page stores only the audio.
        stored = 1 if _looks_like_supported_video_page_url(video_source_url) else 2
        with temp_space_admission(
            (2 + _stored_copies_on_temp_volume(stored)) * limit,
            purpose="Video archival",
            timeout=_remaining_archive_time(deadline),
        ):
            return _archive_video_audio(item=item, source_url=video_source_url, deadline=deadline)

    raise MediaArchivalError("Item does not have an archivable audio source yet")


def _remaining_archive_time(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise MediaArchivalError("Media source exceeded the elapsed archival budget")
    return remaining


TEMP_ADMISSION_POLL_SECONDS = 0.5


@contextmanager
def temp_space_admission(required_bytes: int, *, purpose: str, timeout: float):
    """Hold the host's media temporary-space admission while temporary files exist.

    One exclusive lock (``ARCHIVE_MEDIA_TEMP_LOCK_PATH``, next to the database by
    default) serializes media work across all workers of this Archive, so two of
    them cannot both pass the free-space check for the same capacity, even with
    separate private temporary directories. Waiting for it ends after ``timeout``
    seconds (the caller's remaining budget).
    """
    deadline = monotonic() + timeout
    lock_path = Path(settings.ARCHIVE_MEDIA_TEMP_LOCK_PATH)
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise MediaArchivalError(f"{purpose} could not open the temporary-space lock") from exc
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() >= deadline:
                    raise MediaArchivalError(
                        f"{purpose} timed out waiting for other media work to release "
                        "temporary disk space"
                    ) from None
                sleep(min(TEMP_ADMISSION_POLL_SECONDS, max(deadline - monotonic(), 0)))
        if monotonic() >= deadline:
            # Acquired only after the budget ran out: do not start late work.
            raise MediaArchivalError(
                f"{purpose} timed out waiting for other media work to release temporary disk space"
            )
        ensure_temp_space(required_bytes, purpose=purpose)
        yield
    finally:
        # Closing releases the lock. Its outcome is separate from the media work,
        # which may already have written a reserved upload or raised a refusal.
        try:
            os.close(descriptor)
        except OSError:
            with suppress(Exception):
                logger.warning("Closing the media temporary-space lock failed")


def ensure_temp_space(required_bytes: int, *, purpose: str) -> None:
    """Refuse media work whose temporary files would leave less than the reserve free.

    Multi-gigabyte recordings pass through the temporary directory; filling the
    host's disk would also break the database and every other service there.
    """
    try:
        free = _temp_free_bytes()
    except OSError as exc:
        raise MediaArchivalError(f"{purpose} could not check temporary disk space") from exc
    needed = max(required_bytes, 0) + settings.ARCHIVE_MEDIA_TEMP_RESERVE_BYTES
    if free < needed:
        raise MediaArchivalError(
            f"{purpose} needs {needed} bytes of free temporary disk space "
            f"(including the {settings.ARCHIVE_MEDIA_TEMP_RESERVE_BYTES}-byte reserve), "
            f"but only {free} are free"
        )


def _temp_free_bytes() -> int:
    return shutil.disk_usage(gettempdir()).free


def _stored_copies_on_temp_volume(copies: int) -> int:
    """Stored copies that land on the temporary directory's volume (local storage only).

    Filesystem storage copies each temporary file into its location while the
    temporary file still exists; object storage writes elsewhere.
    """
    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    location = getattr(storage, "location", None)
    if isinstance(storage, S3Storage) or not isinstance(location, str):
        return 0
    probe = Path(location)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        same_volume = probe.stat().st_dev == Path(gettempdir()).stat().st_dev
    except OSError:
        same_volume = True
    return copies if same_volume else 0


def open_archived_audio(item: Item):
    return _open_archived_file(item.archived_audio_path, label="audio")


def open_archived_video(item: Item):
    return _open_archived_file(item.archived_video_path, label="video")


@contextmanager
def _open_direct_media(request: Request, *, timeout: float, max_bytes: int | None = None):
    # subprocess.run kills and reaps the urllib process at the elapsed deadline,
    # including DNS, redirects and reads that keep resetting socket timeouts.
    deadline = monotonic() + timeout
    with TemporaryDirectory(prefix="archive-download-") as temp_dir:
        path = Path(temp_dir) / "source"
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise MediaArchivalError(DIRECT_MEDIA_BUDGET_EXCEEDED)
        options = {
            "url": request.full_url,
            "headers": dict(request.header_items()),
            "path": str(path),
            "timeout": remaining,
            "max_bytes": (
                settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES if max_bytes is None else max_bytes
            ),
        }
        try:
            result = run_download(
                [sys.executable, "-I", str(Path(__file__).with_name("direct_media_download.py"))],
                input=json.dumps(options),
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaArchivalError(DIRECT_MEDIA_BUDGET_EXCEEDED) from exc
        if monotonic() >= deadline:
            raise MediaArchivalError(DIRECT_MEDIA_BUDGET_EXCEEDED)
        stderr_tail = result.stderr.strip().splitlines()[-1:] or ["no diagnostic output"]
        invalid_result = (
            f"Media downloader exited with code {result.returncode} without a valid result: "
            f"{stderr_tail[0][-500:]}"
        )
        try:
            metadata = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise MediaArchivalError(invalid_result) from exc
        if not isinstance(metadata, dict):
            raise MediaArchivalError(invalid_result)
        if result.returncode:
            detail = metadata.get("error")
            if not isinstance(detail, str) or not detail.strip():
                raise MediaArchivalError(invalid_result)
            if metadata.get("limit_exceeded") is True:
                raise MediaSizeLimitError(f"Media download failed: {detail}")
            error = MediaArchivalError(f"Media download failed: {detail}")
            safe_detail = metadata.get("safe_error")
            if (
                isinstance(safe_detail, str)
                and 0 < len(safe_detail) <= 200
                and safe_detail.isprintable()
            ):
                error.safe_detail = safe_detail
            raise error
        if not all(isinstance(metadata.get(key), str) for key in ("url", "content_type")):
            raise MediaArchivalError(invalid_result)
        headers = Message()
        headers["Content-Type"] = metadata["content_type"]
        with path.open("rb") as source:
            yield addinfourl(source, headers, metadata["url"])


def _archive_direct_audio(item: Item, source_url: str, deadline: float) -> ArchivedAudio:
    request = Request(
        source_url,
        headers={
            **REQUEST_HEADERS,
            "Accept": "audio/*;q=1.0,application/octet-stream;q=0.8,*/*;q=0.1",
        },
    )
    try:
        with _open_direct_media(request, timeout=_remaining_archive_time(deadline)) as response:
            final_url = response.geturl() or source_url
            content_type = (response.headers.get_content_type() or "").lower()
            suffix = _detect_audio_suffix(url=final_url, content_type=content_type)
            if not suffix:
                raise MediaArchivalError(
                    f"Unsupported media type for archival: {content_type or 'unknown'}"
                )

            normalized_content_type = content_type or _content_type_for_audio_suffix(suffix)
            # The downloader child already stopped at the byte limit; upload its
            # file as it is instead of copying it to a second temporary file.
            total_bytes = os.fstat(response.fileno()).st_size
            if total_bytes > settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES:
                raise MediaArchivalError(
                    "Audio source exceeded the configured "
                    f"{settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES}-byte archive limit"
                )
            object_name = f"items/{item.pk}/audio/source{suffix}"
            saved_name = save_reserved_media_file(
                target_name=object_name,
                upload_name=f"source{suffix}",
                file_handle=_BoundedFile(response, total_bytes),
                using=_item_database(item),
                owner=item,
            )
    except OSError as exc:
        raise MediaArchivalError(f"Audio archival download failed: {exc}") from exc

    return ArchivedAudio(
        object_name=saved_name,
        content_type=normalized_content_type,
        size_bytes=total_bytes,
    )


def _archive_video_audio(item: Item, source_url: str, deadline: float) -> ArchivedAudio:
    if _looks_like_supported_video_page_url(source_url):
        return _archive_supported_video_page_audio(
            item=item,
            source_url=source_url,
            deadline=deadline,
        )

    try:
        with TemporaryDirectory(prefix="archive-media-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            source_path, source_content_type, source_size_bytes = _download_video_source(
                source_url=source_url,
                temp_dir=temp_dir_path,
                timeout=_remaining_archive_time(deadline),
            )

            extracted_audio_path = temp_dir_path / f"extracted{DEFAULT_EXTRACTED_AUDIO_SUFFIX}"
            _extract_audio_with_ffmpeg(
                source_path=source_path,
                output_path=extracted_audio_path,
                timeout=_remaining_archive_time(deadline),
            )
            if not extracted_audio_path.exists():
                raise MediaArchivalError("Audio extraction did not produce an output file")

            audio_size_bytes = extracted_audio_path.stat().st_size
            if audio_size_bytes <= 0:
                raise MediaArchivalError("Audio extraction produced an empty output file")

            if audio_size_bytes >= settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES:
                raise MediaArchivalError("Audio extraction exceeded the configured output limit")

            source_object_name = f"items/{item.pk}/video/source{source_path.suffix}"
            audio_object_name = f"items/{item.pk}/audio/extracted{DEFAULT_EXTRACTED_AUDIO_SUFFIX}"
            saved_source_name = ""
            saved_audio_name = ""
            try:
                saved_source_name = _save_storage_path(
                    target_name=source_object_name,
                    file_path=source_path,
                    using=_item_database(item),
                    owner=item,
                )
                saved_audio_name = _save_storage_path(
                    target_name=audio_object_name,
                    file_path=extracted_audio_path,
                    using=_item_database(item),
                    owner=item,
                )
            except BaseException:
                _discard_unpublished_upload(saved_audio_name, using=_item_database(item))
                _discard_unpublished_upload(saved_source_name, using=_item_database(item))
                raise
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


def _archive_supported_video_page_audio(
    item: Item,
    source_url: str,
    deadline: float,
) -> ArchivedAudio:
    try:
        with TemporaryDirectory(prefix="archive-media-") as temp_dir:
            temp_dir_path = Path(temp_dir)
            source_path, content_type, admitted = _download_supported_video_page_audio_source(
                source_url=source_url,
                temp_dir=temp_dir_path,
                timeout=_remaining_archive_time(deadline),
            )
            object_name = f"items/{item.pk}/audio/source{source_path.suffix}"
            try:
                source_file = open(
                    source_path,
                    "rb",
                    opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK),
                )
            except OSError:
                raise MediaArchivalError("Video page download output could not be opened") from None
            with source_file:
                opened = os.fstat(source_file.fileno())
                if (
                    not _is_regular_downloaded_output(opened)
                    or not os.path.samestat(admitted, opened)
                    or opened.st_size != admitted.st_size
                ):
                    raise MediaArchivalError("Video page download output changed before storage")
                saved_name = save_reserved_media_file(
                    target_name=object_name,
                    upload_name=source_path.name,
                    file_handle=_BoundedFile(source_file, admitted.st_size),
                    using=_item_database(item),
                    owner=item,
                )
    except OSError as exc:
        raise MediaArchivalError(f"Video archival failed: {exc}") from exc

    return ArchivedAudio(
        object_name=saved_name,
        content_type=content_type,
        size_bytes=admitted.st_size,
    )


def _download_video_source(
    *,
    source_url: str,
    temp_dir: Path,
    timeout: float,
) -> tuple[Path, str, int]:
    if _looks_like_direct_video_url(source_url):
        return _download_direct_video_source(
            source_url=source_url,
            temp_dir=temp_dir,
            timeout=timeout,
        )
    raise MediaArchivalError("Item does not have a supported downloadable video source yet")


def _download_direct_video_source(
    *,
    source_url: str,
    temp_dir: Path,
    timeout: float,
) -> tuple[Path, str, int]:
    request = Request(
        source_url,
        headers={
            **REQUEST_HEADERS,
            "Accept": "video/*;q=1.0,application/octet-stream;q=0.8,*/*;q=0.1",
        },
    )
    with _open_direct_media(request, timeout=timeout) as response:
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


def _download_supported_video_page_audio_source(
    *,
    source_url: str,
    temp_dir: Path,
    timeout: float,
) -> tuple[Path, str, os.stat_result]:
    output_template = str(temp_dir / "source.%(ext)s")
    options = {
        "ffmpeg_location": _youtube_dl_ffmpeg_location(
            settings.ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN
        ),
        "format": YTDLP_AUDIO_ONLY_FORMAT,
        "max_filesize": settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES,
        "noplaylist": True,
        "nopart": True,
        # Keep the downloaded original next to its MP3 conversion, so the size
        # check after the run also sees a source that was briefly over the limit.
        "keepvideo": True,
        "no_warnings": True,
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "2",
            }
        ],
        "postprocessor_args": {
            "extractaudio+ffmpeg_o": ["-fs", str(settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES)]
        },
        # The child's stdout counts toward the diagnostic budget; as a library,
        # quiet alone still prints the progress bar, so turn it off explicitly.
        "noprogress": True,
        "quiet": True,
        "restrictfilenames": True,
        "socket_timeout": timeout,
    }
    js_runtimes = _youtube_dl_js_runtimes_option(settings.ARCHIVE_MEDIA_YTDLP_JS_RUNTIMES)
    if js_runtimes:
        options["js_runtimes"] = js_runtimes
    try:
        run_media_command(
            _video_page_download_command(),
            timeout=timeout,
            input_data=json.dumps(
                {
                    "url": source_url,
                    "options": options,
                    # Checked on disk before any yt-dlp fixup or conversion runs.
                    "max_source_bytes": settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES,
                }
            ).encode("utf-8"),
            # yt-dlp does not enforce max_filesize without a Content-Length: each
            # file (the source before conversion too) must stay within the limit,
            # and the download plus its MP3 conversion within the reserved space.
            output_dir=temp_dir,
            max_output_dir_bytes=2 * settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES,
            max_output_dir_file_bytes=settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES,
        )
    except MediaProcessError as exc:
        if str(exc) == "output limit" or exc.returncode == VIDEO_PAGE_SOURCE_LIMIT_EXIT_CODE:
            raise MediaArchivalError(
                "Video source exceeded the configured "
                f"{settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES}-byte archive limit"
            ) from None
        raise MediaArchivalError(f"Video page download {exc}") from None

    output = _find_downloaded_audio(temp_dir)
    if output is None:
        raise MediaArchivalError("Video page download did not produce a supported audio file")

    source_path, admitted = output
    size_bytes = admitted.st_size
    if size_bytes <= 0:
        raise MediaArchivalError("Video page download produced an empty audio file")
    if size_bytes >= settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES:
        raise MediaArchivalError(
            "Video source exceeded the configured "
            f"{settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES}-byte archive limit"
        )

    suffix = _detect_audio_suffix(url=str(source_path), content_type="")
    if not suffix:
        raise MediaArchivalError("Video page download produced an unsupported audio file")
    return source_path, _content_type_for_audio_suffix(suffix), admitted


def _video_page_download_command() -> list[str]:
    return [sys.executable, "-I", str(Path(__file__).with_name("video_page_download.py"))]


def _youtube_dl_js_runtimes_option(runtimes: list[str]) -> dict[str, dict[str, object]]:
    return {runtime: {} for runtime in runtimes if runtime}


def _youtube_dl_ffmpeg_location(ffmpeg_bin: str) -> str:
    resolved = shutil.which(ffmpeg_bin)
    if resolved:
        return str(Path(resolved).parent)
    return ffmpeg_bin


def _is_regular_downloaded_output(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _find_downloaded_audio(temp_dir: Path) -> tuple[Path, os.stat_result] | None:
    # yt-dlp keeps the original (keepvideo); prefer the MP3 conversion.
    converted = f"source{DEFAULT_EXTRACTED_AUDIO_SUFFIX}"
    for path in sorted(
        temp_dir.glob("source.*"), key=lambda path: (path.name != converted, path.name)
    ):
        if not _detect_audio_suffix(url=path.name, content_type=""):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if _is_regular_downloaded_output(info):
            return path, info
    return None


def _extract_audio_with_ffmpeg(*, source_path: Path, output_path: Path, timeout: float) -> None:
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
        "-fs",
        str(settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES),
        str(output_path),
    ]
    try:
        run_media_command(
            command,
            timeout=timeout,
            output_path=output_path,
            max_output_bytes=settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES,
        )
    except FileNotFoundError:
        raise MediaArchivalError(
            "Video audio extraction requires ffmpeg; configure "
            "ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN or install ffmpeg"
        ) from None
    except MediaProcessError as exc:
        if str(exc) == "output limit":
            raise MediaArchivalError(
                "Audio extraction exceeded the configured output limit"
            ) from None
        raise MediaArchivalError(f"Video audio extraction {exc}") from None


def _save_storage_path(*, target_name: str, file_path: Path, using: str, owner: Item) -> str:
    with file_path.open("rb") as file_handle:
        return save_reserved_media_file(
            target_name=target_name,
            upload_name=file_path.name,
            file_handle=file_handle,
            using=using,
            owner=owner,
        )


def save_reserved_media_file(
    *,
    target_name: str,
    upload_name: str,
    file_handle,
    using: str,
    owner: Item | None = None,
) -> str:
    """Upload into a fresh reserved namespace owned by ``owner``'s current attempt.

    The reservation records the item and its ``processing_started_at`` attempt,
    the same value the publication fence checks, so the reservation can later be
    proven retired once that attempt is no longer current.
    """
    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    target = Path(target_name)
    prefix = f"{target.parent}/{uuid4().hex}/"
    # Commit the reservation before the first storage write. Outer application
    # transactions are rejected: their rollback could erase the protection.
    with transaction.atomic(using=using, durable=True):
        PendingArchiveMediaUpload.objects.using(using).create(
            prefix=prefix,
            owner_item_id=owner.pk if owner is not None else None,
            owner_attempt=owner.processing_started_at if owner is not None else None,
        )
    fresh_name = prefix + target.name
    try:
        stored_name = storage.save(fresh_name, File(file_handle, name=upload_name))
        if not _is_reserved_child_key(stored_name, prefix=prefix):
            # Only the reserved prefix is provably ours; a key elsewhere may name
            # another upload's object, so it is left to the orphan sweep.
            logger.warning("Archive media storage returned a key outside its reservation")
            raise ArchiveMediaStorageKeyError(
                "Archive media storage key escaped its reserved upload namespace"
            )
        return stored_name
    except BaseException:
        _discard_unpublished_upload(fresh_name, using=using)
        raise


def _is_reserved_child_key(stored_name: object, *, prefix: str) -> bool:
    """Whether ``stored_name`` is a direct child of the reserved ``prefix``.

    Reservations, publication release and orphan checks all derive the protecting
    prefix from the object's parent, so a nested, relocated, untrimmed or
    traversal key would leave the stored object unprotected.
    """
    if not isinstance(stored_name, str) or not stored_name.startswith(prefix):
        return False
    child = stored_name[len(prefix) :]
    return (
        bool(child)
        and child == child.strip()
        and child not in {".", ".."}
        and not any(char in child for char in ("/", "\\", "\0"))
    )


def _item_database(item: Item) -> str:
    return item._state.db or "default"


def _discard_unpublished_upload(object_name: str, *, using: str) -> None:
    if not object_name:
        return
    try:
        delete_unreferenced_archive_media_paths((object_name,), using=using, owns_reservation=True)
    except Exception:
        # The reservation stays behind and keeps protecting the object.
        logger.exception("Failed to discard unpublished archive media %s", object_name)


def _open_archived_file(path: str, *, label: str):
    normalized_path = path.strip()
    if not normalized_path:
        raise MediaArchivalError(f"Item does not have archived {label}")
    storage = storages[ARCHIVE_MEDIA_STORAGE_ALIAS]
    try:
        opened = storage.open(normalized_path, mode="rb")
        if isinstance(storage, S3Storage):
            # django-storages would copy the whole object into memory on first
            # read; stream it with ranged GETs instead.
            try:
                return S3RangeReader(
                    opened.obj,
                    extra_args=_filter_download_params(storage.get_object_parameters(opened.name)),
                )
            finally:
                # The wrapper only owns its local buffer.
                with suppress(Exception):
                    opened.close()
        return opened
    except OSError as exc:
        raise MediaArchivalError(f"Archived {label} open failed: {exc}") from exc
    except (BotoCoreError, ClientError) as exc:
        raise MediaArchivalError(f"Archived {label} open failed: {safe_api_error(exc)}") from None


class S3RangeReader(io.RawIOBase):
    """Seekable, read-only view of an S3 object that never buffers the whole object.

    Each read continues one open ``GetObject`` stream from the current position;
    a seek elsewhere closes it and the next read starts a new ranged request.
    ``size`` is the object length from the ``HeadObject`` made when it was opened.
    Every ranged request uses that object's ETag (``IfMatch``). The response must
    echo the ETag and return the requested ``Content-Range`` before any bytes
    are read.
    """

    def __init__(self, s3_object, *, extra_args: dict[str, object] | None = None) -> None:
        super().__init__()
        self._body: Any = None
        self._position = 0
        self._object = s3_object
        self.size = int(s3_object.content_length)
        etag = s3_object.e_tag
        if not isinstance(etag, str) or not etag:
            # Without a validator a same-length replacement could be stitched in.
            raise OSError("Archived media object has no ETag")
        self._etag = etag
        self._extra_args = dict(extra_args or {})

    @property
    def object_etag(self) -> str:
        """The object ETag captured at open and used for all range requests."""
        return self._etag

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self.size + offset
        else:
            raise ValueError(f"invalid whence ({whence})")
        if position < 0:
            raise OSError("Negative seek position")
        if position != self._position:
            self._close_body()
            self._position = position
        return self._position

    def readinto(self, buffer) -> int:
        self._checkClosed()
        wanted = min(len(buffer), self.size - self._position)
        if wanted <= 0:
            return 0
        try:
            body = self._body
            if body is None:
                body = self._body = self._open_range()
            data = body.read(wanted)
            if not data:
                raise OSError("Archived media ended before its recorded length")
            count = len(data)
            if count > wanted:
                raise OSError("Archived media returned more bytes than requested")
            buffer[:count] = data
        except (BotoCoreError, ClientError) as exc:
            self._close_body()
            raise OSError(f"Archived media read failed: {safe_api_error(exc)}") from None
        except BaseException:
            self._close_body()
            raise
        self._position += count
        return count

    def close(self) -> None:
        if not self.closed:
            self._close_body()
        super().close()

    def _open_range(self):
        kwargs: dict[str, object] = {
            **self._extra_args,
            "Range": f"bytes={self._position}-",
            "IfMatch": self._etag,
        }
        response = self._object.get(**kwargs)
        body = response["Body"]
        expected = f"bytes {self._position}-{self.size - 1}/{self.size}"
        if response.get("ContentRange") != expected or response.get("ETag") != self._etag:
            try:
                body.close()
            except Exception:  # noqa: BLE001 - the mismatch is the error to report
                pass
            raise OSError("Archived media changed while it was read")
        return body

    def _close_body(self) -> None:
        body, self._body = self._body, None
        if body is not None:
            try:
                body.close()
            except Exception:  # noqa: BLE001 - closing a dropped stream must not mask the caller
                with suppress(Exception):
                    logger.warning("Closing an archived media stream failed")


class _BoundedFile(io.RawIOBase):
    """Read exactly ``size`` bytes of an open local file for a storage upload."""

    def __init__(self, file_handle, size: int) -> None:
        super().__init__()
        self._file = file_handle
        self.size = size
        self._file.seek(0)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._file.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_END:
            position = self.size + index(offset)
            if position < 0:
                raise OSError("Negative seek position")
            return self._file.seek(position, io.SEEK_SET)
        return self._file.seek(offset, whence)

    def readinto(self, buffer) -> int:
        remaining = self.size - self._file.tell()
        if remaining <= 0 or not len(buffer):
            return 0
        data = self._file.read(min(len(buffer), remaining))
        if not data:
            raise MediaArchivalError("Media upload stage ended before its admitted size")
        buffer[: len(data)] = data
        return len(data)


def _select_audio_archive_source_url(item: Item) -> str | None:
    audio_source_url, _ = resolve_media_sources_for_item(item)
    return audio_source_url


def _select_video_archive_source_url(item: Item) -> str | None:
    _, video_source_url = resolve_media_sources_for_item(item)
    return video_source_url


def _looks_like_audio_url(url: str) -> bool:
    suffix = _detect_audio_suffix(url=url, content_type="")
    return bool(suffix and suffix not in AMBIGUOUS_AUDIO_SUFFIXES)


def _looks_like_ambiguous_audio_url(url: str) -> bool:
    return _detect_audio_suffix(url=url, content_type="") in AMBIGUOUS_AUDIO_SUFFIXES


def _looks_like_direct_video_url(url: str) -> bool:
    return bool(_detect_video_suffix(url=url, content_type=""))


def _looks_like_supported_video_page_url(url: str) -> bool:
    # Archival must recognize exactly the pages classification selected, including
    # default-port and mixed-case adapter authorities.
    return _classified_supported_video_page_url(url)


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
