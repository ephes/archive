"""yt-dlp's own FFmpeg calls inside the disposable video-page download child.

These drive yt-dlp's real FFmpeg postprocessor against a synthetic local ffmpeg,
without network access, so a yt-dlp refactor that bypasses the bound fails here.
"""

import os
import sys
from pathlib import Path

import pytest
import yt_dlp
import yt_dlp.postprocessor.ffmpeg as ytdlp_ffmpeg
from yt_dlp.utils import Popen as YtdlpPopen

from archive import video_page_download
from archive.media_process import MAX_DIAGNOSTIC_BYTES

# Finite on purpose: an unbounded collector must still finish (and fail the test).
SYNTHETIC_FLOOD_BYTES = 8 * MAX_DIAGNOSTIC_BYTES


def _install_ffmpeg(tmp_path: Path, body: str) -> Path:
    location = tmp_path / "bin"
    location.mkdir()
    script = location / "ffmpeg"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\nfrom pathlib import Path\n"
        "if '-bsfs' in sys.argv:\n"
        "    print('ffmpeg version 7.1 Copyright (c) the FFmpeg developers')\n"
        "    sys.exit(0)\n"
        f"Path({str(tmp_path / 'pid')!r}).write_text(str(os.getpid()))\n"
        "output = Path(sys.argv[-1].removeprefix('file:'))\n" + body
    )
    script.chmod(0o700)
    return location


def _download_running_ffmpeg(monkeypatch, tmp_path: Path, location: Path) -> list[object]:
    """Run download() whose extraction step is a single yt-dlp FFmpeg conversion."""
    source = tmp_path / "source.m4a"
    source.write_bytes(b"synthetic")
    results: list[object] = []

    def extract_info(self, url, download):
        postprocessor = ytdlp_ffmpeg.FFmpegPostProcessor(self)
        results.append(postprocessor.run_ffmpeg(str(source), str(tmp_path / "out.mp3"), []))

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    video_page_download.download(
        {
            "url": "https://www.youtube.com/watch?v=demo123",
            "options": {
                "ffmpeg_location": str(location),
                "noprogress": True,
                "quiet": True,
            },
        }
    )
    return results


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_nested_ffmpeg_output_is_bounded_and_the_command_stopped(stream, monkeypatch, tmp_path):
    finished = tmp_path / "finished"
    location = _install_ffmpeg(
        tmp_path,
        f"chunk = b'x' * 65536\n"
        f"for _ in range({SYNTHETIC_FLOOD_BYTES} // len(chunk)):\n"
        f"    sys.{stream}.buffer.write(chunk)\n"
        f"    sys.{stream}.buffer.flush()\n"
        "output.write_bytes(b'ID3')\n"
        f"Path({str(finished)!r}).touch()\n",
    )

    with pytest.raises(RuntimeError, match="^FFmpeg output exceeded the diagnostic limit$"):
        _download_running_ffmpeg(monkeypatch, tmp_path, location)

    assert not finished.exists()
    assert _gone(int((tmp_path / "pid").read_text()))
    assert ytdlp_ffmpeg.Popen is YtdlpPopen


def test_nested_ffmpeg_within_budget_keeps_ytdlp_results(monkeypatch, tmp_path):
    location = _install_ffmpeg(
        tmp_path,
        "sys.stderr.write('size=1kB time=00:00:01\\r' * 20 + 'done\\r\\n')\n"
        "output.write_bytes(b'ID3')\n",
    )

    results = _download_running_ffmpeg(monkeypatch, tmp_path, location)

    assert results == ["size=1kB time=00:00:01\n" * 20 + "done\n"]
    assert (tmp_path / "out.mp3").read_bytes() == b"ID3"
    assert ytdlp_ffmpeg.Popen is YtdlpPopen


def test_nested_ffmpeg_failure_keeps_ytdlp_error(monkeypatch, tmp_path):
    location = _install_ffmpeg(
        tmp_path, "sys.stderr.write('noise\\nfinal reason\\n')\nsys.exit(1)\n"
    )

    with pytest.raises(ytdlp_ffmpeg.FFmpegPostProcessorError, match="^final reason$"):
        _download_running_ffmpeg(monkeypatch, tmp_path, location)
    assert ytdlp_ffmpeg.Popen is YtdlpPopen


def test_child_budget_matches_parent_budget():
    assert video_page_download.MAX_DIAGNOSTIC_BYTES == MAX_DIAGNOSTIC_BYTES
