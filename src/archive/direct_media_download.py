"""Bounded urllib download, isolated so its parent can stop every blocking call."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class HTTPOnlyRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme not in {"http", "https"}:
            raise ValueError("Unsupported media redirect scheme")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main() -> int:
    try:
        options = json.load(sys.stdin)
        if urlparse(options["url"]).scheme not in {"http", "https"}:
            raise ValueError("Unsupported media URL scheme")
        request = Request(options["url"], headers=options["headers"])
        with build_opener(HTTPOnlyRedirects()).open(
            request, timeout=options["timeout"]
        ) as response:
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
