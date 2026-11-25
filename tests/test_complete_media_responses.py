"""Synthetic responses through the real disposable downloader and publication path."""

import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from test_media_archival import _fake_ffmpeg_run

from archive import article_audio, media_archival, metadata, upstream_http
from archive.media_storage import iter_archive_media_object_names
from archive.models import EnrichmentStatus, Item, ItemKind, PendingArchiveMediaUpload
from archive.services import enrich_item_media_archive

RESPONSE_SCRIPT = """
import json, runpy, sys
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
ns = runpy.run_path(sys.argv[1])
config = json.loads(sys.argv[2])
class Response(BytesIO):
    code = config.get('status', 200)
    headers = Message()
    content_type = config.get('content_type', 'audio/mpeg')
    if content_type is not None:
        headers['Content-Type'] = content_type
    for key, value in config.get('headers', []): headers[key] = value
    def getcode(self): return self.code
    def geturl(self): return 'https://example.test/media'
    def read(self, n=-1):
        assert not config.get('no_read'), 'body must be refused before reading'
        return super().read(n)
    def __exit__(self, *args):
        super().__exit__(*args)
        assert self.closed
def open_url(*args, **kwargs):
    response = Response(config.get('body', 'abc').encode())
    if config.get('http_error'):
        raise HTTPError('https://example.test/media', response.code, 'Synthetic',
                        response.headers, response)
    return response
if sys.argv[1].endswith('upstream_download.py'):
    ns['main'].__globals__['_load_transport'] = lambda: SimpleNamespace(
        _open_authenticated_url=open_url)
else:
    ns['main'].__globals__['_load_safe_fetch'] = lambda: SimpleNamespace(open_public_url=open_url)
raise SystemExit(ns['main']())
"""


@pytest.fixture
def synthetic_public_response(monkeypatch):
    run = subprocess.run
    exchanges = []

    def install(config):
        def download(command, **kwargs):
            path = Path(json.loads(kwargs["input"])["path"])
            result = run(
                [sys.executable, "-I", "-c", RESPONSE_SCRIPT, command[-1], json.dumps(config)],
                **kwargs,
            )
            exchanges.append((result, path))
            return result

        monkeypatch.setattr(media_archival, "run_download", download)
        monkeypatch.setattr(metadata, "run_download", download)
        return exchanges

    return install


@pytest.fixture
def synthetic_authenticated_response(monkeypatch, settings):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "SYNTHETIC"
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = "https://example.test/v1"
    run = subprocess.run
    exchanges = []

    def install(config, before_download=lambda: None):
        def download(command, **kwargs):
            before_download()
            path = Path(json.loads(kwargs["input"])["path"])
            result = run(
                [sys.executable, "-I", "-c", RESPONSE_SCRIPT, command[-1], json.dumps(config)],
                **kwargs,
            )
            exchanges.append((result, path))
            return result

        monkeypatch.setattr(upstream_http.subprocess, "run", download)
        return exchanges

    return install


INVALID_RESPONSES = [
    pytest.param({"status": 206}, id="unsolicited-206"),
    pytest.param({"headers": [("Content-Range", "bytes 0-2/100")]}, id="content-range"),
    pytest.param({"body": ""}, id="empty"),
    pytest.param({"headers": [("Content-Length", "100")]}, id="short-body"),
    pytest.param({"headers": [("Content-Length", "2")]}, id="long-body"),
    pytest.param({"headers": [("Content-Length", "-1")]}, id="negative-length"),
    pytest.param({"headers": [("Content-Length", "invalid")]}, id="malformed-length"),
    pytest.param(
        {"headers": [("Content-Length", "3"), ("Content-Length", "100")]},
        id="ambiguous-length",
    ),
    pytest.param({"status": 204}, id="no-content-status"),
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("response", INVALID_RESPONSES)
def test_public_incomplete_response_never_publishes(
    synthetic_public_response, response, monkeypatch
):
    monkeypatch.setattr(media_archival, "run_media_command", _fake_ffmpeg_run)
    exchanges = synthetic_public_response({**response, "content_type": "video/mp4"})
    old_path = "items/old/video.mp4"
    storage = storages["archive_media"]
    storage.save(old_path, ContentFile(b"previous video"))
    item = Item.objects.create(
        original_url="https://example.test/video.mp4",
        kind=ItemKind.VIDEO,
        archived_video_path=old_path,
    )

    assert not enrich_item_media_archive(item)

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.media_archive_retry_count == 1
    assert item.media_archive_retry_at is not None
    assert item.archived_video_path == old_path
    assert not item.archived_audio_path
    assert storage.open(old_path).read() == b"previous video"
    assert set(iter_archive_media_object_names()) == {old_path}
    assert not PendingArchiveMediaUpload.objects.exists()
    assert len(exchanges) == 1
    result, path = exchanges[0]
    assert result.returncode == 1
    assert not path.parent.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("headers", [[], [("Content-Length", "3")]])
def test_public_complete_response_is_published(synthetic_public_response, headers):
    exchanges = synthetic_public_response({"headers": headers})
    item = Item.objects.create(
        original_url="https://example.test/audio.mp3", kind=ItemKind.PODCAST_EPISODE
    )

    assert enrich_item_media_archive(item)

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_size_bytes == 3
    with storages["archive_media"].open(item.archived_audio_path) as stored:
        assert stored.read() == b"abc"
    assert exchanges[0][0].returncode == 0
    assert not exchanges[0][1].parent.exists()


def test_public_oversized_declared_length_is_refused_before_read(synthetic_public_response):
    exchanges = synthetic_public_response(
        {"headers": [("Content-Length", "4")], "body": "abcd", "no_read": True}
    )
    request = Request("https://example.test/audio.mp3")

    with pytest.raises(media_archival.MediaSizeLimitError, match="3-byte archive limit"):
        with media_archival._open_direct_media(request, timeout=2, max_bytes=3):
            pytest.fail("oversized download must not be opened")

    result, path = exchanges[0]
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "error": "Media source exceeded the configured 3-byte archive limit",
        "limit_exceeded": True,
    }
    assert not path.parent.exists()


def test_public_declared_length_equal_to_limit_is_accepted(synthetic_public_response):
    synthetic_public_response({"headers": [("Content-Length", "3")]})
    request = Request("https://example.test/audio.mp3")

    with media_archival._open_direct_media(request, timeout=2, max_bytes=3) as response:
        assert response.read() == b"abc"


def test_metadata_keeps_bounded_prefix_contract(synthetic_public_response, monkeypatch):
    body = "<head><title>Prefix</title></head><body>"
    exchanges = synthetic_public_response(
        {
            "status": 206,
            "content_type": "text/html",
            "headers": [("Content-Range", "bytes 0-999/10000"), ("Content-Length", "1000")],
            "body": body + "x" * 100,
        }
    )
    monkeypatch.setattr(metadata, "MAX_METADATA_BYTES", len(body))

    assert metadata.extract_metadata_from_url("https://example.test/page").title == "Prefix"
    assert exchanges[0][0].returncode == 0
    assert not exchanges[0][1].parent.exists()


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("Text/HTML; charset=utf-8", "<html><head><title>Metadata</title></head></html>"),
        (
            "application/xhtml+xml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Metadata</title></head></html>',
        ),
        ("text/xml", '<?xml version="1.0"?><doc><title>Metadata</title></doc>'),
        ("application/xml", '<?xml version="1.0"?><doc><title>Metadata</title></doc>'),
        (
            "application/rss+xml",
            '<rss version="2.0"><channel><title>Metadata</title></channel></rss>',
        ),
        (
            "application/atom+xml",
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>Metadata</title></feed>',
        ),
        ("image/svg+xml", '<svg xmlns="http://www.w3.org/2000/svg"><title>Metadata</title></svg>'),
    ],
)
def test_metadata_reads_html_and_xml_types(synthetic_public_response, content_type, body):
    synthetic_public_response({"content_type": content_type, "body": body})

    assert metadata.extract_metadata_from_url("https://example.test/page").title == "Metadata"


@pytest.mark.parametrize(
    "content_type,reported_type",
    [
        ("application/nothtml", "application/nothtml"),
        ("application/xml-wrapper", "application/xml-wrapper"),
        ("application/rss+xml-extra", "application/rss+xml-extra"),
        ("application/+xml", "application/+xml"),
        ("application/not xml+xml", "application/not xml+xml"),
        (None, "text/plain"),
    ],
)
def test_metadata_refuses_unsupported_types_before_read(
    synthetic_public_response, content_type, reported_type
):
    exchanges = synthetic_public_response({"content_type": content_type, "no_read": True})
    message = f"Unsupported content type: {reported_type}"

    with pytest.raises(metadata.MetadataExtractionError) as caught:
        metadata.extract_metadata_from_url("https://example.test/page")

    assert str(caught.value) == f"Could not fetch metadata: {message}"
    result, _ = exchanges[0]
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == message


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("response", INVALID_RESPONSES)
@pytest.mark.parametrize("previous_copy", [False, True])
def test_article_incomplete_response_never_publishes(
    synthetic_authenticated_response, response, previous_copy
):
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/v1/artifacts/speech.mp3",
    )
    old_path = "article-audio/previous/speech.mp3"
    storage = storages["archive_media"]

    def concurrent_copy():
        # A copy published after the caller's initial refresh must survive refusal.
        if previous_copy:
            storage.save(old_path, ContentFile(b"previous audio"))
            Item.objects.filter(pk=item.pk).update(
                article_audio_file=old_path,
                article_audio_size_bytes=14,
                article_audio_content_type="audio/mpeg",
            )

    exchanges = synthetic_authenticated_response(response, before_download=concurrent_copy)

    with pytest.raises(article_audio.ArticleAudioGenerationError):
        article_audio.store_generated_article_audio(item=item)

    item.refresh_from_db()
    assert item.article_audio_artifact_path == "/v1/artifacts/speech.mp3"
    assert not PendingArchiveMediaUpload.objects.exists()
    assert len(exchanges) == 1
    result, path = exchanges[0]
    assert result.returncode == 1
    assert not path.parent.exists()
    if previous_copy:
        assert item.article_audio_file.name == old_path
        assert item.article_audio_size_bytes == 14
        assert item.article_audio_content_type == "audio/mpeg"
        with storage.open(old_path) as stored:
            assert stored.read() == b"previous audio"
        assert set(iter_archive_media_object_names()) == {old_path}
    else:
        assert not item.article_audio_file
        assert not list(iter_archive_media_object_names())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("headers", [[], [("Content-Length", "3")]])
def test_article_complete_response_is_published(synthetic_authenticated_response, headers):
    exchanges = synthetic_authenticated_response({"headers": headers})
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/v1/artifacts/speech.mp3",
    )

    article_audio.store_generated_article_audio(item=item)

    item.refresh_from_db()
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_size_bytes == 3
    with item.article_audio_file.open("rb") as stored:
        assert stored.read() == b"abc"
    assert exchanges[0][0].returncode == 0
    assert not exchanges[0][1].parent.exists()


@pytest.mark.parametrize(
    "framing",
    [
        pytest.param({"status": 206}, id="partial-status"),
        pytest.param({"headers": [("Content-Range", "bytes 0-14/100")]}, id="partial-header"),
        pytest.param({"headers": [("Content-Length", "100")]}, id="short-body"),
    ],
)
def test_incomplete_json_is_refused(synthetic_authenticated_response, framing):
    exchanges = synthetic_authenticated_response(
        {"content_type": "application/json", "body": '{"status":"ok"}', **framing}
    )

    with pytest.raises(OSError, match="complete"):
        with upstream_http.open_authenticated_url(
            Request("https://example.test/v1/job"), timeout=5
        ):
            pytest.fail("incomplete JSON response admitted")

    assert exchanges[0][0].returncode == 1
    assert not exchanges[0][1].parent.exists()


@pytest.mark.parametrize("status", [200, 201, 202])
def test_complete_json_is_admitted(synthetic_authenticated_response, status):
    body = b'{"status":"ok"}'
    exchanges = synthetic_authenticated_response(
        {
            "status": status,
            "content_type": "application/json",
            "headers": [("Content-Length", str(len(body)))],
            "body": body.decode(),
        }
    )

    with upstream_http.open_authenticated_url(
        Request("https://example.test/v1/job"), timeout=5
    ) as response:
        assert response.read() == body

    assert exchanges[0][0].returncode == 0
    assert not exchanges[0][1].parent.exists()


def test_http_error_body_is_bounded_without_success_framing_checks(
    synthetic_authenticated_response,
):
    exchanges = synthetic_authenticated_response(
        {
            "status": 500,
            "http_error": True,
            "content_type": "application/json",
            "headers": [("Content-Range", "bytes 0-2/100"), ("Content-Length", "100")],
            "body": "e" * 70000,
        }
    )
    request = Request("https://example.test/v1/artifact")
    with pytest.raises(HTTPError) as caught:
        with upstream_http.open_authenticated_url(request, timeout=5, audio=True):
            pytest.fail("HTTP error admitted")
    with caught.value as response:
        assert response.code == 500
        assert response.read() == b"e" * 65536
    assert exchanges[0][0].returncode == 0
    assert not exchanges[0][1].parent.exists()


@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize(
    "partial", [{"status": 206}, {"headers": [("Content-Range", "bytes 0-2/100")]}]
)
def test_partial_response_refused_before_body(
    synthetic_public_response, synthetic_authenticated_response, authenticated, partial
):
    config = {**partial, "no_read": True}
    request = Request("https://example.test/media.mp3")
    if authenticated:
        exchanges = synthetic_authenticated_response(config)
        response = upstream_http.open_authenticated_url(request, timeout=5, audio=True)
        error = OSError
    else:
        exchanges = synthetic_public_response(config)
        response = media_archival._open_direct_media(request, timeout=5)
        error = media_archival.MediaArchivalError
    with pytest.raises(error, match="complete HTTP 200 representation"):
        with response:
            pytest.fail("partial response admitted")
    assert exchanges[0][0].returncode == 1
    assert not exchanges[0][1].parent.exists()
