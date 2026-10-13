import io

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.urls import reverse

from archive.media_responses import UnsatisfiableRange, parse_single_byte_range
from archive.models import Item, ItemKind

PAYLOAD = b"ID3-audio"  # 9 bytes


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-1", (0, 1)),
        ("bytes=0-0", (0, 0)),
        ("bytes=3-", (3, 8)),
        ("bytes=-4", (5, 8)),
        ("bytes=-100", (0, 8)),
        ("bytes=2-100", (2, 8)),
        ("bytes=8-8", (8, 8)),
        ("bytes=000000000000000002-3", (2, 3)),
    ],
)
def test_parse_single_byte_range_returns_inclusive_positions(header, expected) -> None:
    assert parse_single_byte_range(header, 9) == expected


@pytest.mark.parametrize(
    "header",
    [
        "",
        "bytes=-",
        "bytes=5-2",
        "bytes=0-1,3-4",
        "bytes= 0-1",
        "bytes=0-1 ",
        "bytes=0 - 1",
        "items=0-1",
        "bytes=a-b",
        "bytes=+1-2",
        "bytes=0-1\n",
        "bytes=" + "1" * 19 + "-",
        "bytes=-" + "1" * 19,
        "bytes=٠-١",
    ],
)
def test_parse_single_byte_range_ignores_everything_else(header) -> None:
    assert parse_single_byte_range(header, 9) is None


@pytest.mark.parametrize(
    ("header", "size"),
    [("bytes=9-", 9), ("bytes=10-12", 9), ("bytes=-0", 9), ("bytes=-1", 0), ("bytes=0-", 0)],
)
def test_parse_single_byte_range_rejects_ranges_outside_the_file(header, size) -> None:
    with pytest.raises(UnsatisfiableRange):
        parse_single_byte_range(header, size)


class TrackedFile(io.BytesIO):
    seek_error: Exception | None = None

    def seek(self, *args):
        if self.seek_error is not None:
            raise self.seek_error
        return super().seek(*args)


@pytest.fixture
def archived_audio(db, monkeypatch):
    """A public item with archived audio whose opened file handle is observable."""

    def create(*, size_bytes: int = len(PAYLOAD)):
        item = Item.objects.create(
            original_url="https://example.com/episode",
            title="Archived episode",
            kind=ItemKind.PODCAST_EPISODE,
            archived_audio_path="items/1/audio/source.mp3",
            archived_audio_content_type="audio/mpeg",
            archived_audio_size_bytes=size_bytes,
        )
        handle = TrackedFile(PAYLOAD)
        monkeypatch.setattr("archive.views.open_archived_audio", lambda item: handle)
        return reverse("archive:item-archived-audio", kwargs={"pk": item.pk}), handle

    return create


def test_archived_audio_without_range_is_served_whole_and_offers_ranges(
    client, archived_audio
) -> None:
    url, handle = archived_audio()

    response = client.get(url)

    assert response.status_code == 200
    assert response["Content-Type"] == "audio/mpeg"
    assert response["Content-Length"] == "9"
    assert response["Accept-Ranges"] == "bytes"
    assert response["Cache-Control"] == "public, max-age=3600"
    assert "Content-Range" not in response
    assert b"".join(response.streaming_content) == PAYLOAD
    response.close()
    assert handle.closed


@pytest.mark.parametrize(
    ("header", "content_range", "body"),
    [
        ("bytes=0-1", "bytes 0-1/9", b"ID"),
        ("bytes=3-", "bytes 3-8/9", b"-audio"),
        ("bytes=-4", "bytes 5-8/9", b"udio"),
        ("bytes=2-100", "bytes 2-8/9", b"3-audio"),
        ("bytes=0-8", "bytes 0-8/9", PAYLOAD),
    ],
)
def test_archived_audio_answers_a_single_range_with_206(
    client, archived_audio, header, content_range, body
) -> None:
    url, handle = archived_audio()

    response = client.get(url, headers={"Range": header})

    assert response.status_code == 206
    assert response["Content-Type"] == "audio/mpeg"
    assert response["Content-Range"] == content_range
    assert response["Content-Length"] == str(len(body))
    assert response["Accept-Ranges"] == "bytes"
    assert response["Cache-Control"] == "public, max-age=3600"
    assert b"".join(response.streaming_content) == body
    response.close()
    assert handle.closed


def test_archived_audio_range_response_closes_the_file_when_it_is_never_read(
    client, archived_audio
) -> None:
    url, handle = archived_audio()

    response = client.get(url, headers={"Range": "bytes=0-1"})
    response.close()

    assert handle.closed


@pytest.mark.parametrize("header", ["bytes=9-", "bytes=100-200", "bytes=-0"])
def test_archived_audio_answers_an_unsatisfiable_range_with_416(
    client, archived_audio, header
) -> None:
    url, handle = archived_audio()

    response = client.get(url, headers={"Range": header})

    assert response.status_code == 416
    assert response["Content-Range"] == "bytes */9"
    assert response["Accept-Ranges"] == "bytes"
    assert "Cache-Control" not in response
    assert response.content == b""
    assert handle.closed


@pytest.mark.parametrize(
    "headers",
    [
        {"Range": "bytes=0-1,3-4"},
        {"Range": "bytes=5-2"},
        {"Range": "items=0-1"},
        {"Range": "bytes=0-1", "If-Range": '"some-etag"'},
        {"Range": "bytes=9-", "If-Range": "Wed, 21 Oct 2015 07:28:00 GMT"},
    ],
)
def test_archived_audio_serves_the_whole_file_for_requests_it_does_not_honour(
    client, archived_audio, headers
) -> None:
    url, _ = archived_audio()

    response = client.get(url, headers=headers)

    assert response.status_code == 200
    assert response["Content-Length"] == "9"
    assert "Content-Range" not in response
    assert b"".join(response.streaming_content) == PAYLOAD


def test_archived_audio_without_recorded_size_does_not_offer_ranges(client, archived_audio) -> None:
    url, _ = archived_audio(size_bytes=0)

    response = client.get(url, headers={"Range": "bytes=0-1"})

    assert response.status_code == 200
    assert "Accept-Ranges" not in response
    assert "Content-Range" not in response
    assert b"".join(response.streaming_content) == PAYLOAD


def test_archived_audio_reports_a_storage_failure_before_sending_a_range(
    client, archived_audio
) -> None:
    url, handle = archived_audio()
    handle.seek_error = RuntimeError("storage backend failed")

    response = client.get(url, headers={"Range": "bytes=2-"})

    assert response.status_code == 502
    assert "Content-Range" not in response
    assert handle.closed


def test_archived_audio_range_is_read_from_the_archive_media_storage(
    client, db, settings, tmp_path
) -> None:
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path)},
        },
    }
    payload = bytes(range(256)) * 64
    path = storages["archive_media"].save("items/1/audio/source.mp3", ContentFile(payload))
    item = Item.objects.create(
        original_url="https://example.com/episode",
        title="Archived episode",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path=path,
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=len(payload),
    )

    response = client.get(
        reverse("archive:item-archived-audio", kwargs={"pk": item.pk}),
        headers={"Range": "bytes=5000-12999"},
    )

    assert response.status_code == 206
    assert response["Content-Range"] == f"bytes 5000-12999/{len(payload)}"
    assert b"".join(response.streaming_content) == payload[5000:13000]


def test_archived_audio_still_rejects_other_methods(client, archived_audio) -> None:
    url, _ = archived_audio()

    assert client.head(url).status_code == 200
    assert client.post(url).status_code == 405


@pytest.mark.parametrize(
    "headers,code,length", [({}, 200, "9"), ({"Range": "bytes=2-4"}, 200, "9")]
)
def test_archived_head_has_get_headers_closes_file_without_reading(
    client, archived_audio, headers, code, length, monkeypatch
):
    url, handle = archived_audio()
    monkeypatch.setattr(handle, "read", lambda *args: pytest.fail("HEAD read the audio body"))
    response = client.head(url, headers=headers)
    assert response.status_code == code
    assert response["Content-Length"] == length
    assert response["Content-Type"] == "audio/mpeg"
    assert response["Accept-Ranges"] == "bytes"
    assert b"".join(response.streaming_content) == b""
    assert handle.closed
