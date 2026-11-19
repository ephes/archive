"""Serve a stored media file, honouring a single-range ``Range`` request.

Podcast apps and AVPlayer/Safari seek and resume audio with byte-range
requests, so enclosures answer ``Range: bytes=...`` with ``206 Partial Content``.
Anything this module does not understand is answered with the whole file,
which RFC 9110 always permits.
"""

from __future__ import annotations

import logging
import os
import re
from typing import IO

from django.db.models.fields.files import FieldFile
from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseNotModified
from django.utils.http import parse_etags
from storages.backends.s3 import S3File

from archive.media_archival import S3RangeReader

logger = logging.getLogger(__name__)

# At most 18 digits per number: enough for any file, and it bounds int().
_RANGE_RE = re.compile(r"bytes=([0-9]{0,18})-([0-9]{0,18})")
_LAZY_STORAGE_READERS = (S3File, S3RangeReader)


class UnsatisfiableRange(Exception):
    pass


def parse_single_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """Return the inclusive ``(start, end)`` of a single byte range in a file of ``size`` bytes.

    Returns ``None`` when the header is not exactly one well-formed byte range
    (several ranges, another unit, whitespace, oversized numbers, end before
    start); the caller then serves the whole file. Raises ``UnsatisfiableRange``
    when the range is well-formed but selects no byte of the file.
    """

    match = _RANGE_RE.fullmatch(header)
    if match is None:
        return None
    raw_start, raw_end = match.groups()
    if not raw_start:
        if not raw_end:
            return None
        suffix_length = int(raw_end)
        if suffix_length == 0 or size == 0:
            raise UnsatisfiableRange
        return max(size - suffix_length, 0), size - 1
    start = int(raw_start)
    if raw_end and int(raw_end) < start:
        return None
    if start >= size:
        raise UnsatisfiableRange
    end = min(int(raw_end), size - 1) if raw_end else size - 1
    return start, end


class _LimitedReader:
    """File-like view of the next ``length`` bytes of a file; closing it closes the file."""

    def __init__(self, file_handle: IO[bytes], length: int) -> None:
        self._file = file_handle
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        if size is None or size < 0 or size > self._remaining:
            size = self._remaining
        chunk = self._file.read(size)
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self._file.close()


def ranged_file_response(
    request: HttpRequest,
    *,
    file_handle: IO[bytes],
    size: int,
    content_type: str,
    cache_control: str,
    etag: str | None = None,
) -> HttpResponse:
    """Answer GET/HEAD for an open file of ``size`` bytes; the response owns and closes the file.

    ``size`` is the recorded length of the file. When it is unknown (0), the
    file is served whole and ranges are not offered. When the opened object
    reports its own length (see ``opened_object_length``) and it disagrees with
    a known recorded size, no length, range or body is committed: the request
    gets a non-cacheable 502 and the handle is closed.

    ``etag`` is a strong validator for the current stored generation. A matching
    ``If-None-Match`` (weak comparison) answers 304 and closes the handle; an
    ``If-Range`` is honoured only when it equals ``etag`` exactly.
    """

    def finalize(response: HttpResponse) -> HttpResponse:
        response["Cache-Control"] = "no-store" if response.status_code >= 500 else cache_control
        return response

    transferred = False
    try:
        opened = _underlying_file(file_handle)
        lazy_head = request.method == "HEAD" and isinstance(opened, _LAZY_STORAGE_READERS)
        available = opened_object_length(opened) if size > 0 or lazy_head else None
        if size > 0 and available is not None and available != size:
            logger.warning("stored_media_length_mismatch")
            return finalize(HttpResponse("Audio is temporarily unavailable.", status=502))
        if etag is not None and _if_none_match(request.headers.get("If-None-Match"), etag):
            # Conditionals precede Range (RFC 9110 section 13.2.2); the handle closes below.
            not_modified = HttpResponseNotModified()
            not_modified["ETag"] = etag
            return finalize(not_modified)
        byte_range = None
        # An If-Range other than the current strong validator never matches, so
        # such a request gets the whole file (RFC 9110 section 13.1.5).
        if_range = request.headers.get("If-Range")
        range_allowed = if_range is None or (etag is not None and if_range == etag)
        if request.method == "GET" and size > 0 and range_allowed:
            try:
                byte_range = parse_single_byte_range(request.headers.get("Range", ""), size)
            except UnsatisfiableRange:
                response = HttpResponse(status=416)
                response["Content-Range"] = f"bytes */{size}"
                response["Accept-Ranges"] = "bytes"
                if etag is not None:
                    response["ETag"] = etag
                return finalize(response)

        if byte_range is None:
            if lazy_head:
                # FileResponse's header inference can seek and load a lazy file.
                # Give it an empty reader that still owns the opened handle.
                response = FileResponse(
                    _LimitedReader(file_handle, 0),
                    content_type=content_type,
                    filename=getattr(file_handle, "name", ""),
                )
                if available is not None:
                    response["Content-Length"] = str(available)
            else:
                response = FileResponse(file_handle, content_type=content_type)
            if size > 0:
                response["Content-Length"] = str(size)
                response["Accept-Ranges"] = "bytes"
        else:
            start, end = byte_range
            try:
                file_handle.seek(start)
            except Exception:
                # Storage backends raise their own error types; any failure before
                # the headers are sent is reported instead of a broken stream.
                # Messages/tracebacks can contain signed URLs or backend credentials.
                logger.warning("stored_media_range_seek_failed")
                return finalize(HttpResponse("Audio is temporarily unavailable.", status=502))
            length = end - start + 1
            response = FileResponse(
                _LimitedReader(file_handle, length), status=206, content_type=content_type
            )
            response["Content-Range"] = f"bytes {start}-{end}/{size}"
            response["Content-Length"] = str(length)
            response["Accept-Ranges"] = "bytes"
        if etag is not None:
            response["ETag"] = etag
        if request.method == "HEAD":
            # Keep GET headers without iterating the body. The response already
            # owns the file and closes it exactly once when the server closes it.
            response.streaming_content = ()
        finalize(response)
        transferred = True
        return response
    finally:
        if not transferred:
            try:
                file_handle.close()
            except Exception as exc:
                # Cleanup must not replace the response setup/storage failure.
                logger.warning(
                    "Closing stored media during response preparation failed (%s)",
                    type(exc).__name__,
                )


def _underlying_file(file_handle: object) -> object:
    return file_handle.file if isinstance(file_handle, FieldFile) else file_handle


def opened_object_length(file_handle: object) -> int | None:
    """Use opened-object metadata for lazy S3 readers and fstat for local files.

    Other readers may provide an integer size when there is no descriptor.
    """
    file_handle = _underlying_file(file_handle)
    if not isinstance(file_handle, _LAZY_STORAGE_READERS):
        try:
            descriptor = file_handle.fileno()  # type: ignore[attr-defined]
        except Exception:
            descriptor = None
        if isinstance(descriptor, int) and not isinstance(descriptor, bool) and descriptor >= 0:
            try:
                return os.fstat(descriptor).st_size
            except OSError:
                return None
    try:
        reported = file_handle.size  # type: ignore[attr-defined]
    except Exception:
        return None
    if isinstance(reported, bool) or not isinstance(reported, int) or reported < 0:
        return None
    return reported


def _if_none_match(header: str | None, etag: str) -> bool:
    """Weak comparison of ``If-None-Match`` against the current validator."""
    if not header:
        return False
    candidates = parse_etags(header)
    if candidates == ["*"]:
        return True
    current = etag.removeprefix("W/")
    return any(candidate.removeprefix("W/") == current for candidate in candidates)
