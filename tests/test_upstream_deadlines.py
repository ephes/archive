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


@pytest.mark.parametrize("timing", ["in-time", "late", "expired"])
def test_poll_does_not_accept_or_request_after_deadline(monkeypatch, settings, timing):
    clock = [10.0 if timing == "expired" else 0.0]
    calls = []
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 0
    monkeypatch.setattr(transcriptions, "monotonic", lambda: clock[0])

    def poll(job_id, timeout, **kwargs):
        calls.append(timeout)
        clock[0] = 10 if timing == "late" else 5
        return transcriptions.BatchTranscriptionJobUpdate(job_id, "succeeded", "words")

    monkeypatch.setattr(transcriptions, "_poll_batch_transcription_job", poll)
    job = transcriptions.BatchTranscriptionJob("synthetic", "synthetic-ref")
    if timing == "in-time":
        assert transcriptions._poll_stored_batch_transcription_job(job=job, deadline=10) == "words"
    else:
        # A late or unrequested result stays pending; the job is polled again later.
        with pytest.raises(transcriptions.TranscriptionPending) as pending:
            transcriptions._poll_stored_batch_transcription_job(job=job, deadline=10)
        assert pending.value.job == job
        assert "timeout exceeded" in pending.value.poll_error
    assert calls == ([] if timing == "expired" else [10])


def test_poll_does_not_admit_a_transcript_normalized_after_deadline(monkeypatch, settings):
    clock = [0.0]
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 0
    monkeypatch.setattr(transcriptions, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        transcriptions,
        "_poll_batch_transcription_job",
        lambda job_id, timeout, **kwargs: transcriptions.BatchTranscriptionJobUpdate(
            job_id, "succeeded", "words"
        ),
    )

    def slow_normalize(value):
        clock[0] = 11
        return value

    monkeypatch.setattr(transcriptions, "_normalize_transcript", slow_normalize)
    job = transcriptions.BatchTranscriptionJob("synthetic", "synthetic-ref")
    with pytest.raises(transcriptions.TranscriptionPending) as pending:
        transcriptions._poll_stored_batch_transcription_job(job=job, deadline=10)
    assert pending.value.job == job
    assert "timeout exceeded" in pending.value.poll_error


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "iterable",
        "307",
        "500",
        "limit",
        "media",
        "wrong-type",
        "oversized-header",
        "irrelevant-headers",
        "oversized-receipt",
        "duplicate-type-audio-first",
        "duplicate-type-text-first",
        "parameterized-type",
        "missing-type",
    ],
)
def test_real_child_transport_round_trip(monkeypatch, case):
    from urllib.error import HTTPError

    helper = Path(upstream_http.__file__).with_name("upstream_download.py")
    script = """
import runpy, sys
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
ns = runpy.run_path(sys.argv[1])
case = sys.argv[2]
class Response(BytesIO):
    code = 200
    headers = Message()
    audio_cases = ('media', 'oversized-header', 'irrelevant-headers')
    if case != 'missing-type':
        headers['Content-Type'] = 'audio/mpeg' if case in audio_cases else 'text/plain'
    if case == 'duplicate-type-audio-first':
        headers.replace_header('Content-Type', 'audio/mpeg')
        headers['content-type'] = 'text/plain'
    if case == 'duplicate-type-text-first':
        headers['content-type'] = 'audio/mpeg'
    if case == 'parameterized-type':
        headers.replace_header('Content-Type', 'audio/mpeg; name="speech.mp3"')
    if case == 'irrelevant-headers':
        headers['cOnTeNt-LeNgTh'] = '8'
        for i in range(50):
            headers[f'X-Synthetic-{i}'] = 'x' * 4096
    if case == 'oversized-receipt':
        headers.replace_header('Content-Type', 'text/plain; name=' + 'x' * 4096)
    if case == 'oversized-header': headers['Content-Length'] = '999999999'
    def read(self, n=-1):
        assert case not in ('wrong-type', 'oversized-header'), 'must reject before reading'
        assert not case.startswith('duplicate-type'), 'must reject before reading'
        return super().read(n)
def open_url(request, timeout):
    assert request.data.read() == b'synthetic request'
    assert request.get_header('Content-length') == str(len(b'synthetic request'))
    assert request.get_header('Authorization') == 'Bearer SYNTHETIC'
    body = b'x' * (9 * 1024 * 1024) if case == 'media' else b'response'
    if case in ('307', '500'): body = b'e' * 70000
    response = Response(body)
    if case in ('307', '500'):
        raise HTTPError(request.full_url, int(case), 'Synthetic', response.headers, response)
    return response
ns['main'].__globals__['_load_transport'] = lambda: SimpleNamespace(
    _open_authenticated_url=open_url)
raise SystemExit(ns['main']())
"""
    real_run = subprocess.run

    def run(command, **kwargs):
        result = real_run([sys.executable, "-I", "-c", script, str(helper), case], **kwargs)
        if case == "irrelevant-headers":
            assert len(result.stdout) < 256
        return result

    monkeypatch.setattr(subprocess, "run", run)
    request = Request(
        "https://api.test/",
        data=(iter([b"synthetic ", b"request"]) if case == "iterable" else b"synthetic request"),
        headers={"Authorization": "Bearer SYNTHETIC"},
    )
    options = {"max_bytes": 10 * 1024 * 1024, "audio": True} if case == "media" else {}
    if case == "limit":
        options = {"max_bytes": 3}
    if case in (
        "wrong-type",
        "oversized-header",
        "irrelevant-headers",
        "duplicate-type-audio-first",
        "duplicate-type-text-first",
        "parameterized-type",
    ):
        options = {"audio": True}
    if case in ("307", "500"):
        with pytest.raises(HTTPError) as caught:
            with upstream_http.open_authenticated_url(request, timeout=5, **options):
                pytest.fail("HTTP error accepted")
        with caught.value as response:
            assert response.code == int(case)
            assert response.read() == b"e" * 65536
    elif case.startswith("duplicate-type"):
        with pytest.raises(OSError, match="ambiguous Content-Type"):
            with upstream_http.open_authenticated_url(request, timeout=5, **options):
                pytest.fail("ambiguous Content-Type accepted")
    elif case in ("limit", "wrong-type", "oversized-header", "oversized-receipt"):
        with pytest.raises(OSError, match=("not MP3" if case == "wrong-type" else "exceeded")):
            with upstream_http.open_authenticated_url(request, timeout=5, **options):
                pytest.fail("invalid body accepted")
    else:
        with upstream_http.open_authenticated_url(request, timeout=5, **options) as response:
            assert response.read() == (b"x" * (9 * 1024 * 1024) if case == "media" else b"response")
            if case == "parameterized-type":
                assert response.headers["Content-Type"] == 'audio/mpeg; name="speech.mp3"'
                assert response.headers.get_content_type() == "audio/mpeg"
            if case == "missing-type":
                assert "Content-Type" not in response.headers
            if case == "irrelevant-headers":
                assert response.headers.get_content_type() == "audio/mpeg"
                assert response.headers["Content-Length"] == "8"
                assert "X-Synthetic-0" not in response.headers
