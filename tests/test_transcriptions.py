from __future__ import annotations

import json

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages

from archive.models import Item, ItemKind
from archive.transcriptions import (
    MAX_TRANSCRIPTION_BYTES,
    TranscriptionGenerationError,
    can_transcribe_item,
    generate_item_transcript,
)


class _FakeHeaders:
    def __init__(self, content_type: str, charset: str = "utf-8") -> None:
        self._content_type = content_type
        self._charset = charset

    def get_content_type(self) -> str:
        return self._content_type

    def get_content_charset(self) -> str:
        return self._charset


class _FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_type: str,
        url: str,
    ) -> None:
        self._payload = payload
        self.headers = _FakeHeaders(content_type=content_type)
        self._url = url

    def read(self, _size: int | None = None) -> bytes:
        return self._payload

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _delete_archived_object(path: str) -> None:
    if path and storages["archive_media"].exists(path):
        storages["archive_media"].delete(path)


@pytest.mark.django_db
def test_can_transcribe_item_detects_media_sources() -> None:
    item = Item(
        original_url="https://example.com/article",
        kind=ItemKind.LINK,
        archived_audio_path="items/1/audio/source.mp3",
    )

    assert can_transcribe_item(item) is True


@pytest.mark.django_db
def test_generate_item_transcript_downloads_media_and_parses_response(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        title="Example episode",
        source="Example Radio",
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.openai.com/v1"
    settings.ARCHIVE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

    def fake_urlopen(request, timeout):
        if request.full_url == "https://cdn.example.com/episode.mp3":
            return _FakeResponse(
                b"audio-bytes",
                content_type="audio/mpeg",
                url="https://cdn.example.com/episode.mp3",
            )

        assert request.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert b'name="model"' in request.data
        assert b"gpt-4o-mini-transcribe" in request.data
        assert b'name="prompt"' in request.data
        return _FakeResponse(
            json.dumps({"text": " Hello  \n\n world "}).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    transcript = generate_item_transcript(item=item)

    assert transcript == "Hello\n\nworld"


@pytest.mark.django_db
def test_generate_item_transcript_rejects_oversized_media(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"

    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        lambda request, timeout: _FakeResponse(
            b"x" * (MAX_TRANSCRIPTION_BYTES + 1),
            content_type="audio/mpeg",
            url=request.full_url,
        ),
    )

    with pytest.raises(TranscriptionGenerationError, match="25 MiB"):
        generate_item_transcript(item=item)


@pytest.mark.django_db
def test_generate_item_transcript_prefers_archived_audio_over_remote_urls(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=len(b"archived-audio"),
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.openai.com/v1"
    settings.ARCHIVE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
    _delete_archived_object(item.archived_audio_path)
    storages["archive_media"].save(item.archived_audio_path, ContentFile(b"archived-audio"))

    def fake_urlopen(request, timeout):
        assert request.full_url != "https://cdn.example.com/episode.mp3"
        assert request.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert b"archived-audio" in request.data
        return _FakeResponse(
            json.dumps({"text": "Archived transcript"}).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    transcript = generate_item_transcript(item=item)

    assert transcript == "Archived transcript"


@pytest.mark.django_db
def test_generate_item_transcript_falls_back_to_archived_video_when_audio_is_missing(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
        archived_video_path="items/1/video/source.mp4",
        archived_video_content_type="video/mp4",
        archived_video_size_bytes=len(b"archived-video"),
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.openai.com/v1"
    settings.ARCHIVE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
    _delete_archived_object(item.archived_video_path)
    storages["archive_media"].save(item.archived_video_path, ContentFile(b"archived-video"))

    def fake_urlopen(request, timeout):
        assert request.full_url != "https://www.youtube.com/watch?v=demo123"
        assert request.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert b"archived-video" in request.data
        return _FakeResponse(
            json.dumps({"text": "Video transcript"}).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    transcript = generate_item_transcript(item=item)

    assert transcript == "Video transcript"


@pytest.mark.django_db
def test_generate_item_transcript_does_not_fallback_to_remote_when_archived_audio_open_fails(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/missing.mp3",
        archived_audio_content_type="audio/mpeg",
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("remote fallback should not run when archived audio exists")
        ),
    )

    with pytest.raises(TranscriptionGenerationError, match="Archived audio open failed"):
        generate_item_transcript(item=item)


@pytest.mark.django_db
def test_generate_item_transcript_rejects_oversized_archived_media(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=MAX_TRANSCRIPTION_BYTES + 1,
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("remote fallback should not run when archived audio exists")
        ),
    )

    with pytest.raises(TranscriptionGenerationError, match="Archived media exceeded 25 MiB"):
        generate_item_transcript(item=item)


@pytest.mark.django_db
def test_generate_item_transcript_rejects_unsupported_archived_media(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.bin",
        archived_audio_content_type="application/octet-stream",
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("remote fallback should not run when archived audio exists")
        ),
    )

    with pytest.raises(TranscriptionGenerationError, match="Unsupported archived audio type"):
        generate_item_transcript(item=item)
