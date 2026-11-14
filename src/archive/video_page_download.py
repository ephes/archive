"""Disposable yt-dlp operation. Supervision and admission belong to the parent."""

import json
import sys

import yt_dlp


def download(request) -> None:
    with yt_dlp.YoutubeDL(request["options"]) as downloader:
        downloader.extract_info(request["url"], download=True)


def main() -> int:
    try:
        download(json.load(sys.stdin))
    except Exception:
        # Do not emit an additional raw exception or traceback; the parent
        # discards and bounds any diagnostics produced by yt-dlp itself.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
