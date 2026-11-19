"""Lengths and ranges come from validated evidence about the opened stored object."""

from __future__ import annotations

import io
from unittest.mock import Mock

import pytest
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import RequestFactory
from django.urls import reverse
from storages.backends.s3 import S3File

from archive.media_responses import ranged_file_response
from archive.models import EnrichmentStatus, Item, ItemKind

PAYLOAD = b"ID3-audio-payload"  # 17 bytes


@pytest.fixture(autouse=True)
def isolated_storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }


@pytest.fixture
def observed_closes(monkeypatch):
    closes: list[str] = []
    original = File.close

    def close(self):
        closes.append(getattr(self, "name", "") or "")
        return original(self)

    monkeypatch.setattr(File, "close", close)
    return closes


def _item(route: str, stored_bytes: bytes, recorded: int) -> str:
    storage = storages["archive_media"]
    path = storage.save("items/1/audio/source.mp3", ContentFile(stored_bytes))
    fields = (
        {"archived_audio_path": path, "archived_audio_size_bytes": recorded}
        if route == "item-archived-audio"
        else {
            "kind": ItemKind.ARTICLE,
            "article_audio_artifact_path": "/synthetic.mp3",
            "article_audio_status": EnrichmentStatus.COMPLETE,
            "article_audio_file": path,
            "article_audio_size_bytes": recorded,
        }
    )
    item = Item.objects.create(
        original_url="https://example.com/episode",
        title="Stored audio",
        **{"kind": ItemKind.PODCAST_EPISODE, **fields},
    )
    return reverse(f"archive:{route}", kwargs={"pk": item.pk})


ROUTES = ["item-archived-audio", "item-article-audio"]
REQUESTS = [
    pytest.param("get", {}, id="GET"),
    pytest.param("head", {}, id="HEAD"),
    pytest.param("get", {"Range": "bytes=2-5"}, id="range"),
    pytest.param("get", {"Range": "bytes=40-"}, id="range-beyond-recorded"),
]


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("stored", [PAYLOAD[:5], PAYLOAD + b"-appended"], ids=["shorter", "longer"])
@pytest.mark.parametrize(("method", "headers"), REQUESTS)
def test_stored_object_length_disagreeing_with_record_is_refused(
    client, observed_closes, route, stored, method, headers
):
    url = _item(route, stored, recorded=len(PAYLOAD))

    response = getattr(client, method)(url, headers=headers)

    assert response.status_code == 502
    assert response["Cache-Control"] == "no-store"
    assert "Content-Range" not in response
    assert "Accept-Ranges" not in response
    if method == "get":
        assert response.content == b"Audio is temporarily unavailable."
    response.close()
    assert len(observed_closes) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("method", "headers", "status", "body", "length", "content_range"),
    [
        ("get", {}, 200, PAYLOAD, "17", None),
        ("head", {}, 200, b"", "17", None),
        ("get", {"Range": "bytes=2-5"}, 206, PAYLOAD[2:6], "4", "bytes 2-5/17"),
        ("get", {"Range": "bytes=-3"}, 206, PAYLOAD[-3:], "3", "bytes 14-16/17"),
        ("get", {"Range": "bytes=17-"}, 416, b"", None, "bytes */17"),
    ],
)
def test_matching_lengths_keep_validated_responses(
    client, observed_closes, route, method, headers, status, body, length, content_range
):
    url = _item(route, PAYLOAD, recorded=len(PAYLOAD))

    response = getattr(client, method)(url, headers=headers)

    assert response.status_code == status
    if length is not None:
        assert response["Content-Length"] == length
    if content_range is not None:
        assert response["Content-Range"] == content_range
    content = b"".join(response.streaming_content) if response.streaming else response.content
    assert content == body
    response.close()
    assert len(observed_closes) == 1


@pytest.mark.django_db
def test_unknown_recorded_size_keeps_whole_file_behavior(client, observed_closes):
    url = _item("item-archived-audio", PAYLOAD, recorded=0)

    response = client.get(url, headers={"Range": "bytes=2-5"})

    assert response.status_code == 200
    assert "Accept-Ranges" not in response
    assert b"".join(response.streaming_content) == PAYLOAD
    response.close()
    assert len(observed_closes) == 1


@pytest.mark.parametrize("header", [None, "bytes=1-3"])
def test_handles_without_size_evidence_keep_recorded_size(db, header):
    handle = io.BytesIO(PAYLOAD)

    response = ranged_file_response(
        RequestFactory().get("/media", headers={"Range": header} if header else {}),
        file_handle=handle,
        size=len(PAYLOAD),
        content_type="audio/mpeg",
        cache_control="public",
    )

    assert response.status_code == (206 if header else 200)
    expected = PAYLOAD[1:4] if header else PAYLOAD
    assert b"".join(response.streaming_content) == expected
    response.close()
    assert handle.closed


class _SizedHandle(io.BytesIO):
    def __init__(self, payload: bytes, size):
        super().__init__(payload)
        self._size = size
        self.closes = 0

    def fileno(self):
        raise io.UnsupportedOperation("no descriptor")

    @property
    def size(self):
        if isinstance(self._size, Exception):
            raise self._size
        return self._size

    def close(self):
        self.closes += 1
        super().close()


def test_backend_size_attribute_is_used_as_evidence(db):
    handle = _SizedHandle(PAYLOAD, size=len(PAYLOAD) - 1)

    response = ranged_file_response(
        RequestFactory().get("/media", headers={"Range": "bytes=0-1"}),
        file_handle=handle,
        size=len(PAYLOAD),
        content_type="audio/mpeg",
        cache_control="public, max-age=3600",
    )

    assert response.status_code == 502
    assert response["Cache-Control"] == "no-store"
    assert handle.closes == 1


@pytest.mark.parametrize("size", [None, "17", True, -1, OSError("backend down")])
def test_unusable_size_evidence_keeps_documented_behavior(db, size):
    handle = _SizedHandle(PAYLOAD, size=size)

    response = ranged_file_response(
        RequestFactory().get("/media"),
        file_handle=handle,
        size=len(PAYLOAD),
        content_type="audio/mpeg",
        cache_control="public",
    )

    assert response.status_code == 200
    assert response["Content-Length"] == str(len(PAYLOAD))
    assert b"".join(response.streaming_content) == PAYLOAD
    response.close()
    assert handle.closes == 1


@pytest.mark.django_db
@pytest.mark.parametrize("reported_size", [len(PAYLOAD), len(PAYLOAD) - 1])
@pytest.mark.parametrize("conditional", [False, True], ids=["HEAD", "conditional-GET"])
def test_lazy_s3_file_validates_head_and_revalidation_without_spooling(
    client, monkeypatch, reported_size, conditional
):
    url = _item("item-article-audio", PAYLOAD, recorded=len(PAYLOAD))
    etag = client.head(url)["ETag"]
    storage = Mock(location="", max_memory_size=0, gzip=False)
    storage.get_object_parameters.return_value = {}
    obj = storage.bucket.Object.return_value
    obj.content_length = reported_size
    obj.download_fileobj.side_effect = lambda file, **_: file.write(PAYLOAD)
    handle = S3File("synthetic.mp3", "rb", storage, buffer_size=1)
    close = Mock(wraps=handle.close)
    monkeypatch.setattr(handle, "close", close)
    monkeypatch.setattr(
        Item._meta.get_field("article_audio_file").storage, "open", lambda *a, **kw: handle
    )

    response = client.get(url, headers={"If-None-Match": etag}) if conditional else client.head(url)

    obj.download_fileobj.assert_not_called()
    if reported_size != len(PAYLOAD):
        assert response.status_code == 502
        assert response["Cache-Control"] == "no-store"
    else:
        assert response.status_code == (304 if conditional else 200)
        assert response["ETag"] == etag
        if not conditional:
            assert response["Content-Length"] == str(len(PAYLOAD))
            assert response["Accept-Ranges"] == "bytes"
            assert b"".join(response.streaming_content) == b""
    response.close()
    close.assert_called_once_with()
