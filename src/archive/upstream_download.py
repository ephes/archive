"""Disposable authenticated transport; invoked only by upstream_http."""

import importlib.util
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request


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
        if options["data_path"] is not None and not request.has_header("Content-length"):
            request.add_header("Content-Length", str(Path(options["data_path"]).stat().st_size))
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
            if options.get("audio") and not is_error:
                if response.headers.get_content_type().lower() not in {"audio/mpeg", "audio/mp3"}:
                    raise UpstreamResponseError("Article audio artifact is not MP3 audio")
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > limit:
                    raise UpstreamResponseError("Article audio artifact exceeded download limit")
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
                        raise UpstreamResponseError(
                            "Article audio artifact exceeded download limit"
                            if options.get("audio")
                            else "API response exceeded the 8 MiB limit"
                        )
                    output.write(chunk)
                    if is_error and size == limit:
                        break
            info = {
                "status": status,
                "is_error": is_error,
                "headers": list(response.headers.items()),
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
