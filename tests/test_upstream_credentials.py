from __future__ import annotations

from email.message import Message
from io import BytesIO
from urllib.request import HTTPHandler, HTTPSHandler, Request, build_opener
from urllib.response import addinfourl

import pytest

from archive import article_audio, transcriptions


@pytest.mark.parametrize("module", [article_audio, transcriptions])
@pytest.mark.parametrize(
    "path",
    [
        "https://foreign.test/capture",
        "//foreign.test/capture",
        "http://api.test/capture",
        "https://user@api.test/capture",
        "https://api.test:444/capture",
    ],
)
def test_artifact_urls_refuse_foreign_authority(settings, module, path):
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = "https://api.test/v1"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.test/v1"
    with pytest.raises(
        (
            OSError,
            article_audio.ArticleAudioGenerationError,
            transcriptions.TranscriptionGenerationError,
        )
    ):
        module._api_url(path)


@pytest.mark.parametrize("module", [article_audio, transcriptions])
@pytest.mark.parametrize(
    "path,expected",
    [
        ("jobs/one", "https://api.test/v1/jobs/one"),
        ("/artifacts/one", "https://api.test/artifacts/one"),
        ("https://API.test:443/artifacts/one", "https://API.test:443/artifacts/one"),
    ],
)
def test_artifact_urls_keep_same_origin(settings, module, path, expected):
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = "https://api.test/v1"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.test/v1"
    assert module._api_url(path) == expected


@pytest.mark.parametrize("module", ["article_audio", "transcriptions", "summaries"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "destination",
    [
        "https://foreign.test/capture",
        "http://api.test/capture",
        "https://user@api.test/capture",
        "https://api.test/final",
    ],
)
def test_authenticated_calls_guard_redirects(monkeypatch, module, method, destination):
    # Execute urllib's real redirect machinery with an in-memory HTTP transport.
    import urllib.request

    try:
        from archive import upstream_http
    except ImportError:
        upstream_http = None

    sent = []
    responses = []

    class Transport(HTTPHandler, HTTPSHandler):
        def http_open(self, req):
            sent.append((req.full_url, req.get_header("Authorization"), req.get_method()))
            headers = Message()
            code = 200
            if len(sent) == 1:
                headers["Location"] = destination
                code = 302
            response = addinfourl(BytesIO(b"{}"), headers, req.full_url, code)
            response.msg = "Synthetic"
            responses.append(response)
            return response

        https_open = http_open

    real_build = build_opener
    if upstream_http is not None:
        monkeypatch.setattr(
            upstream_http, "build_opener", lambda *handlers: real_build(Transport(), *handlers)
        )
    # Also catches any production call site still using ordinary urllib.urlopen.
    monkeypatch.setattr(urllib.request, "_opener", real_build(Transport()))
    imported = __import__(f"archive.{module}", fromlist=["urlopen"])
    request = Request(
        "https://api.test/start",
        data=b"{}" if method == "POST" else None,
        headers={"Authorization": "Bearer SYNTHETIC"},
        method=method,
    )
    if destination == "https://api.test/final":
        with imported.urlopen(request, timeout=1) as response:
            assert response.read() == b"{}"
        assert sent[-1][:2] == (destination, "Bearer SYNTHETIC")
    else:
        with pytest.raises(OSError):
            imported.urlopen(request, timeout=1)
        assert len(sent) == 1
        assert responses[0].closed
