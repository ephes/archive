from __future__ import annotations

import json

import pytest

from archive.article_audio import (
    DEFAULT_AUDIO_FORMAT,
    ArticleAudioGenerationError,
    DownloadedArticleAudio,
    build_article_audio_script,
    download_generated_article_audio,
    generate_item_article_audio,
)
from archive.models import Item, ItemKind


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _FakeResponse:
    def __init__(self, payload: bytes, *, content_type: str, url: str) -> None:
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
def test_build_article_audio_script_uses_title_and_long_summary() -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        kind=ItemKind.ARTICLE,
        title="Article headline",
        short_summary="Short summary",
        long_summary="Long summary with more context.",
    )

    assert build_article_audio_script(item) == "Article headline\n\nLong summary with more context."


@pytest.mark.django_db
def test_generate_item_article_audio_submits_batch_job(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        kind=ItemKind.ARTICLE,
        title="Article headline",
        long_summary="Long summary with more context.",
    )
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "tts-token"
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = "https://voxhelm.example.test/v1"
    settings.ARCHIVE_ARTICLE_AUDIO_MODEL = "tts-1"
    settings.ARCHIVE_ARTICLE_AUDIO_LANGUAGE = "en"
    settings.ARCHIVE_ARTICLE_AUDIO_VOICE = "en_US-lessac-medium"

    def fake_urlopen(request, timeout):
        assert timeout == 30
        assert request.full_url == "https://voxhelm.example.test/v1/jobs"
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["job_type"] == "synthesize"
        assert payload["output"]["formats"] == [DEFAULT_AUDIO_FORMAT]
        assert payload["input"]["text"] == "Article headline\n\nLong summary with more context."
        return _FakeResponse(
            json.dumps({"id": "job-123", "state": "queued"}).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.article_audio.urlopen", fake_urlopen)

    update = generate_item_article_audio(item=item)

    assert update.job_id == "job-123"
    assert update.state == "queued"
    assert update.is_pending is True


@pytest.mark.django_db
def test_generate_item_article_audio_polls_existing_job(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        kind=ItemKind.ARTICLE,
        title="Article headline",
        long_summary="Long summary with more context.",
        article_audio_job_id="job-123",
    )
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "tts-token"
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = "https://voxhelm.example.test/v1"

    def fake_urlopen(request, timeout):
        assert timeout == 30
        assert request.full_url == "https://voxhelm.example.test/v1/jobs/job-123"
        return _FakeResponse(
            json.dumps(
                {
                    "id": "job-123",
                    "state": "succeeded",
                    "result": {
                        "artifacts": {
                            DEFAULT_AUDIO_FORMAT: "/v1/jobs/job-123/artifacts/speech.mp3"
                        }
                    },
                }
            ).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.article_audio.urlopen", fake_urlopen)

    update = generate_item_article_audio(item=item)

    assert update.is_complete is True
    assert update.artifact_path == "/v1/jobs/job-123/artifacts/speech.mp3"


@pytest.mark.django_db
def test_download_generated_article_audio_fetches_artifact(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/v1/jobs/job-123/artifacts/speech.mp3",
    )
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "tts-token"
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = "https://voxhelm.example.test/v1"

    def fake_urlopen(request, timeout):
        assert timeout == 30
        assert request.full_url == "https://voxhelm.example.test/v1/jobs/job-123/artifacts/speech.mp3"
        return _FakeResponse(
            b"ID3-audio",
            content_type="audio/mpeg",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.article_audio.urlopen", fake_urlopen)

    audio = download_generated_article_audio(item=item)

    assert audio == DownloadedArticleAudio(content_type="audio/mpeg", payload=b"ID3-audio")


@pytest.mark.django_db
def test_download_generated_article_audio_rejects_oversized_artifact(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/v1/jobs/job-123/artifacts/speech.mp3",
    )
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "tts-token"
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = "https://voxhelm.example.test/v1"
    settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES = 4

    def fake_urlopen(request, timeout):
        assert timeout == 30
        return _FakeResponse(
            b"12345",
            content_type="audio/mpeg",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.article_audio.urlopen", fake_urlopen)

    with pytest.raises(ArticleAudioGenerationError, match="download limit"):
        download_generated_article_audio(item=item)


@pytest.mark.django_db
def test_generate_item_article_audio_requires_configured_api_key(settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        kind=ItemKind.ARTICLE,
        title="Article headline",
        long_summary="Long summary with more context.",
    )
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = ""

    with pytest.raises(ArticleAudioGenerationError, match="not configured"):
        generate_item_article_audio(item=item)
