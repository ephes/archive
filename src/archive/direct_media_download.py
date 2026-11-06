"""Bounded urllib download, isolated so its parent can stop every blocking call."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request


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


def main() -> int:
    try:
        options = json.load(sys.stdin)
        request = Request(options["url"], headers=options["headers"])
        with _load_safe_fetch().open_public_url(request, timeout=options["timeout"]) as response:
            metadata_mode = options.get("metadata", False)
            content_type = response.headers.get_content_type()
            if (
                metadata_mode
                and content_type
                and not any(kind in content_type for kind in ("html", "xml"))
            ):
                raise ValueError(f"Unsupported content type: {content_type}")
            size = 0
            with Path(options["path"]).open("wb") as output:
                while not metadata_mode or size < options["max_bytes"]:
                    read_size = (
                        min(8192, options["max_bytes"] - size) if metadata_mode else 1024 * 1024
                    )
                    chunk = response.read(read_size)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > options["max_bytes"]:
                        raise ValueError(
                            "Media source exceeded the configured "
                            f"{options['max_bytes']}-byte archive limit"
                        )
                    output.write(chunk)
            print(
                json.dumps(
                    {
                        "url": response.geturl(),
                        "content_type": content_type,
                        "charset": response.headers.get_content_charset() or "utf-8",
                    }
                )
            )
    except HTTPError as exc:
        print(json.dumps({"error": str(exc), "http_status": exc.code}))
        return 1
    except URLError as exc:
        print(json.dumps({"error": str(exc), "url_reason": str(exc.reason)}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
