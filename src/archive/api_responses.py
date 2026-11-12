"""Bound reads from operator-configured upstream APIs."""

import json
from typing import Protocol
from urllib.error import HTTPError, URLError

from archive.safe_fetch import BlockedFetchError


class ReadableResponse(Protocol):
    def read(self, size: int, /) -> bytes: ...

    def close(self) -> None: ...


MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_API_ERROR_BYTES = 64 * 1024


def read_api_response(response: ReadableResponse) -> bytes:
    payload = response.read(MAX_API_RESPONSE_BYTES + 1)
    if len(payload) > MAX_API_RESPONSE_BYTES:
        raise OSError("API response exceeded the 8 MiB limit")
    return payload


class InvalidAPIJSON(ValueError):
    """An upstream response could not be decoded within interpreter limits."""


def decode_api_json(payload: bytes | str):
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        return json.loads(text)
    except (ValueError, RecursionError):
        raise InvalidAPIJSON("Invalid upstream JSON") from None


# Exact internal diagnostics only: arbitrary exception text can contain echoed
# request bodies, credentials or private URLs. Never persist an unlisted string.
_SAFE_TRANSPORT_ERRORS = frozenset(
    {
        "API response exceeded the 8 MiB limit",
        "Upstream exchange exceeded the total timeout",
        "Upstream URL must be HTTP(S) without userinfo",
        "Upstream destination must match the configured API origin",
        "Authenticated upstream redirect changed origin",
        "Upstream transport failed",
        "Upstream transport failed or response exceeded limit",
        "Article audio artifact is not MP3 audio",
        "Article audio artifact exceeded download limit",
    }
)


def safe_api_error(exc: BaseException) -> str:
    """Close HTTP failures without reading or exposing their diagnostic bodies."""
    if isinstance(exc, HTTPError):
        try:
            exc.close()
        except Exception:
            pass  # Cleanup must not replace the fixed primary diagnostic.
        code = exc.code
        return f"HTTP {code}" if isinstance(code, int) and 100 <= code <= 599 else "HTTP error"
    if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    message = str(exc)
    if message in _SAFE_TRANSPORT_ERRORS:
        return message
    if isinstance(exc, BlockedFetchError):
        # Raised only with fixed policy texts; never echoes the requested URL.
        return f"blocked destination: {message}"
    # The exception class name distinguishes local read faults from network failures
    # without copying its message, which can contain paths, hosts or upstream text.
    return f"transport error ({type(exc).__name__})"
