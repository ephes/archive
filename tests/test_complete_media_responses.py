"""Synthetic responses through the real disposable downloader and publication path."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from test_media_archival import _fake_ffmpeg_run

from archive import media_archival, metadata
from archive.media_storage import iter_archive_media_object_names
from archive.models import EnrichmentStatus, Item, ItemKind, PendingArchiveMediaUpload
from archive.services import enrich_item_media_archive

RESPONSE_SCRIPT = """
import json, runpy, sys
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
ns = runpy.run_path(sys.argv[1])
config = json.loads(sys.argv[2])
class Response(BytesIO):
    code = config.get('status', 200)
    headers = Message()
    headers['Content-Type'] = config.get('content_type', 'audio/mpeg')
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
    return Response(config.get('body', 'abc').encode())
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
