import io
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from archive import article_audio, summaries, transcriptions
from archive.api_responses import MAX_API_ERROR_BYTES, MAX_API_RESPONSE_BYTES


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.sizes = []
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, size=-1):
        self.sizes.append(size)
        return super().read(size)


CALLS = [
    (
        article_audio,
        article_audio.ArticleAudioGenerationError,
        lambda: article_audio._request_json(Request("https://api.example/"), 1, "Audio"),
    ),
    (
        transcriptions,
        transcriptions.TranscriptionGenerationError,
        lambda: transcriptions._request_json(Request("https://api.example/"), 1, "Transcript"),
    ),
    (summaries, summaries.SummaryGenerationError, lambda: summaries._request_summary("prompt", 1)),
    (
        transcriptions,
        transcriptions.TranscriptionGenerationError,
        lambda: transcriptions._request_transcription(
            transcriptions.DownloadedMedia("test.mp3", "audio/mpeg", b"audio"), "prompt", 1
        ),
    ),
]


@pytest.mark.parametrize("module,error,call", CALLS)
def test_success_body_limit(monkeypatch, module, error, call):
    response = Response(b" " * (MAX_API_RESPONSE_BYTES + 1))
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: response)
    with pytest.raises(error, match="exceeded the 8 MiB limit"):
        call()
    assert response.sizes == [MAX_API_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("module,error,call", CALLS)
def test_error_body_limit(monkeypatch, module, error, call):
    response = Response(b"x" * (MAX_API_ERROR_BYTES + 100))
    failure = HTTPError("https://api.example/", 500, "failed", response.headers, response)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(module, "urlopen", fail)
    with pytest.raises(error, match="HTTP 500") as caught:
        call()
    assert response.sizes == [MAX_API_ERROR_BYTES]
    assert response.closed
    assert str(caught.value).count("x") == MAX_API_ERROR_BYTES


@pytest.mark.parametrize("module", [article_audio, transcriptions])
def test_normal_json_and_exact_limit(monkeypatch, module):
    response = Response(b"{}" + b" " * (MAX_API_RESPONSE_BYTES - 2))
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: response)
    assert module._request_json(Request("https://api.example/"), 1, "API") == {}
    assert response.sizes == [MAX_API_RESPONSE_BYTES + 1]


def test_audio_artifact_error_body_limit(monkeypatch, settings):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "test"
    response = Response(b"x" * (MAX_API_ERROR_BYTES + 100))
    failure = HTTPError("https://api.example/", 500, "failed", response.headers, response)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(article_audio, "urlopen", fail)
    from archive.models import Item

    item = Item(article_audio_artifact_path="/artifact")
    with pytest.raises(article_audio.ArticleAudioGenerationError, match="HTTP 500"):
        article_audio.download_generated_article_audio(item)
    assert response.sizes == [MAX_API_ERROR_BYTES]
    assert response.closed
