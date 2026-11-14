"""Bound diagnostic work for disposable media commands (POSIX workers)."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryFile
from time import monotonic, sleep
from typing import BinaryIO

# Count each stream independently; retain no upstream diagnostic text.
MAX_DIAGNOSTIC_BYTES = 1024 * 1024


class MediaProcessError(RuntimeError):
    """Fixed categories suitable for persisted media failures."""


def run_media_command(
    command: list[str],
    *,
    timeout: float,
    input_data: bytes = b"",
    output_path: Path | None = None,
    max_output_bytes: int | None = None,
) -> None:
    deadline = monotonic() + timeout
    if timeout <= 0:
        raise MediaProcessError("timed out")
    with TemporaryFile() as request:
        request.write(input_data)
        request.seek(0)
        _run_media_command(
            command,
            request=request,
            deadline=deadline,
            output_path=output_path,
            max_output_bytes=max_output_bytes,
        )
        if monotonic() >= deadline:
            raise MediaProcessError("timed out")


def _run_media_command(
    command: list[str],
    *,
    request: BinaryIO,
    deadline: float,
    output_path: Path | None,
    max_output_bytes: int | None,
) -> None:
    process = subprocess.Popen(
        command,
        stdin=request,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    retired = False
    primary_error: BaseException | None = None
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, 0)
            while selector.get_map() or process.poll() is None:
                # A successful wrapper can leave a child holding these pipes.
                if process.poll() is not None and not retired:
                    if process.returncode:
                        raise MediaProcessError("failed")
                    _retire_process_group(process)
                    retired = True
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise MediaProcessError("timed out")
                _check_output_size(output_path, max_output_bytes)
                for key, _ in selector.select(min(remaining, 0.02)):
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total = key.data + len(chunk)
                    if total > MAX_DIAGNOSTIC_BYTES:
                        raise MediaProcessError("exceeded the diagnostic limit")
                    selector.modify(key.fileobj, selectors.EVENT_READ, total)
            _check_output_size(output_path, max_output_bytes)
            if process.returncode:
                raise MediaProcessError("failed")
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if not retired:
                _retire_process_group(process)
        except (OSError, subprocess.TimeoutExpired):
            if primary_error is None:
                raise MediaProcessError("process cleanup failed") from None
            primary_error.add_note("Media process cleanup failed")
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _retire_process_group(process: subprocess.Popen[bytes]) -> None:
    # The session is ours even after its leader exits. Always retire descendants,
    # including children with redirected streams, before releasing temporary files.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = monotonic() + 0.2
    while monotonic() < deadline:
        process.poll()  # reap the leader while descendants settle
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            process.wait()
            return
        sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=1)


def _check_output_size(path: Path | None, limit: int | None) -> None:
    # ffmpeg -fs can overshoot by one encoded packet and exits successfully when
    # it truncates. Refuse the boundary too rather than publish truncated audio.
    if path is not None and limit is not None and path.exists() and path.stat().st_size >= limit:
        raise MediaProcessError("output limit")
