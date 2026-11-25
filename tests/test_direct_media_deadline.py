from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request

import pytest
from django.core.files.storage import storages

from archive import media_archival
from archive.media_archival import MediaArchivalError, archive_item_audio
from archive.media_storage import iter_archive_media_object_names
from archive.models import Item, ItemKind


@pytest.fixture
def media_server(monkeypatch):
    # The test server listens on loopback, which the fetch guard refuses by default.
    monkeypatch.setenv("ARCHIVE_FETCH_PRIVATE_HOSTS", "127.0.0.1")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            try:
                if self.path.startswith("/missing"):
                    self.send_error(404)
                    return
                if self.path.startswith("/redirect"):
                    time.sleep(0.4)
                    self.send_response(302)
                    self.send_header("Location", "/redirect.mp3")
                    self.end_headers()
                    return
                if self.path.startswith("/headers"):
                    self.wfile.write(b"HTTP/1.0 200 OK\r\n")
                    for _ in range(20):
                        self.wfile.write(b"X-Test: trickle\r\n")
                        self.wfile.flush()
                        time.sleep(0.15)
                    self.wfile.write(b"Content-Type: audio/mpeg\r\n\r\n")
                    self.wfile.write(b"audio")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.end_headers()
                if self.path.startswith("/fast"):
                    self.wfile.write(b"fast audio")
                else:
                    for _ in range(20):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.15)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(autouse=True)
def isolated_storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "path,kind",
    [
        ("/trickle.mp3", ItemKind.PODCAST_EPISODE),
        ("/trickle.mp4", ItemKind.VIDEO),
        ("/redirect.mp3", ItemKind.PODCAST_EPISODE),
        ("/headers.mp3", ItemKind.PODCAST_EPISODE),
    ],
)
def test_direct_download_has_total_deadline(media_server, path, kind, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "archive.media_archival.TemporaryDirectory",
        partial(tempfile.TemporaryDirectory, dir=tmp_path),
    )
    item = Item.objects.create(original_url=media_server + path, kind=kind)
    started = time.monotonic()
    with pytest.raises(MediaArchivalError, match="elapsed download budget"):
        archive_item_audio(item, timeout=1)
    assert time.monotonic() - started < 1.8
    assert not list(tmp_path.glob("archive-*"))
    assert not list(iter_archive_media_object_names())


@pytest.mark.django_db(transaction=True)
def test_fast_direct_download_is_archived(media_server):
    item = Item.objects.create(
        original_url=media_server + "/fast.mp3", kind=ItemKind.PODCAST_EPISODE
    )
    audio = archive_item_audio(item, timeout=2)
    with storages["archive_media"].open(audio.object_name) as source:
        assert source.read() == b"fast audio"


@pytest.mark.django_db(transaction=True)
def test_real_download_byte_limit_still_applies(media_server, settings):
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 3
    item = Item.objects.create(
        original_url=media_server + "/fast.mp3", kind=ItemKind.PODCAST_EPISODE
    )
    with pytest.raises(MediaArchivalError, match="3-byte archive limit"):
        archive_item_audio(item, timeout=2)
    assert not list(iter_archive_media_object_names())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("returncode,stdout", [(-9, ""), (1, "{}"), (0, "[]"), (0, "{}")])
def test_child_crash_reports_exit_code_and_diagnostic(monkeypatch, returncode, stdout):
    monkeypatch.setattr(
        "archive.media_archival.run_download",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr="traceback\nchild failed\n"
        ),
    )
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    with pytest.raises(MediaArchivalError, match=f"code {returncode}.*child failed"):
        archive_item_audio(item, timeout=2)
    assert not list(iter_archive_media_object_names())


@pytest.mark.django_db(transaction=True)
def test_http_failure_is_an_archival_error(media_server):
    item = Item.objects.create(
        original_url=media_server + "/missing.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    with pytest.raises(MediaArchivalError, match="HTTP Error 404"):
        archive_item_audio(item, timeout=2)
    assert not list(iter_archive_media_object_names())


@pytest.mark.django_db(transaction=True)
def test_direct_download_refuses_loopback_source_by_default(media_server, monkeypatch):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS")
    item = Item.objects.create(
        original_url=media_server + "/fast.mp3", kind=ItemKind.PODCAST_EPISODE
    )
    with pytest.raises(MediaArchivalError, match="non-public network address"):
        archive_item_audio(item, timeout=2)
    assert not list(iter_archive_media_object_names())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("path", ["/trickle.mp3", "/headers.mp3", "/redirect.mp3"])
def test_remote_transcription_source_obeys_pipeline_deadline(
    media_server, path, monkeypatch, settings, tmp_path
):
    from archive.transcriptions import TranscriptionGenerationError, generate_item_transcript

    monkeypatch.setattr(
        "archive.media_archival.TemporaryDirectory",
        partial(tempfile.TemporaryDirectory, dir=tmp_path),
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    monkeypatch.setattr(
        "archive.transcriptions._request_transcription",
        lambda **kwargs: pytest.fail("a timed-out download must not reach transcription"),
    )
    item = Item.objects.create(
        original_url=media_server + "/page",
        audio_url=media_server + path,
        kind=ItemKind.PODCAST_EPISODE,
    )
    started = time.monotonic()
    with pytest.raises(TranscriptionGenerationError, match="elapsed download budget"):
        generate_item_transcript(item, timeout=1)
    assert time.monotonic() - started < 1.8
    assert not list(tmp_path.glob("archive-*"))


@pytest.mark.django_db(transaction=True)
def test_remote_transcription_source_size_limit_uses_transcription_message(
    media_server, monkeypatch, settings
):
    from archive import transcriptions as tr

    monkeypatch.setattr(tr, "MAX_TRANSCRIPTION_BYTES", 3)
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    item = Item.objects.create(
        original_url=media_server + "/page",
        audio_url=media_server + "/fast.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    with pytest.raises(tr.TranscriptionGenerationError, match="transcription limit"):
        tr.generate_item_transcript(item, timeout=2)


@pytest.mark.django_db(transaction=True)
def test_fast_remote_transcription_source_is_loaded(media_server, monkeypatch, settings):
    from archive import transcriptions as tr

    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    seen = []
    monkeypatch.setattr(
        tr, "_request_transcription", lambda **kwargs: seen.append(kwargs["media"]) or "words"
    )
    item = Item.objects.create(
        original_url=media_server + "/page",
        audio_url=media_server + "/fast.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    assert tr.generate_item_transcript(item, timeout=2) == "words"
    assert seen[0].payload == b"fast audio"
    assert seen[0].content_type == "audio/mpeg"


@pytest.mark.django_db(transaction=True)
def test_remote_transcription_failure_keeps_fixed_diagnostic(media_server, settings):
    from archive.transcriptions import TranscriptionGenerationError, generate_item_transcript

    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    item = Item.objects.create(
        original_url=media_server + "/page",
        audio_url=media_server + "/missing.mp3?signature=secret",
        kind=ItemKind.PODCAST_EPISODE,
    )
    with pytest.raises(TranscriptionGenerationError) as caught:
        generate_item_transcript(item, timeout=2)
    assert str(caught.value) == "Media download failed: HTTP 404"


def test_direct_media_response_close_failure_keeps_one_receipt_and_removes_stage(monkeypatch):
    script = """
import runpy, sys
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
main = runpy.run_path(sys.argv[1])['main']

class Response(BytesIO):
    headers = Message()
    headers['Content-Type'] = 'audio/mpeg'
    headers['Content-Length'] = '5'
    def getcode(self): return 200
    def geturl(self): return 'https://example.test/audio.mp3'
    def __exit__(self, *args):
        super().__exit__(*args)
        raise OSError('synthetic response close fault ' + 'x' * 10000)

main.__globals__['_load_safe_fetch'] = lambda: SimpleNamespace(
    open_public_url=lambda *args, **kwargs: Response(b'audio')
)
raise SystemExit(main())
"""
    staged = []

    def run(command, **kwargs):
        assert command[-1].endswith("direct_media_download.py")
        path = Path(json.loads(kwargs["input"])["path"])
        staged.append(path)
        result = subprocess.run([sys.executable, "-I", "-c", script, command[-1]], **kwargs)
        assert path.read_bytes() == b"audio"
        assert result.returncode == 1
        receipt = json.loads(result.stdout)
        assert receipt["safe_error"] == "transport error (OSError)"
        assert len(result.stdout) < 1024
        return result

    monkeypatch.setattr(media_archival, "run_download", run)
    with pytest.raises(
        MediaArchivalError, match="^Media download failed: synthetic response close"
    ):
        with media_archival._open_direct_media(
            Request("https://example.test/audio.mp3"), timeout=5, max_bytes=32
        ):
            pytest.fail("download published despite a failed response close")
    assert len(staged) == 1
    assert not staged[0].parent.exists()
