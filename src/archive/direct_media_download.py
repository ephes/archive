"""Bounded urllib download, isolated so its parent can stop every blocking call."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

_ERROR_TEXT_LIMIT = 500


def supports_metadata_content_type(content_type: str) -> bool:
    """Admit normalized HTML and XML media types, including structured XML."""
    # A missing Content-Type defaults to text/plain in HTTPMessage and is refused.
    return content_type in {"text/html", "text/xml", "application/xml"} or bool(
        re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\+xml", content_type)
    )


class _LimitExceeded(ValueError):
    """The declared or observed body size exceeds the caller's byte budget."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"Media source exceeded the configured {max_bytes}-byte archive limit")


def _load_safe_fetch():
    # Isolated mode keeps this script's directory off sys.path, so load the
    # stdlib-only guard module by file path.
    spec = importlib.util.spec_from_file_location(
        "archive_safe_fetch", Path(__file__).with_name("safe_fetch.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load the fetch guard")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_error(exc: BaseException, blocked: type[BaseException] | None) -> str:
    """Fixed-shape diagnostic that never copies URLs, hosts or upstream text."""
    if isinstance(exc, HTTPError):
        code = exc.code
        return f"HTTP {code}" if isinstance(code, int) and 100 <= code <= 599 else "HTTP error"
    if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if blocked is not None and isinstance(exc, blocked):
        # The fetch guard raises only fixed policy texts.
        return f"blocked destination: {exc}"
    return f"transport error ({type(exc).__name__})"


def _full_media_length(response) -> int | None:
    if response.getcode() != 200 or "Content-Range" in response.headers:
        raise ValueError("Media source did not return a complete HTTP 200 representation")
    lengths = response.headers.get_all("Content-Length", [])
    if not lengths:
        return None
    raw = lengths[0].strip()
    if len(lengths) != 1 or not raw.isascii() or not raw.isdecimal():
        raise ValueError("Media source has an invalid Content-Length")
    try:
        return int(raw)
    except ValueError:
        raise ValueError("Media source has an invalid Content-Length") from None


def main() -> int:
    blocked: type[BaseException] | None = None
    exit_code = 1
    info: dict[str, str | int | bool]
    try:
        options = json.load(sys.stdin)
        request = Request(options["url"], headers=options["headers"])
        safe_fetch = _load_safe_fetch()
        blocked = getattr(safe_fetch, "BlockedFetchError", None)
        with safe_fetch.open_public_url(request, timeout=options["timeout"]) as response:
            metadata_mode = options.get("metadata", False)
            expected_size = None if metadata_mode else _full_media_length(response)
            max_bytes = options["max_bytes"]
            if expected_size is not None and expected_size > max_bytes:
                raise _LimitExceeded(max_bytes)
            content_type = response.headers.get_content_type()
            if metadata_mode and not supports_metadata_content_type(content_type):
                raise ValueError(f"Unsupported content type: {content_type}")
            size = 0
            with Path(options["path"]).open("wb") as output:
                while not metadata_mode or size < max_bytes:
                    read_size = min(8192, max_bytes - size) if metadata_mode else 1024 * 1024
                    chunk = response.read(read_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise _LimitExceeded(max_bytes)
                    output.write(chunk)
            if not metadata_mode:
                if not size:
                    raise ValueError("Media source is empty")
                if expected_size is not None and size != expected_size:
                    raise ValueError("Media source download was incomplete")
            info = {
                "url": response.geturl(),
                "content_type": content_type,
                "charset": response.headers.get_content_charset() or "utf-8",
            }
    except HTTPError as exc:
        info = {
            "error": str(exc)[:_ERROR_TEXT_LIMIT],
            "http_status": exc.code,
            "safe_error": _safe_error(exc, blocked),
        }
    except URLError as exc:
        info = {
            "error": str(exc)[:_ERROR_TEXT_LIMIT],
            "url_reason": str(exc.reason)[:_ERROR_TEXT_LIMIT],
            "safe_error": _safe_error(exc, blocked),
        }
    except _LimitExceeded as exc:
        info = {"error": str(exc), "limit_exceeded": True}
    except Exception as exc:
        info = {"error": str(exc)[:_ERROR_TEXT_LIMIT], "safe_error": _safe_error(exc, blocked)}
    else:
        exit_code = 0

    print(json.dumps(info))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
