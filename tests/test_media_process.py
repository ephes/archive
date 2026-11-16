"""Synthetic local process trees, without network or archive storage."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from archive.media_archival import _extract_audio_with_ffmpeg


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Linux may briefly retain an orphan zombie; it can no longer execute or
    # hold files/pipes. macOS reaps these through launchd.
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        return proc_stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return not Path("/proc").exists()


@pytest.mark.parametrize("ending", ["success", "timeout"])
@pytest.mark.parametrize("pipes", [True, False])
def test_ffmpeg_retires_owned_descendants(ending, pipes, tmp_path, settings):
    marker = tmp_path / "pid"
    ready = tmp_path / "ready"
    child_code = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(ready)!r}).touch(); time.sleep(10)"
    )
    script = tmp_path / "wrapper"
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        + ("" if pipes else "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, ")
        + ")\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(.005)\n"
        "Path(sys.argv[-1]).write_bytes(b'ID3')\n"
        + ("time.sleep(10)\n" if ending == "timeout" else "")
    )
    script.chmod(0o700)
    settings.ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN = str(script)
    started = monotonic()
    try:
        if ending == "timeout":
            with pytest.raises(RuntimeError, match="timed out"):
                _extract_audio_with_ffmpeg(
                    source_path=tmp_path / "source", output_path=tmp_path / "out", timeout=0.5
                )
        else:
            _extract_audio_with_ffmpeg(
                source_path=tmp_path / "source", output_path=tmp_path / "out", timeout=2
            )
        pid = int(marker.read_text())
        deadline = monotonic() + 1
        while _alive(pid) and monotonic() < deadline:
            sleep(0.01)
        assert not _alive(pid)
        assert monotonic() - started < 3
        # Independent follow-up extraction remains usable after cleanup.
        script.write_text(
            f"#!{sys.executable}\nimport sys\nopen(sys.argv[-1], 'wb').write(b'ID3')\n"
        )
        _extract_audio_with_ffmpeg(
            source_path=tmp_path / "source", output_path=tmp_path / "next", timeout=1
        )
        assert (tmp_path / "next").read_bytes() == b"ID3"
    finally:
        if marker.exists():
            pid = int(marker.read_text())
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("excess", ["", "stdout", "stderr"])
def test_diagnostic_budget_counts_each_stream_independently(excess, monkeypatch):
    from archive import media_process

    monkeypatch.setattr(media_process, "MAX_DIAGNOSTIC_BYTES", 1024)
    code = "import sys; sys.stdout.buffer.write(b'x'*1024); sys.stderr.buffer.write(b'y'*1024);"
    if excess:
        code += f"sys.{excess}.buffer.write(b'z');"
        with pytest.raises(media_process.MediaProcessError, match="diagnostic limit"):
            media_process.run_media_command([sys.executable, "-I", "-c", code], timeout=2)
    else:
        media_process.run_media_command([sys.executable, "-I", "-c", code], timeout=2)


def test_cleanup_failure_keeps_primary_extraction_error(monkeypatch):
    from archive import media_process

    retire = media_process._retire_process_group

    def fail_after_cleanup(process):
        retire(process)
        raise OSError("synthetic-private-marker")

    monkeypatch.setattr(media_process, "_retire_process_group", fail_after_cleanup)
    with pytest.raises(media_process.MediaProcessError, match="^failed") as caught:
        media_process.run_media_command([sys.executable, "-I", "-c", "exit(1)"], timeout=2)
    assert str(caught.value) == "failed"
    assert caught.value.__notes__ == ["Media process cleanup failed"]


@pytest.mark.parametrize("ending", ["success", "failure", "timeout"])
def test_group_identity_stays_reserved_until_last_signal(ending, monkeypatch):
    from archive import media_process

    events = []
    clock = 0.0

    def now():
        nonlocal clock
        clock += 0.05
        return clock

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            streams = []
            for _ in range(2):
                reader, writer = os.pipe()
                os.close(writer)
                streams.append(os.fdopen(reader, "rb"))
            self.stdout, self.stderr = streams

        def poll(self):
            if ending != "timeout" or signal.SIGTERM in events:
                self.wait()
            return self.returncode

        def wait(self, timeout=None):
            # Reaping makes the numeric PID/PGID available to another owner.
            events.append("reap")
            self.returncode = 1 if ending == "failure" else 0
            return self.returncode

    def observe(idtype, pid, options):
        assert (idtype, pid) == (os.P_PID, Process.pid)
        assert options == os.WEXITED | os.WNOHANG | os.WNOWAIT
        events.append("observe")
        if ending == "timeout":
            return None
        return SimpleNamespace(si_status=1 if ending == "failure" else 0, si_code=os.CLD_EXITED)

    def signal_group(pid, sig):
        assert pid == Process.pid
        assert "reap" not in events, "signal reached a recycled process group"
        events.append(sig)

    monkeypatch.setattr(media_process.subprocess, "Popen", Process)
    monkeypatch.setattr(media_process.os, "waitid", observe, raising=False)
    monkeypatch.setattr(media_process.os, "killpg", signal_group)
    monkeypatch.setattr(media_process, "monotonic", now)
    monkeypatch.setattr(media_process, "sleep", lambda _: None)
    if ending == "success":
        media_process.run_media_command(["synthetic"], timeout=2)
    else:
        with pytest.raises(
            media_process.MediaProcessError,
            match=("failed" if ending == "failure" else "timed out"),
        ):
            media_process.run_media_command(["synthetic"], timeout=2)
    assert events.count("reap") == 1
    assert signal.SIGTERM in events and signal.SIGKILL in events


@pytest.mark.parametrize("status", [0, 7])
def test_exit_observation_does_not_reap_child(status):
    from archive import media_process

    process = subprocess.Popen([sys.executable, "-I", "-c", f"exit({status})"])
    try:
        deadline = monotonic() + 2
        while not media_process._leader_has_exited(process.pid):
            assert monotonic() < deadline
            sleep(0.01)
        assert process.returncode is None
        assert process.wait(timeout=1) == status
    finally:
        if process.returncode is None:
            process.kill()
            process.wait(timeout=1)


@pytest.mark.parametrize("stage", ["term", "grace", "kill", "wait"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, OSError])
def test_retirement_attempts_remaining_actions_after_cleanup_error(stage, error_type, monkeypatch):
    from archive import media_process

    events = []
    error = error_type("synthetic-private-marker")

    class Process:
        pid = 12345
        returncode = None

        def wait(self, timeout):
            events.append(("wait", timeout))
            if stage == "wait":
                raise error
            self.returncode = 0

    def signal_group(pid, sig):
        assert pid == Process.pid
        events.append(sig)
        if (stage, sig) in (("term", signal.SIGTERM), ("kill", signal.SIGKILL)):
            raise error

    def pause(_):
        events.append("grace")
        if stage == "grace":
            raise error

    monkeypatch.setattr(media_process.os, "killpg", signal_group)
    monkeypatch.setattr(media_process, "sleep", pause)
    with pytest.raises(error_type) as caught:
        media_process._retire_process_group(Process())
    assert caught.value is error
    assert events[0] == signal.SIGTERM
    assert events[-2:] == [signal.SIGKILL, ("wait", 1)]


@pytest.mark.parametrize("primary", [None, "failure", "interrupt"])
@pytest.mark.parametrize("close_error", [RuntimeError, KeyboardInterrupt])
def test_cleanup_closes_both_streams_and_preserves_primary(primary, close_error, monkeypatch):
    from archive import media_process

    events = []
    error = (
        KeyboardInterrupt("primary")
        if primary == "interrupt"
        else media_process.MediaProcessError("output limit")
    )

    class Stream:
        def __init__(self, name):
            self.name = name
            reader, writer = os.pipe()
            os.close(writer)
            self.file = os.fdopen(reader, "rb")

        def fileno(self):
            return self.file.fileno()

        def close(self):
            events.append(self.name)
            self.file.close()
            # Even another interruption must not skip the other stream.
            raise close_error("synthetic-private-marker")

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            self.stdout = Stream("stdout")
            self.stderr = Stream("stderr")

        def wait(self, timeout):
            events.append(("wait", timeout))
            self.returncode = 0

    def check_size(*args):
        if primary:
            raise error

    def pause(_):
        # A second interruption while handling the primary failure.
        raise KeyboardInterrupt("cleanup")

    monkeypatch.setattr(media_process.subprocess, "Popen", Process)
    monkeypatch.setattr(media_process, "_leader_has_exited", lambda _: primary is None)
    monkeypatch.setattr(media_process, "_check_output_size", check_size)
    monkeypatch.setattr(media_process.os, "killpg", lambda pid, sig: events.append(sig))
    if primary:
        monkeypatch.setattr(media_process, "sleep", pause)
    if primary == "interrupt":
        expected = type(error)
    elif primary == "failure":
        # The cleanup cancellation wins over an ordinary primary failure.
        expected = KeyboardInterrupt
    else:
        expected = (
            close_error if close_error is KeyboardInterrupt else media_process.MediaProcessError
        )
    with pytest.raises(BaseException) as caught:
        media_process.run_media_command(["synthetic"], timeout=2)
    assert isinstance(caught.value, expected)
    if primary == "interrupt":
        assert caught.value is error
        assert caught.value.__notes__ == ["Media process cleanup failed"]
    elif primary == "failure":
        assert str(caught.value) == "cleanup"
        assert error.__notes__ == ["Media process cleanup failed"]
    elif close_error is RuntimeError:
        assert str(caught.value) == "process cleanup failed"
    assert events == [signal.SIGTERM, signal.SIGKILL, ("wait", 1), "stdout", "stderr"]


@pytest.mark.parametrize(
    ("platform", "error_type", "propagates"),
    [
        ("darwin", ProcessLookupError, False),
        ("darwin", PermissionError, False),
        ("linux", ProcessLookupError, False),
        ("linux", PermissionError, True),
        ("darwin", OSError, True),
    ],
)
def test_group_signal_tolerates_only_vanished_members(
    platform, error_type, propagates, monkeypatch
):
    from archive import media_process

    def signal_group(pid, sig):
        raise error_type("synthetic")

    monkeypatch.setattr(media_process.sys, "platform", platform)
    monkeypatch.setattr(media_process.os, "killpg", signal_group)
    if propagates:
        with pytest.raises(error_type):
            media_process._signal_owned_group(12345, signal.SIGTERM)
    else:
        media_process._signal_owned_group(12345, signal.SIGTERM)


def test_success_near_deadline_is_not_reported_as_timeout(monkeypatch):
    from archive import media_process

    clock = 0.0

    def now():
        return clock

    def pause(seconds):
        nonlocal clock
        clock += seconds

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            streams = []
            for _ in range(2):
                reader, writer = os.pipe()
                os.close(writer)
                streams.append(os.fdopen(reader, "rb"))
            self.stdout, self.stderr = streams

        def wait(self, timeout):
            self.returncode = 0
            return 0

    def exited(_):
        nonlocal clock
        # The command itself finishes just before its deadline.
        clock = 1.95
        return True

    monkeypatch.setattr(media_process.subprocess, "Popen", Process)
    monkeypatch.setattr(media_process, "_leader_has_exited", exited)
    monkeypatch.setattr(media_process.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(media_process, "monotonic", now)
    monkeypatch.setattr(media_process, "sleep", pause)
    media_process.run_media_command(["synthetic"], timeout=2)
    assert clock > 2  # retirement grace ran past the original deadline


@pytest.mark.parametrize("cancel_at", ["kill", "wait"])
def test_later_cleanup_cancellation_replaces_ordinary_cleanup_error(cancel_at, monkeypatch):
    from archive import media_process

    events = []

    class Process:
        pid = 12345
        returncode = None

        def wait(self, timeout):
            events.append("wait")
            if cancel_at == "wait":
                raise KeyboardInterrupt("stall")
            self.returncode = 0

    def signal_group(pid, sig):
        events.append(sig)
        if sig == signal.SIGTERM:
            raise OSError("synthetic-private-marker")
        if sig == signal.SIGKILL and cancel_at == "kill":
            raise KeyboardInterrupt("stall")

    monkeypatch.setattr(media_process.os, "killpg", signal_group)
    monkeypatch.setattr(media_process, "sleep", lambda _: None)
    with pytest.raises(KeyboardInterrupt, match="stall"):
        media_process._retire_process_group(Process())
    assert events == [signal.SIGTERM, signal.SIGKILL, "wait"]


def test_stream_close_cancellation_replaces_ordinary_retirement_error(monkeypatch):
    from archive import media_process

    events = []

    class Stream:
        def __init__(self, name):
            self.name = name
            reader, writer = os.pipe()
            os.close(writer)
            self.file = os.fdopen(reader, "rb")

        def fileno(self):
            return self.file.fileno()

        def close(self):
            events.append(self.name)
            self.file.close()
            if self.name == "stderr":
                raise KeyboardInterrupt("stall")

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            self.stdout = Stream("stdout")
            self.stderr = Stream("stderr")

    def failing_retirement(process):
        events.append("retire")
        raise OSError("synthetic-private-marker")

    def output_limit(*args):
        raise media_process.MediaProcessError("output limit")

    monkeypatch.setattr(media_process.subprocess, "Popen", Process)
    monkeypatch.setattr(media_process, "_leader_has_exited", lambda _: False)
    monkeypatch.setattr(media_process, "_check_output_size", output_limit)
    monkeypatch.setattr(media_process, "_retire_process_group", failing_retirement)
    with pytest.raises(KeyboardInterrupt, match="stall"):
        media_process.run_media_command(["synthetic"], timeout=2)
    assert events == ["retire", "stdout", "stderr"]


def test_interruption_before_retirement_still_retires_in_cleanup(monkeypatch):
    from archive import media_process

    events = []
    clock = 0.0
    interrupt_next_clock = False

    def now():
        nonlocal clock, interrupt_next_clock
        if interrupt_next_clock:
            interrupt_next_clock = False
            raise KeyboardInterrupt("stall")
        clock += 0.01
        return clock

    class Process:
        pid = 12345
        returncode = None

        def __init__(self, *args, **kwargs):
            streams = []
            for _ in range(2):
                reader, writer = os.pipe()
                os.close(writer)
                streams.append(os.fdopen(reader, "rb"))
            self.stdout, self.stderr = streams

        def wait(self, timeout):
            events.append("wait")
            self.returncode = 0

    def exited(_):
        nonlocal interrupt_next_clock
        # The stall alarm fires right after exit observation, before retirement.
        interrupt_next_clock = True
        return True

    monkeypatch.setattr(media_process.subprocess, "Popen", Process)
    monkeypatch.setattr(media_process, "_leader_has_exited", exited)
    monkeypatch.setattr(media_process.os, "killpg", lambda pid, sig: events.append(sig))
    monkeypatch.setattr(media_process, "monotonic", now)
    monkeypatch.setattr(media_process, "sleep", lambda _: None)
    with pytest.raises(KeyboardInterrupt, match="stall"):
        media_process.run_media_command(["synthetic"], timeout=2)
    assert events == [signal.SIGTERM, signal.SIGKILL, "wait"]
