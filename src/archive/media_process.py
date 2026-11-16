"""Bound diagnostic work for disposable media commands (POSIX workers)."""

from __future__ import annotations

import errno
import os
import select
import selectors
import signal
import subprocess
import sys
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
        # Retirement time after an observed exit extends the execution deadline.
        deadline = _run_media_command(
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
) -> float:
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
            while selector.get_map() or not retired:
                # A successful wrapper can leave a child holding these pipes.
                if not retired and _leader_has_exited(process.pid):
                    # Retirement owns all remaining signals and the final reap.
                    # Until it reaps the leader, the finally block retries it.
                    retirement_started = monotonic()
                    try:
                        _retire_process_group(process)
                    except (OSError, subprocess.TimeoutExpired):
                        if process.returncode:
                            error = MediaProcessError("failed")
                            error.add_note("Media process cleanup failed")
                            raise error from None
                        raise MediaProcessError("process cleanup failed") from None
                    retired = True
                    if process.returncode:
                        raise MediaProcessError("failed")
                    # The fixed TERM grace and bounded reap are cleanup, not
                    # command runtime: a success near the deadline stays a success.
                    deadline += monotonic() - retirement_started
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
            return deadline
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            # Skipped once the leader is reaped; until then the group is still ours.
            if process.returncode is None:
                _retire_process_group(process)
        except BaseException as exc:
            cleanup_error = exc
        for owned_stream in (process.stdout, process.stderr):
            try:
                if owned_stream is not None:
                    owned_stream.close()
            except BaseException as exc:
                cleanup_error = _keep_cancellation(cleanup_error, exc)
        if cleanup_error is not None:
            if primary_error is None:
                if not isinstance(cleanup_error, Exception):
                    raise cleanup_error
                raise MediaProcessError("process cleanup failed") from None
            already_cleanup_failure = isinstance(primary_error, MediaProcessError) and (
                str(primary_error) == "process cleanup failed"
            )
            if not already_cleanup_failure:
                primary_error.add_note("Media process cleanup failed")
            if isinstance(primary_error, Exception) and not isinstance(cleanup_error, Exception):
                # A cancellation (for example a worker stall alarm) during cleanup
                # must still reach its handler instead of becoming a note.
                raise cleanup_error


def _leader_has_exited(pid: int) -> bool:
    # poll()/waitpid() release the PID reservation. Observe without reaping so
    # the final group signal cannot target a recycled numeric process group.
    if hasattr(os, "waitid"):
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    if sys.platform == "linux" or sys.platform == "win32":
        raise MediaProcessError("process exit observation unavailable")
    # Python 3.12 on macOS does not expose waitid. A process kqueue also observes
    # without reaping; registering an already-exited child can return ESRCH.
    queue = select.kqueue()
    try:
        event = select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
            fflags=select.KQ_NOTE_EXIT,
        )
        try:
            events = queue.control([event], 1, 0)
        except ProcessLookupError:
            return True
        for result in events:
            if result.flags & select.KQ_EV_ERROR:
                if result.data == errno.ESRCH:
                    return True
                raise OSError(result.data, "process observation failed")
        return bool(events)
    finally:
        queue.close()


def _retire_process_group(process: subprocess.Popen[bytes]) -> None:
    # The session is ours even after its leader exits. Always retire descendants,
    # including children with redirected streams, before releasing temporary files.
    if process.returncode is not None:
        return  # No signal is safe after the leader has been reaped.
    cleanup_error: BaseException | None = None
    try:
        _signal_owned_group(process.pid, signal.SIGTERM)
        deadline = monotonic() + 0.2
        while monotonic() < deadline:
            sleep(0.01)
    except BaseException as exc:
        cleanup_error = exc
    # Independent final attempts: interruption of TERM/grace must not skip KILL,
    # and a signal error must not skip the bounded direct-child reap.
    try:
        _signal_owned_group(process.pid, signal.SIGKILL)
    except BaseException as exc:
        cleanup_error = _keep_cancellation(cleanup_error, exc)
    try:
        process.wait(timeout=1)
    except BaseException as exc:
        cleanup_error = _keep_cancellation(cleanup_error, exc)
    if cleanup_error is not None:
        raise cleanup_error


def _keep_cancellation(recorded: BaseException | None, new: BaseException) -> BaseException:
    # Keep the first cleanup error, except that a later cancellation (such as the
    # worker stall alarm) replaces an ordinary one so it still reaches its handler.
    if recorded is None or (isinstance(recorded, Exception) and not isinstance(new, Exception)):
        return new
    return recorded


def _signal_owned_group(pgid: int, sig: int) -> None:
    # Callers keep the leader unreaped, so the group identity is still ours.
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin reports EPERM, not ESRCH, when only zombies (such as the
        # unreaped leader) remain in the group: no live member is left to signal.
        # Linux signals zombie-only groups successfully, so EPERM stays an error.
        if sys.platform != "darwin":
            raise


def _check_output_size(path: Path | None, limit: int | None) -> None:
    # ffmpeg -fs can overshoot by one encoded packet and exits successfully when
    # it truncates. Refuse the boundary too rather than publish truncated audio.
    if path is not None and limit is not None and path.exists() and path.stat().st_size >= limit:
        raise MediaProcessError("output limit")
