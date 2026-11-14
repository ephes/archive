"""Synthetic local process trees, without network or archive storage."""

import os
import signal
import sys
from pathlib import Path
from time import monotonic, sleep

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
