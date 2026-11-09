"""Exercise the real subprocess boundary using only synthetic transports."""

import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request

import pytest

from archive import transcriptions, upstream_http


@pytest.mark.parametrize("stage", ["dns", "headers", "body", "error", "close"])
def test_authenticated_exchange_deadline(monkeypatch, tmp_path, stage):
    marker = tmp_path / "entered"
    helper = Path(upstream_http.__file__).with_name("upstream_download.py")
    script = """
import runpy, sys, time
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
ns = runpy.run_path(sys.argv[1])
marker = Path(sys.argv[2])
stage = sys.argv[3]
def stall():
    marker.write_text(stage)
    time.sleep(10)
class Response:
    code = 200
    closed = False
    def close(self): self.closed = True
    headers = Message()
    def __enter__(self): return self
    def __exit__(self, *args):
        if stage == 'close': stall()
    def read(self, n):
        if stage in ('body', 'error'): stall()
        return b''
def open_url(request, timeout):
    assert request.get_header('Authorization') == 'Bearer SYNTHETIC'
    if stage in ('dns', 'headers'): stall()
    response = Response()
    if stage == 'error':
        raise HTTPError(request.full_url, 500, 'Synthetic', response.headers, response)
    return response
ns['main'].__globals__['_load_transport'] = lambda: SimpleNamespace(
    _open_authenticated_url=open_url)
raise SystemExit(ns['main']())
"""
    real_run = subprocess.run

    def run(command, **kwargs):
        assert command[-1] == str(helper)
        return real_run(
            [sys.executable, "-I", "-c", script, str(helper), str(marker), stage], **kwargs
        )

    # Keep the pre-fix opener synthetic too, so the regression never contacts DNS.
    from types import SimpleNamespace

    def old_open(*args, **kwargs):
        marker.write_text(stage)
        time.sleep(0.7)
        raise OSError("synthetic unbounded transport")

    monkeypatch.setattr(upstream_http, "build_opener", lambda *a: SimpleNamespace(open=old_open))
    monkeypatch.setattr(upstream_http, "subprocess", subprocess, raising=False)
    monkeypatch.setattr(subprocess, "run", run)
    start = time.monotonic()
    with pytest.raises(OSError, match="total timeout"):
        with upstream_http.open_authenticated_url(
            Request("https://api.test/", headers={"Authorization": "Bearer SYNTHETIC"}),
            timeout=0.5,
        ):
            pytest.fail("late response published")
    assert marker.read_text() == stage
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("late", [False, True])
def test_poll_does_not_accept_or_request_after_deadline(monkeypatch, settings, late):
    clock = [0.0]
    calls = []
    settings.ARCHIVE_TRANSCRIPTION_POLL_SECONDS = 1 if late else 10
    monkeypatch.setattr(transcriptions, "monotonic", lambda: clock[0])
    monkeypatch.setattr(transcriptions, "sleep", lambda seconds: clock.__setitem__(0, seconds))

    def poll(job_id, timeout):
        calls.append(timeout)
        clock[0] = 10
        return transcriptions.BatchTranscriptionJobUpdate(job_id, "succeeded", "words")

    monkeypatch.setattr(transcriptions, "_poll_batch_transcription_job", poll)
    with pytest.raises(transcriptions.TranscriptionGenerationError, match="Timed out"):
        transcriptions._wait_for_batch_transcription_job(
            update=transcriptions.BatchTranscriptionJobUpdate("synthetic", "running"), timeout=10
        )
    assert calls == ([9] if late else [])
