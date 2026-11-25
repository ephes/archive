"""Disposable yt-dlp operation. Supervision and admission belong to the parent."""

import json
import os
import selectors
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from time import monotonic

import yt_dlp
import yt_dlp.postprocessor.ffmpeg as ytdlp_ffmpeg

# Same per-stream budget as archive.media_process.MAX_DIAGNOSTIC_BYTES. This
# script runs isolated (python -I) and imports nothing from the application.
MAX_DIAGNOSTIC_BYTES = 1024 * 1024
# Exit status for a downloaded source over ``max_source_bytes``; the parent
# (archive.media_archival) reports it as the archive limit.
SOURCE_LIMIT_EXIT_CODE = 3


class SourceLimitError(RuntimeError):
    """A downloaded source file grew past its byte limit."""


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
                with suppress(OSError, subprocess.TimeoutExpired):
                    process.kill(timeout=None)
                raise
        if deadline is not None and monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout or 0)
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


def source_limit_hook(limit: int, exceeded: list[bool]):
    """A progress hook that stops a download once its file passes ``limit`` bytes.

    yt-dlp ignores ``max_filesize`` without a ``Content-Length`` and may rewrite
    the downloaded file in its fixups before any conversion. A ``finished`` event
    comes before every postprocessor, so the original is measured here, on disk.
    """

    def hook(status) -> None:
        size = status.get("downloaded_bytes")
        if status.get("status") == "finished":
            filename = status.get("filename")
            size = os.lstat(filename).st_size if isinstance(filename, str) else None
        if isinstance(size, int) and size > limit:
            exceeded.append(True)
            raise SourceLimitError("Downloaded source exceeded its byte limit")

    return hook


def output_limit_postprocessor_hook(directory: str, limit: int, exceeded: list[bool]):
    """A postprocessor hook that measures every output file before each postprocessor.

    Some downloaders (FFmpeg-delegated HLS, for example) do not forward progress
    hooks, and fixups may replace the downloaded file. Each postprocessor reports
    ``started`` before it touches a file, so an oversized source is refused there.
    """

    def hook(status) -> None:
        if status.get("status") != "started":
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_size > limit:
                        exceeded.append(True)
                        raise SourceLimitError("Downloaded source exceeded its byte limit")

    return hook


def download(request, exceeded: list[bool] | None = None) -> None:
    options = dict(request["options"])
    limit = request.get("max_source_bytes")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("max_source_bytes must be a positive integer")
        flag = exceeded if exceeded is not None else []
        directory = os.path.dirname(os.path.abspath(str(options["outtmpl"])))
        options["progress_hooks"] = [source_limit_hook(limit, flag)]
        options["postprocessor_hooks"] = [output_limit_postprocessor_hook(directory, limit, flag)]
    with bounded_ffmpeg_output(), yt_dlp.YoutubeDL(options) as downloader:
        downloader.extract_info(request["url"], download=True)


def main() -> int:
    exceeded: list[bool] = []
    try:
        download(json.load(sys.stdin), exceeded)
    except Exception:
        # Do not emit an additional raw exception or traceback; the parent
        # discards and bounds any diagnostics produced by yt-dlp itself.
        return SOURCE_LIMIT_EXIT_CODE if exceeded else 1
    return SOURCE_LIMIT_EXIT_CODE if exceeded else 0


if __name__ == "__main__":
    sys.exit(main())
