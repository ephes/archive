from __future__ import annotations

import json

import pytest

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


@pytest.mark.django_db
def test_can_transcribe_item_detects_media_sources() -> None:
    item = Item(
        original_url="https://example.com/article",
        kind=ItemKind.PODCAST_EPISODE,
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
