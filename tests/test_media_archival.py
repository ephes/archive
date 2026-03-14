from __future__ import annotations

import subprocess

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages

from archive.media_archival import MediaArchivalError, archive_item_audio, can_archive_audio
from archive.models import Item, ItemKind


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        content_type: str = "audio/mpeg",
        final_url: str = "https://cdn.example.com/audio.mp3",
    ) -> None:
        self._chunks = list(chunks)
        self.headers = _FakeHeaders(content_type)
        self._final_url = final_url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _TrackingSpool:
    instances: list[_TrackingSpool] = []

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        _TrackingSpool.instances.append(self)

    def write(self, _chunk: bytes) -> None:
        return None

    def seek(self, _position: int) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _fake_ffmpeg_run(command, check, capture_output, text, timeout) -> None:
    output_path = command[-1]
    with open(output_path, "wb") as output_file:
        output_file.write(b"ID3-derived-audio")


def _failing_ffmpeg_run(command, check, capture_output, text, timeout) -> None:
    raise subprocess.CalledProcessError(1, command, stderr="ffmpeg exploded")


def _delete_storage_objects(*paths: str) -> None:
    for path in paths:
        if storages["archive_media"].exists(path):
            storages["archive_media"].delete(path)


@pytest.mark.django_db
def test_can_archive_audio_accepts_direct_video_media_url() -> None:
    item = Item(
        original_url="https://example.com/watch/demo",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )

    assert can_archive_audio(item) is True


@pytest.mark.django_db
def test_can_archive_audio_accepts_explicit_audio_webm_url() -> None:
    item = Item(
        original_url="https://example.com/listen",
        audio_url="https://cdn.example.com/audio.webm",
        kind=ItemKind.PODCAST_EPISODE,
    )

    assert can_archive_audio(item) is True


@pytest.mark.django_db
def test_archive_item_audio_closes_spool_when_download_exceeds_size_limit(
    monkeypatch,
    settings,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 6
    _TrackingSpool.instances.clear()
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda request, timeout: _FakeResponse([b"1234", b"5678"]),
    )
    monkeypatch.setattr("archive.media_archival.SpooledTemporaryFile", _TrackingSpool)

    with pytest.raises(MediaArchivalError, match="archive limit"):
        archive_item_audio(item=item)

    assert len(_TrackingSpool.instances) == 1
    assert _TrackingSpool.instances[0].closed is True


@pytest.mark.django_db
def test_archive_item_audio_rejects_video_downloads_over_size_limit(
    monkeypatch,
    settings,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.mp4",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 6
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda request, timeout: _FakeResponse(
            [b"1234", b"5678"],
            content_type="video/mp4",
            final_url="https://cdn.example.com/video.mp4",
        ),
    )

    with pytest.raises(MediaArchivalError, match="Video source exceeded"):
        archive_item_audio(item=item)

    assert storages["archive_media"].exists(f"items/{item.pk}/video/source.mp4") is False
    assert storages["archive_media"].exists(f"items/{item.pk}/audio/extracted.mp3") is False


@pytest.mark.django_db
def test_archive_item_audio_extracts_audio_from_direct_video_and_tracks_source(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.mp4",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda request, timeout: _FakeResponse(
            [b"video-bytes-1", b"video-bytes-2"],
            content_type="video/mp4",
            final_url="https://cdn.example.com/video.mp4",
        ),
    )
    monkeypatch.setattr("archive.media_archival.subprocess.run", _fake_ffmpeg_run)

    archived_audio = archive_item_audio(item=item)

    assert archived_audio.object_name == f"items/{item.pk}/audio/extracted.mp3"
    assert archived_audio.content_type == "audio/mpeg"
    assert archived_audio.size_bytes == len(b"ID3-derived-audio")
    assert archived_audio.source_object_name == f"items/{item.pk}/video/source.mp4"
    assert archived_audio.source_content_type == "video/mp4"
    assert archived_audio.source_size_bytes == len(b"video-bytes-1video-bytes-2")
    assert storages["archive_media"].exists(archived_audio.object_name) is True
    assert storages["archive_media"].exists(archived_audio.source_object_name) is True


@pytest.mark.django_db
def test_archive_item_audio_prefers_video_path_for_webm_video_urls(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.webm",
        media_url="https://cdn.example.com/video.webm",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.webm",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda request, timeout: _FakeResponse(
            [b"video-webm"],
            content_type="video/webm",
            final_url="https://cdn.example.com/video.webm",
        ),
    )
    monkeypatch.setattr("archive.media_archival.subprocess.run", _fake_ffmpeg_run)

    archived_audio = archive_item_audio(item=item)

    assert archived_audio.object_name == f"items/{item.pk}/audio/extracted.mp3"
    assert archived_audio.source_object_name == f"items/{item.pk}/video/source.webm"


@pytest.mark.django_db
def test_archive_item_audio_cleans_up_stale_archived_video_when_switching_to_direct_audio(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        audio_url="https://cdn.example.com/audio.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_video_path="items/1/video/source.mp4",
    )
    storages["archive_media"].save(item.archived_video_path, ContentFile(b"old-video"))
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda request, timeout: _FakeResponse(
            [b"fresh-audio"],
            content_type="audio/mpeg",
            final_url="https://cdn.example.com/audio.mp3",
        ),
    )

    archived_audio = archive_item_audio(item=item)

    assert archived_audio.object_name == f"items/{item.pk}/audio/source.mp3"
    assert storages["archive_media"].exists(item.archived_video_path) is False


@pytest.mark.django_db
def test_archive_item_audio_cleans_up_failed_video_archival_before_persisting_objects(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.mp4",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda request, timeout: _FakeResponse(
            [b"video-bytes"],
            content_type="video/mp4",
            final_url="https://cdn.example.com/video.mp4",
        ),
    )
    monkeypatch.setattr("archive.media_archival.subprocess.run", _failing_ffmpeg_run)

    with pytest.raises(MediaArchivalError, match="Video audio extraction failed"):
        archive_item_audio(item=item)

    assert storages["archive_media"].exists(f"items/{item.pk}/video/source.mp4") is False
    assert storages["archive_media"].exists(f"items/{item.pk}/audio/extracted.mp3") is False
