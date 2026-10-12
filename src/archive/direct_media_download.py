"""Bounded urllib download, isolated so its parent can stop every blocking call."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
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
            size = 0
            with Path(options["path"]).open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > options["max_bytes"]:
                        raise ValueError(
                            "Media source exceeded the configured "
                            f"{options['max_bytes']}-byte archive limit"
                        )
                    output.write(chunk)
            print(
                json.dumps(
                    {"url": response.geturl(), "content_type": response.headers.get_content_type()}
                )
            )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
