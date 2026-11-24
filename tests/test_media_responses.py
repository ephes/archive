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
    assert response["Cache-Control"] == "public, no-cache"
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
    assert response["Cache-Control"] == "public, no-cache"
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
    assert response["Cache-Control"] == "public, no-cache"
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


@pytest.mark.parametrize("header", [None, "bytes=2-4"])
@pytest.mark.parametrize("fault", ["constructor", "inference", "headers"])
@pytest.mark.parametrize("close_error", [False, True])
def test_response_construction_failure_closes_owned_handle_once(
    monkeypatch, caplog, header, fault, close_error
):
    from django.http import FileResponse
    from django.test import RequestFactory

    from archive import media_responses

    failure = RuntimeError("response setup failed")

    class OwnedFile(io.BytesIO):
        closes = 0

        def close(self):
            self.closes += 1
            super().close()
            if close_error:
                raise RuntimeError("secondary close failure")

    handle = OwnedFile(PAYLOAD)

    def fail(*args, **kwargs):
        raise failure

    if fault == "constructor":
        monkeypatch.setattr(media_responses, "FileResponse", fail)
    elif fault == "inference":
        monkeypatch.setattr(FileResponse, "set_headers", fail)
    else:
        original_setitem = FileResponse.__setitem__

        def fail_headers(self, name, value):
            if name == "Cache-Control":
                raise failure
            original_setitem(self, name, value)

        monkeypatch.setattr(FileResponse, "__setitem__", fail_headers)
    request = RequestFactory().get("/media", headers={"Range": header} if header else {})
    with pytest.raises(RuntimeError) as caught:
        media_responses.ranged_file_response(
            request,
            file_handle=handle,
            size=len(PAYLOAD),
            content_type="audio/mpeg",
            cache_control="public, max-age=3600",
        )
    assert caught.value is failure
    assert handle.closed
    assert handle.closes == 1
    if close_error:
        assert "response preparation failed (RuntimeError)" in caplog.text
        assert "secondary close failure" not in caplog.text


@pytest.mark.parametrize("fault", ["seek", "tell"])
def test_response_file_probe_failure_releases_handle(monkeypatch, fault):
    from django.test import RequestFactory

    from archive.media_responses import ranged_file_response

    handle = TrackedFile(PAYLOAD)
    failure = RuntimeError("file probe failed")
    closes = []
    original_close = handle.close

    def close():
        closes.append(True)
        original_close()

    def fail(*args):
        raise failure

    monkeypatch.setattr(handle, "close", close)
    monkeypatch.setattr(handle, fault, fail)
    if fault == "seek":
        response = ranged_file_response(
            RequestFactory().get("/media", headers={"Range": "bytes=2-4"}),
            file_handle=handle,
            size=9,
            content_type="audio/mpeg",
            cache_control="public",
        )
        assert response.status_code == 502
    else:
        with pytest.raises(RuntimeError) as caught:
            ranged_file_response(
                RequestFactory().get("/media"),
                file_handle=handle,
                size=0,  # For an unknown length, FileResponse still probes the original handle.
                content_type="audio/mpeg",
                cache_control="public",
            )
        assert caught.value is failure
    assert handle.closed
    assert closes == [True]


@pytest.mark.parametrize("header", [None, "bytes=2-4"])
def test_successful_response_transfers_file_until_response_close(db, monkeypatch, header):
    from django.test import RequestFactory

    from archive.media_responses import ranged_file_response

    handle = TrackedFile(PAYLOAD)
    closes = []
    original_close = handle.close

    def close():
        closes.append(True)
        original_close()

    monkeypatch.setattr(handle, "close", close)
    response = ranged_file_response(
        RequestFactory().get("/media", headers={"Range": header} if header else {}),
        file_handle=handle,
        size=9,
        content_type="audio/mpeg",
        cache_control="public",
    )
    assert not handle.closed
    assert b"".join(response.streaming_content) == (PAYLOAD[2:5] if header else PAYLOAD)
    assert closes == []
    response.close()
    response.close()
    assert closes == [True]


@pytest.mark.parametrize("cache_control", ["private, no-store", "public, max-age=3600"])
@pytest.mark.parametrize("outcome", [200, 206, 416, 502])
def test_range_outcomes_keep_cache_policy_except_transient_errors_and_close_once(
    db, cache_control, outcome
):
    from django.test import RequestFactory

    from archive.media_responses import ranged_file_response

    class OwnedFile(TrackedFile):
        closes = 0

        def close(self):
            self.closes += 1
            super().close()

    handle = OwnedFile(PAYLOAD)
    if outcome == 502:
        handle.seek_error = OSError("synthetic seek failure")
    headers = {} if outcome == 200 else {"Range": "bytes=2-4"}
    if outcome == 416:
        headers = {"Range": "bytes=9-"}
    response = ranged_file_response(
        RequestFactory().get("/media", headers=headers),
        file_handle=handle,
        size=len(PAYLOAD),
        content_type="audio/mpeg",
        cache_control=cache_control,
    )
    assert response.status_code == outcome
    assert response["Cache-Control"] == ("no-store" if outcome == 502 else cache_control)
    if outcome in (200, 206):
        assert handle.closes == 0
        assert b"".join(response.streaming_content) == (PAYLOAD if outcome == 200 else b"3-a")
    else:
        assert handle.closes == 1
    if outcome == 416:
        assert response["Content-Range"] == "bytes */9"
    response.close()
    response.close()
    assert handle.closed
    assert handle.closes == 1


@pytest.mark.django_db
@pytest.mark.parametrize("route", ["item-archived-audio", "item-article-audio"])
def test_transient_range_failure_is_not_cached_and_retry_recovers(client, monkeypatch, route):
    class OwnedFile(TrackedFile):
        closes = 0

        def close(self):
            self.closes += 1
            super().close()

    item = Item.objects.create(
        original_url="https://example.com/synthetic-retry",
        kind=ItemKind.ARTICLE,
        archived_audio_path="items/retry/source.mp3",
        archived_audio_size_bytes=len(PAYLOAD),
        article_audio_artifact_path="/synthetic.mp3",
        article_audio_file="article-audio/retry.mp3",
        article_audio_size_bytes=len(PAYLOAD),
    )
    handles = []

    def open_audio(*args, **kwargs):
        handle = OwnedFile(PAYLOAD)
        if not handles:
            handle.seek_error = OSError("synthetic transient storage failure")
        handles.append(handle)
        return handle

    monkeypatch.setattr("archive.views.open_archived_audio", open_audio)
    monkeypatch.setattr("archive.views.store_generated_article_audio", lambda **kw: None)
    monkeypatch.setattr(storages["archive_media"], "open", open_audio)
    url = reverse(f"archive:{route}", args=[item.pk])
    failed = client.get(url, headers={"Range": "bytes=2-4"})
    assert failed.status_code == 502
    assert failed["Cache-Control"] == "no-store"
    failed.close()
    assert handles[0].closes == 1
    recovered = client.get(url, headers={"Range": "bytes=2-4"})
    assert recovered.status_code == 206
    assert recovered["Cache-Control"] == "public, no-cache"
    assert b"".join(recovered.streaming_content) == PAYLOAD[2:5]
    recovered.close()
    assert handles[1].closes == 1


def test_range_seek_diagnostic_omits_backend_secrets(db, caplog):
    from django.test import RequestFactory

    from archive.media_responses import ranged_file_response

    class OwnedFile(TrackedFile):
        closes = 0

        def close(self):
            self.closes += 1
            super().close()

    handle = OwnedFile(PAYLOAD)
    handle.seek_error = RuntimeError(
        "https://signed.example.invalid/private?credential=CANARY_TOKEN backend=CANARY_CONNECTION"
    )
    response = ranged_file_response(
        RequestFactory().get("/media", headers={"Range": "bytes=2-4"}),
        file_handle=handle,
        size=len(PAYLOAD),
        content_type="audio/mpeg",
        cache_control="public, max-age=3600",
    )
    assert response.status_code == 502
    response.close()
    assert handle.closed and handle.closes == 1
    records = [record for record in caplog.records if record.name == "archive.media_responses"]
    assert len(records) == 1
    assert "stored_media_range_seek_failed" in records[0].getMessage()
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    for canary in ("CANARY_TOKEN", "CANARY_CONNECTION", "signed.example.invalid"):
        assert canary not in caplog.text
        assert canary.encode() not in response.content
