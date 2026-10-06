"""Serve stored media files with single-range ``Range`` support.

Podcast apps and AVPlayer/Safari seek and resume audio with byte-range requests,
so enclosures must answer ``Range: bytes=...`` with ``206 Partial Content``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import IO

from django.http import FileResponse, HttpRequest, HttpResponse, StreamingHttpResponse

try:
    from storages.backends.s3 import S3File
except ImportError:  # pragma: no cover - django-storages is an optional backend.
    S3File = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)
STREAM_CHUNK_BYTES = 64 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_MAX_RANGE_DIGITS = 18
_MAX_RANGE_NUMBER = 10**_MAX_RANGE_DIGITS


class UnsatisfiableRange(Exception):
    pass


def parse_single_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """Return an inclusive ``(start, end)`` for a single byte range.

    Returns ``None`` when the header is absent, malformed or asks for several
    ranges; callers then serve the whole file, which RFC 9110 permits. Raises
    ``UnsatisfiableRange`` when the range is well-formed but cannot be served.
    """

    match = _RANGE_RE.match(header.strip().replace(" ", ""))
    if match is None:
        return None
    raw_start, raw_end = match.groups()
    if not raw_start and not raw_end:
        return None
    if not raw_start:
        suffix_length = _bounded_int(raw_end)
        if suffix_length == 0 or size == 0:
            raise UnsatisfiableRange
        return max(size - suffix_length, 0), size - 1
    start = _bounded_int(raw_start)
    end = _bounded_int(raw_end) if raw_end else size - 1
    if raw_end and end < start:
        return None
    if start >= size:
        raise UnsatisfiableRange
    return start, min(end, size - 1)


def _bounded_int(digits: str) -> int:
    # Clients control these numerals; never hand int() an unbounded digit string.
    # Any value past _MAX_RANGE_NUMBER exceeds every file this app can serve.
    significant = digits.lstrip("0") or "0"
    if len(significant) > _MAX_RANGE_DIGITS:
        return _MAX_RANGE_NUMBER
    return int(significant)


def ranged_file_response(
    request: HttpRequest,
    *,
    file_handle: IO[bytes],
    size: int,
    content_type: str,
    cache_control: str,
) -> HttpResponse:
    if size <= 0:
        size = _file_size(file_handle)
    if size <= 0:
        # Without a trustworthy length, ranges cannot be computed; serve as-is.
        response: HttpResponse = FileResponse(file_handle, content_type=content_type)
        response["Cache-Control"] = cache_control
        return response

    byte_range = None
    range_header = request.headers.get("Range", "")
    # Without validators (ETag/Last-Modified) an If-Range never matches, so the
    # full representation must be sent (RFC 9110 section 13.1.5).
    # Range only applies to GET (RFC 9110 section 14.2); HEAD describes the full file.
    if request.method == "GET" and range_header and not request.headers.get("If-Range"):
        try:
            byte_range = parse_single_byte_range(range_header, size)
        except UnsatisfiableRange:
            file_handle.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            response["Accept-Ranges"] = "bytes"
            response["Cache-Control"] = cache_control
            return response

    start, end = byte_range if byte_range is not None else (0, size - 1)
    length = end - start + 1
    if request.method == "HEAD":
        file_handle.close()
        response = HttpResponse(content_type=content_type)
    else:
        s3_object = _unloaded_s3_object(file_handle)
        if s3_object is not None:
            # Open the ranged GET before sending headers so backend failures
            # (missing object, outage) become a 502 instead of a broken stream.
            try:
                body = s3_object.get(Range=f"bytes={start}-{end}")["Body"]
            except Exception:
                file_handle.close()
                logger.warning("Ranged S3 read failed", exc_info=True)
                return HttpResponse("Audio is temporarily unavailable.", status=502)
            content: Iterator[bytes] = _iter_s3_body(file_handle, body, length=length)
        else:
            content = _iter_file_range(file_handle, start=start, length=length)
        response = StreamingHttpResponse(content, content_type=content_type)
    if byte_range is not None:
        response.status_code = 206
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = cache_control
    return response


def _iter_file_range(file_handle: IO[bytes], *, start: int, length: int) -> Iterator[bytes]:
    try:
        if start:
            file_handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = file_handle.read(min(STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        file_handle.close()


def _unloaded_s3_object(file_handle: IO[bytes]):
    """Return the boto3 object behind an S3File that has not been downloaded yet.

    Reading or seeking an S3File first downloads the whole object into a spool,
    so ranges are instead fetched directly with a ranged GetObject.
    """

    if S3File is None or not isinstance(file_handle, S3File):
        return None
    if getattr(file_handle, "_file", None) is not None:
        return None
    return file_handle.obj


def _iter_s3_body(file_handle, body, *, length: int) -> Iterator[bytes]:
    try:
        remaining = length
        for chunk in body.iter_chunks(STREAM_CHUNK_BYTES):
            if not chunk:
                continue
            chunk = chunk[:remaining]
            remaining -= len(chunk)
            yield chunk
            if remaining <= 0:
                break
    finally:
        body.close()
        file_handle.close()


def _file_size(file_handle: IO[bytes]) -> int:
    size = getattr(file_handle, "size", None)
    if isinstance(size, int):
        return size
    try:
        current = file_handle.tell()
        file_handle.seek(0, 2)
        size = file_handle.tell()
        file_handle.seek(current)
    except (AttributeError, OSError, ValueError):
        return 0
    return size
