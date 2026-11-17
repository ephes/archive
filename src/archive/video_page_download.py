"""Disposable yt-dlp operation. Supervision and admission belong to the parent."""

import json
import os
import selectors
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic

import yt_dlp
import yt_dlp.postprocessor.ffmpeg as ytdlp_ffmpeg

# Same per-stream budget as archive.media_process.MAX_DIAGNOSTIC_BYTES. This
# script runs isolated (python -I) and imports nothing from the application.
MAX_DIAGNOSTIC_BYTES = 1024 * 1024


class DiagnosticLimitError(RuntimeError):
    """A nested FFmpeg/ffprobe stream exceeded its byte budget."""

    def __init__(self) -> None:
        super().__init__("FFmpeg output exceeded the diagnostic limit")


class BoundedPopen(ytdlp_ffmpeg.Popen):
    """yt-dlp's Popen whose run() caps each captured stream.

    yt-dlp's FFmpeg postprocessors call ``Popen.run`` with captured stdout and
    stderr, which buffers both streams whole in memory. Read them here with a
    separate byte budget each; past it, kill the command and fail.
    """

    @classmethod
    def run(cls, *args, timeout=None, input=None, **kwargs):  # noqa: A002 - yt-dlp's signature
        if input is not None:
            raise ValueError("bounded FFmpeg commands take no input")
        text = bool(
            kwargs.pop("text", False)
            or kwargs.pop("universal_newlines", False)
            or kwargs.get("encoding")
            or kwargs.get("errors")
        )
        encoding = kwargs.pop("encoding", None) or "utf-8"
        errors = kwargs.pop("errors", None) or "replace"
        if kwargs.get("stdin") == subprocess.PIPE:
            kwargs["stdin"] = subprocess.DEVNULL  # communicate() closes it unused
        deadline = None if timeout is None else monotonic() + timeout
        with cls(*args, **kwargs) as process:
            try:
                stdout, stderr = _read_bounded(process, deadline, timeout)
                remaining = None if deadline is None else max(deadline - monotonic(), 0)
                process.wait(timeout=remaining)
            except BaseException:
                process.kill(timeout=None)
                raise
        if text:
            return (
                _decode(stdout, encoding, errors),
                _decode(stderr, encoding, errors),
                (process.returncode),
            )
        return bytes(stdout), bytes(stderr), process.returncode


def _read_bounded(
    process: subprocess.Popen[bytes], deadline: float | None, timeout: float | None
) -> tuple[bytearray, bytearray]:
    stdout, stderr = bytearray(), bytearray()
    with selectors.DefaultSelector() as selector:
        for stream, buffer in ((process.stdout, stdout), (process.stderr, stderr)):
            if stream is not None:
                selector.register(stream, selectors.EVENT_READ, buffer)
        while selector.get_map():
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout or 0)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(key.data) + len(chunk) > MAX_DIAGNOSTIC_BYTES:
                    raise DiagnosticLimitError
                key.data.extend(chunk)
    return stdout, stderr


def _decode(data: bytearray, encoding: str, errors: str) -> str:
    # Match text-mode pipes: universal newlines, as yt-dlp's parsing expects.
    return data.decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")


@contextmanager
def bounded_ffmpeg_output() -> Iterator[None]:
    original = ytdlp_ffmpeg.Popen
    ytdlp_ffmpeg.Popen = BoundedPopen
    try:
        yield
    finally:
        ytdlp_ffmpeg.Popen = original


def download(request) -> None:
    with bounded_ffmpeg_output(), yt_dlp.YoutubeDL(request["options"]) as downloader:
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
