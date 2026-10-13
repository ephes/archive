"""Exercise lazy copies through the public endpoint and a fake Voxhelm HTTP server."""

import io
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import URLError

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.urls import reverse

from archive.article_audio import ArticleAudioJobUpdate, store_generated_article_audio
from archive.media_storage import referenced_archive_media_paths
from archive.models import EnrichmentStatus, Item, ItemKind, PendingArchiveMediaUpload
from archive.services import enrich_item_article_audio

PAYLOAD = bytes(range(256)) * 600
pytestmark = pytest.mark.django_db


@pytest.fixture
def article():
    return Item.objects.create(
        original_url="https://example.com/article",
        title="Article",
        kind=ItemKind.ARTICLE,
        short_summary="A substantial essay.",
        long_summary="A coherent argument. " * 40,
        summary_status=EnrichmentStatus.COMPLETE,
        article_audio_artifact_path="/speech.mp3",
    )


@pytest.fixture
def voxhelm(settings):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = f"http://127.0.0.1:{server.server_port}/v1"
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "fake-key"
    yield requests
    server.shutdown()
    server.server_close()
    thread.join()


def audio_url(article):
    return reverse("archive:item-article-audio", kwargs={"pk": article.pk})


def body(response):
    try:
        return b"".join(response.streaming_content) if response.streaming else response.content
    finally:
        response.close()


def test_backfill_storage_round_trip_and_feed(client, article, voxhelm):
    stale_item = Item.objects.get(pk=article.pk)
    feed = reverse("archive:podcast-feed")
    assert b'length="0"' in client.get(feed).content
    assert not voxhelm
    response = client.get(audio_url(article), headers={"Range": "bytes=64000-66000"})
    assert response.status_code == 206
    assert response["Content-Length"] == "2001"
    assert response["Content-Range"] == f"bytes 64000-66000/{len(PAYLOAD)}"
    assert body(response) == PAYLOAD[64000:66001]
    article.refresh_from_db()
    assert article.article_audio_size_bytes == len(PAYLOAD)
    assert article.article_audio_content_type == "audio/mpeg"
    with article.article_audio_file.open("rb") as stored:
        assert stored.read() == PAYLOAD
    assert article.article_audio_file.name in referenced_archive_media_paths()
    assert not PendingArchiveMediaUpload.objects.exists()
    assert f'length="{len(PAYLOAD)}"'.encode() in client.get(feed).content
    assert body(client.get(audio_url(article))) == PAYLOAD
    store_generated_article_audio(stale_item)
    assert voxhelm == [("/speech.mp3", "Bearer fake-key")]


@pytest.mark.parametrize(
    "headers,code,expected",
    [
        ({"Range": "bytes=0-0"}, 206, b"I"),
        ({"Range": "bytes=3-"}, 206, b"-audio"),
        ({"Range": "bytes=-4"}, 206, b"udio"),
        ({"Range": "bytes=-100"}, 206, b"ID3-audio"),
        ({"Range": "bytes=2-100"}, 206, b"3-audio"),
        ({"Range": "bytes=9-"}, 416, b""),
        ({"Range": "bytes=-0"}, 416, b""),
        ({"Range": "bytes=0-1,3-4"}, 200, b"ID3-audio"),
        ({"Range": "bytes=5-2"}, 200, b"ID3-audio"),
        ({"Range": "bytes=a-b"}, 200, b"ID3-audio"),
        ({"Range": "bytes=0-1", "If-Range": '"validator"'}, 200, b"ID3-audio"),
    ],
)
def test_stored_article_range_edges(client, article, monkeypatch, headers, code, expected):
    article.article_audio_file.save("speech.mp3", ContentFile(b"ID3-audio"), save=False)
    article.article_audio_size_bytes = 9
    article.article_audio_content_type = "audio/mpeg"
    article.save()
    monkeypatch.setattr("archive.article_audio.urlopen", lambda *a, **k: pytest.fail("download"))
    response = client.get(audio_url(article), headers=headers)
    assert response.status_code == code
    if code == 416:
        assert response["Content-Range"] == "bytes */9"
    else:
        assert response["Content-Length"] == str(len(expected))
        assert response["Content-Type"] == "audio/mpeg"
    assert response["Accept-Ranges"] == "bytes"
    assert body(response) == expected


def test_head_backfills_once_without_body(client, article, voxhelm):
    response = client.head(audio_url(article))
    assert response.status_code == 200
    assert response["Content-Length"] == str(len(PAYLOAD))
    assert response["Content-Type"] == "audio/mpeg"
    assert body(response) == b""
    assert body(client.head(audio_url(article), headers={"Range": "bytes=1-3"})) == b""
    assert len(voxhelm) == 1


def test_backfill_unreachable_returns_retry_without_storing(client, article, settings, monkeypatch):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "fake"

    def fail(request, timeout):
        assert timeout == 30
        raise URLError("unreachable")

    monkeypatch.setattr("archive.article_audio.urlopen", fail)
    response = client.get(audio_url(article))
    assert response.status_code == 503
    assert response["Retry-After"] == "60"
    article.refresh_from_db()
    assert not article.article_audio_file
    assert article.article_audio_size_bytes is None
    assert not PendingArchiveMediaUpload.objects.exists()
    assert storages["archive_media"].exists("article-audio") is False


@pytest.mark.parametrize("failure", ["cap", "read", "truncated", "empty", "type"])
def test_failed_download_never_writes_storage(client, article, settings, monkeypatch, failure):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "fake"
    settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES = 100_000

    class Response(io.BytesIO):
        headers = Message()

        def __enter__(self):
            return self

        def read(self, size=-1):
            assert size == 64 * 1024
            if failure == "read" and self.tell():
                raise TimeoutError("interrupted")
            return super().read(size)

    response = Response(b"" if failure == "empty" else PAYLOAD)
    response.headers["Content-Type"] = "text/html" if failure == "type" else "audio/mpeg"
    if failure == "truncated":
        settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES = len(PAYLOAD) + 10
        response.headers["Content-Length"] = str(len(PAYLOAD) + 1)
    monkeypatch.setattr("archive.article_audio.urlopen", lambda *a, **k: response)
    result = client.get(audio_url(article))
    assert result.status_code == 503
    assert response.closed
    article.refresh_from_db()
    assert not article.article_audio_file
    assert not PendingArchiveMediaUpload.objects.exists()
    assert not storages["archive_media"].exists("article-audio")


def test_generation_completes_then_stores(article, voxhelm, monkeypatch):
    article.article_audio_artifact_path = ""
    article.article_audio_status = EnrichmentStatus.PENDING
    article.save()
    monkeypatch.setattr(
        "archive.services.generate_item_article_audio",
        lambda **kw: ArticleAudioJobUpdate(
            job_id="job", state="succeeded", artifact_path="/speech.mp3"
        ),
    )
    assert enrich_item_article_audio(article)
    article.refresh_from_db()
    assert article.article_audio_status == EnrichmentStatus.COMPLETE
    assert article.article_audio_file
    assert article.article_audio_size_bytes == len(PAYLOAD)
    assert len(voxhelm) == 1


def test_article_storage_failure_does_not_publish(client, article, voxhelm, monkeypatch):
    storage = storages["archive_media"]
    original_save = storage.save

    def interrupted(name, content, **kwargs):
        original_save(name, content, **kwargs)
        raise OSError("storage write interrupted")

    monkeypatch.setattr(storage, "save", interrupted)
    assert client.get(audio_url(article)).status_code == 503
    article.refresh_from_db()
    assert not article.article_audio_file
    assert not PendingArchiveMediaUpload.objects.exists()
    assert list(Path(storage.location).rglob("*.mp3")) == []


@pytest.mark.django_db(transaction=True)
def test_stored_article_deleted_only_when_unreferenced(article, voxhelm):
    from archive.media_storage import delete_unreferenced_archive_media_paths

    store_generated_article_audio(article)
    name = article.article_audio_file.name
    storage = storages["archive_media"]
    assert delete_unreferenced_archive_media_paths((name,)) == []
    assert storage.exists(name)
    article.delete()
    assert not storage.exists(name)


@pytest.mark.django_db(transaction=True)
def test_migration_nullable_legacy_insert_and_reverse():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("archive", "0014_pendingarchivemediaupload")]
    after = [("archive", "0015_item_article_audio_content_type_and_more")]
    executor = MigrationExecutor(connection)
    legacy = executor.loader.project_state(before).apps.get_model("archive", "Item")
    # Old model inserts omit the new columns, as old workers do after migration.
    row = legacy.objects.create(original_url="https://example.com/legacy", title="Legacy")
    migrated = Item.objects.get(pk=row.pk)
    assert not migrated.article_audio_file
    assert migrated.article_audio_size_bytes is None
    assert migrated.article_audio_content_type is None
    try:
        executor.migrate(before)
        assert legacy.objects.get(pk=row.pk).title == "Legacy"
    finally:
        MigrationExecutor(connection).migrate(after)
    assert Item.objects.get(pk=row.pk).title == "Legacy"


def test_worker_copy_failure_retains_complete_artifact_then_lazy_retry(
    client, article, voxhelm, monkeypatch, caplog
):
    from archive.article_audio import urlopen
    from archive.services import request_item_reprocess

    article.article_audio_artifact_path = ""
    article.article_audio_status = EnrichmentStatus.PENDING
    article.save()
    generation_calls = []

    def generated(**kwargs):
        generation_calls.append(1)
        return ArticleAudioJobUpdate(job_id="job", state="succeeded", artifact_path="/speech.mp3")

    def unreachable(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr("archive.services.generate_item_article_audio", generated)
    monkeypatch.setattr("archive.article_audio.urlopen", unreachable)
    assert enrich_item_article_audio(article)
    article.refresh_from_db()
    assert article.article_audio_status == EnrichmentStatus.COMPLETE
    assert article.article_audio_generated
    assert article.article_audio_job_id == "job"
    assert article.article_audio_artifact_path == "/speech.mp3"
    assert not article.article_audio_file
    assert "lazy backfill will retry" in caplog.text
    request_item_reprocess(article)
    assert article.article_audio_status == EnrichmentStatus.COMPLETE
    monkeypatch.setattr("archive.article_audio.urlopen", urlopen)
    assert body(client.get(audio_url(article))) == PAYLOAD
    article.refresh_from_db()
    assert article.article_audio_generated
    assert article.article_audio_status == EnrichmentStatus.COMPLETE
    assert not article.article_audio_error
    assert generation_calls == [1]


def test_worker_recovers_generated_flag_without_resubmission(article, voxhelm, monkeypatch):
    article.article_audio_status = EnrichmentStatus.FAILED
    article.article_audio_generated = False
    article.article_audio_error = "old failure"
    article.save()
    monkeypatch.setattr(
        "archive.services.generate_item_article_audio", lambda **kw: pytest.fail("job")
    )
    assert enrich_item_article_audio(article)
    article.refresh_from_db()
    assert article.article_audio_generated
    assert article.article_audio_status == EnrichmentStatus.COMPLETE
    assert article.article_audio_error == ""
    assert article.article_audio_file


@pytest.mark.parametrize("after_commit", [False, True])
def test_interrupted_publication_compensation_preserves_committed_copy(
    article, voxhelm, monkeypatch, after_commit
):
    class Interrupted(BaseException):
        pass

    if after_commit:
        original_refresh = Item.refresh_from_db
        refresh_calls = []

        def interrupted_refresh(self, *args, **kwargs):
            refresh_calls.append(1)
            original_refresh(self, *args, **kwargs)
            if len(refresh_calls) == 2:
                raise Interrupted()

        monkeypatch.setattr(Item, "refresh_from_db", interrupted_refresh)
    else:

        def interrupted_release(*args, **kwargs):
            raise Interrupted()

        monkeypatch.setattr(
            "archive.article_audio.release_pending_archive_media_paths", interrupted_release
        )

    with pytest.raises(Interrupted):
        store_generated_article_audio(article)
    fresh = Item.objects.get(pk=article.pk)
    assert bool(fresh.article_audio_file) == after_commit
    storage = storages["archive_media"]
    files = list(Path(storage.location).rglob("*.mp3"))
    assert len(files) == int(after_commit)
    assert not PendingArchiveMediaUpload.objects.exists()


def test_mp3_alias_normalized_for_response_and_feed(client, article, settings, monkeypatch):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "fake"

    class Response(io.BytesIO):
        headers = Message()

    response = Response(b"ID3-audio")
    response.headers["Content-Type"] = "audio/mp3"
    monkeypatch.setattr("archive.article_audio.urlopen", lambda *a, **k: response)
    result = client.get(audio_url(article))
    assert result["Content-Type"] == "audio/mpeg"
    assert body(result) == b"ID3-audio"
    assert b'type="audio/mpeg"' in client.get(reverse("archive:podcast-feed")).content


def test_lazy_copy_refreshes_legacy_failure_state(article, voxhelm):
    article.article_audio_status = EnrichmentStatus.FAILED
    article.article_audio_error = "old failure"
    article.article_audio_generated = False
    article.save()
    store_generated_article_audio(article)
    assert article.article_audio_status == EnrichmentStatus.COMPLETE
    assert article.article_audio_generated
    assert article.article_audio_error == ""
    assert article.article_audio_poll_at is None
    article.save(
        update_fields=["article_audio_status", "article_audio_error", "article_audio_generated"]
    )
    article.refresh_from_db()
    assert article.article_audio_status == EnrichmentStatus.COMPLETE
    assert article.article_audio_generated
    assert article.article_audio_error == ""
