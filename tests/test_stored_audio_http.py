"""Real local HTTP clients exercise both stored-audio WSGI routes."""

from http.client import HTTPConnection
from threading import Event
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.urls import reverse

from archive.models import EnrichmentStatus, Item, ItemKind

PAYLOAD = bytes(range(256)) * 100


@pytest.fixture
def stored_http_audio(live_server, monkeypatch):
    storage = storages["archive_media"]
    path = storage.save("synthetic/audio.mp3", ContentFile(PAYLOAD))
    item = Item.objects.create(
        original_url="https://example.com/synthetic",
        title="Synthetic audio",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/synthetic.mp3",
        article_audio_status=EnrichmentStatus.COMPLETE,
        article_audio_file=path,
        article_audio_size_bytes=len(PAYLOAD),
        article_audio_content_type="audio/mpeg",
        archived_audio_path=path,
        archived_audio_size_bytes=len(PAYLOAD),
        archived_audio_content_type="audio/mpeg",
    )
    state = SimpleNamespace(readers=[], pause=False, paused=Event(), resume=Event())
    original_open = storage._open

    class ObservedFile(File):
        def __init__(self, file):
            super().__init__(file)
            self.reads = 0
            self.closes = 0
            self.closed_event = Event()

        def read(self, size=-1):
            self.reads += 1
            if state.pause and self.reads == 2:
                state.paused.set()
                if not state.resume.wait(10):
                    raise TimeoutError("Test did not release stored reader")
            return super().read(size)

        def close(self):
            self.closes += 1
            try:
                super().close()
            finally:
                self.closed_event.set()

    def open_reader(name, mode="rb"):
        reader = ObservedFile(original_open(name, mode))
        state.readers.append(reader)
        return reader

    monkeypatch.setattr(storage, "_open", open_reader)
    # Existing stored generated audio must not contact the synthesis provider.
    monkeypatch.setattr("archive.article_audio.urlopen", lambda *a, **k: pytest.fail("download"))
    host = urlsplit(live_server.url)
    state.connect = lambda: HTTPConnection(host.hostname, host.port, timeout=5)
    state.path = lambda route: reverse(route, kwargs={"pk": item.pk})
    try:
        yield state
    finally:
        state.resume.set()


@pytest.mark.parametrize("route", ["archive:item-archived-audio", "archive:item-article-audio"])
@pytest.mark.parametrize(
    "method,header,status,start,stop,content_range",
    [
        ("GET", None, 200, 0, len(PAYLOAD), None),
        ("GET", "bytes=4000-9000", 206, 4000, 9001, f"bytes 4000-9000/{len(PAYLOAD)}"),
        (
            "GET",
            "bytes=-5000",
            206,
            len(PAYLOAD) - 5000,
            len(PAYLOAD),
            f"bytes {len(PAYLOAD) - 5000}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}",
        ),
        (
            "GET",
            "bytes=22000-",
            206,
            22000,
            len(PAYLOAD),
            f"bytes 22000-{len(PAYLOAD) - 1}/{len(PAYLOAD)}",
        ),
        ("GET", "bytes=bad", 200, 0, len(PAYLOAD), None),
        ("GET", "bytes=0-1,4-5", 200, 0, len(PAYLOAD), None),
        ("GET", f"bytes={len(PAYLOAD)}-", 416, 0, 0, f"bytes */{len(PAYLOAD)}"),
        ("HEAD", None, 200, 0, len(PAYLOAD), None),
        ("HEAD", "bytes=4000-9000", 200, 0, len(PAYLOAD), None),
    ],
)
def test_stored_audio_real_http_bytes_and_headers(
    stored_http_audio, route, method, header, status, start, stop, content_range
):
    state = stored_http_audio
    connection = state.connect()
    try:
        connection.request(method, state.path(route), headers={"Range": header} if header else {})
        response = connection.getresponse()
        assert response.status == status
        assert response.getheader("Content-Range") == content_range
        assert response.getheader("Accept-Ranges") == "bytes"
        assert response.getheader("Content-Length") == str(stop - start)
        assert response.getheader("Cache-Control") == "public, max-age=3600"
        if status != 416:
            assert response.getheader("Content-Type") == "audio/mpeg"
        assert response.read() == (b"" if method == "HEAD" else PAYLOAD[start:stop])
    finally:
        connection.close()
    assert len(state.readers) == 1
    reader = state.readers[0]
    assert reader.closed_event.wait(5), "Server did not release the storage reader"
    assert reader.closed
    if method == "HEAD":
        assert reader.reads == 0
    else:
        assert reader.closes == 1


@pytest.mark.parametrize("route", ["archive:item-archived-audio", "archive:item-article-audio"])
def test_stored_audio_interrupted_http_client_releases_reader(stored_http_audio, route):
    state = stored_http_audio
    state.pause = True
    connection = state.connect()
    try:
        connection.request("GET", state.path(route))
        response = connection.getresponse()
        assert response.status == 200
        assert response.read(1) == PAYLOAD[:1]
        assert state.paused.wait(5), "Reader did not reach the streaming barrier"
        # The server cannot finish its body before this client disconnects.
        response.close()
        connection.close()
    finally:
        connection.close()
        state.resume.set()
    assert len(state.readers) == 1
    assert state.readers[0].closed_event.wait(5), "Disconnected stream retained its reader"
    assert state.readers[0].closed
    assert state.readers[0].closes == 1
