"""Serve a stored media file, honouring a single-range ``Range`` request.

Podcast apps and AVPlayer/Safari seek and resume audio with byte-range
requests, so enclosures answer ``Range: bytes=...`` with ``206 Partial Content``.
Anything this module does not understand is answered with the whole file,
which RFC 9110 always permits.
"""

from __future__ import annotations

import logging
import re
from typing import IO

from django.http import FileResponse, HttpRequest, HttpResponse

logger = logging.getLogger(__name__)

# At most 18 digits per number: enough for any file, and it bounds int().
_RANGE_RE = re.compile(r"bytes=([0-9]{0,18})-([0-9]{0,18})")


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
) -> HttpResponse:
    """Answer GET/HEAD for an open file of ``size`` bytes; the response owns and closes the file.

    ``size`` is the recorded length of the file. When it is unknown (0), the
    file is served whole and ranges are not offered.
    """

    byte_range = None
    # Without a validator (ETag/Last-Modified) an If-Range can never match, so
    # such a request gets the whole file (RFC 9110 section 13.1.5).
    if request.method == "GET" and size > 0 and "If-Range" not in request.headers:
        try:
            byte_range = parse_single_byte_range(request.headers.get("Range", ""), size)
        except UnsatisfiableRange:
            file_handle.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            response["Accept-Ranges"] = "bytes"
            return response

    if byte_range is None:
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
            file_handle.close()
            logger.warning("Seeking stored media for a range request failed", exc_info=True)
            return HttpResponse("Audio is temporarily unavailable.", status=502)
        length = end - start + 1
        response = FileResponse(
            _LimitedReader(file_handle, length), status=206, content_type=content_type
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = str(length)
        response["Accept-Ranges"] = "bytes"
    if request.method == "HEAD":
        # Keep GET headers, release the file without iterating its body.
        file_handle.close()
        response.streaming_content = ()
    response["Cache-Control"] = cache_control
    return response
