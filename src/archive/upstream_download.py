"""Disposable authenticated transport; invoked only by upstream_http."""

import importlib.util
import json
import sys
from contextlib import ExitStack
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

_RECEIPT_HEADER_LIMIT = 4096


class UpstreamResponseError(OSError):
    pass


def _load_transport():
    spec = importlib.util.spec_from_file_location(
        "archive_upstream_http", Path(__file__).with_name("upstream_http.py")
    )
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load upstream transport")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _content_length(response, representation: str) -> int | None:
    lengths = response.headers.get_all("Content-Length", [])
    if not lengths:
        return None
    raw = lengths[0].strip()
    if len(lengths) != 1 or not raw.isascii() or not raw.isdecimal():
        raise UpstreamResponseError(f"{representation} has an invalid Content-Length")
    try:
        return int(raw)
    except ValueError:
        raise UpstreamResponseError(f"{representation} has an invalid Content-Length") from None


def _receipt_headers(headers: Message) -> list[tuple[str, str]]:
    selected = []
    for name in ("Content-Type", "Content-Length"):
        value = headers.get(name)
        if value is not None:
            selected.append((name, value))
    # Bound input to the JSON encoder as well as its escaped output.
    if (
        sum(len(value) for _, value in selected) > _RECEIPT_HEADER_LIMIT
        or len(json.dumps(selected)) > _RECEIPT_HEADER_LIMIT
    ):
        raise UpstreamResponseError("Upstream response metadata exceeded receipt limit")
    return selected


def main() -> int:
    with ExitStack() as stack:
        return _exchange(stack)


def _exchange(stack) -> int:
    try:
        options = json.load(sys.stdin)
        request = Request(
            options["url"],
            headers=options["headers"],
            method=options["method"],
            data=(
                stack.enter_context(Path(options["data_path"]).open("rb"))
                if options["data_path"] is not None
                else None
            ),
        )
        is_error = False
        try:
            response = _load_transport()._open_authenticated_url(
                request, timeout=options["timeout"]
            )
        except HTTPError as exc:
            response = exc
            is_error = True
        with response:
            status = response.code
            limit = 64 * 1024 if is_error else options["max_bytes"]
            audio_mode = options.get("audio")
            representation = "Article audio artifact" if audio_mode else "API response"
            limit_error = (
                "Article audio artifact exceeded download limit"
                if audio_mode
                else "API response exceeded the 8 MiB limit"
            )
            expected_size = None
            if not is_error:
                if (
                    "Content-Range" in response.headers
                    or status == 206
                    or (audio_mode and status != 200)
                ):
                    raise UpstreamResponseError(
                        "Article audio artifact did not return a complete HTTP 200 representation"
                        if audio_mode
                        else "API response did not return a complete representation"
                    )
                expected_size = _content_length(response, representation)
                if audio_mode and response.headers.get_content_type().lower() not in {
                    "audio/mpeg",
                    "audio/mp3",
                }:
                    raise UpstreamResponseError("Article audio artifact is not MP3 audio")
                if expected_size is not None and expected_size > limit:
                    raise UpstreamResponseError(limit_error)
            # HTTP errors are classified by status and need no response headers.
            headers = [] if is_error else _receipt_headers(response.headers)
            size = 0
            with Path(options["path"]).open("wb") as output:
                while size <= limit:
                    chunk = response.read(min(64 * 1024, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        if is_error:
                            output.write(chunk[: len(chunk) - (size - limit)])
                            break
                        raise UpstreamResponseError(limit_error)
                    output.write(chunk)
                    if is_error and size == limit:
                        break
            if not is_error and audio_mode and not size:
                raise UpstreamResponseError("Article audio artifact is empty")
            if expected_size is not None and size != expected_size:
                raise UpstreamResponseError(f"{representation} download was incomplete")
            info = {
                "status": status,
                "is_error": is_error,
                "headers": headers,
            }
        print(json.dumps(info))
        return 0
    except UpstreamResponseError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except Exception:
        # Do not echo URLs, credentials, response bodies or interpreter tracebacks.
        print(json.dumps({"error": "Upstream transport failed or response exceeded limit"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
