from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.utils import timezone

from archive import services
from archive.models import EnrichmentStatus as Status
from archive.models import Item, ItemKind, ProcessingEvent

pytestmark = pytest.mark.django_db(transaction=True)


def update_on_other_connection(pk, updates):
    def update():
        try:
            Item.objects.filter(pk=pk).update(**updates)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(update).result(timeout=5)


@pytest.mark.parametrize("path", ["generated", "unsupported", "not_ready"])
@pytest.mark.parametrize("change", ["hold", "kind", "job", "source", "attempt"])
def test_article_audio_fastpaths_preserve_concurrent_authority(monkeypatch, path, change):
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.LINK if path == "unsupported" else ItemKind.ARTICLE,
        article_audio_artifact_path="/old.mp3" if path == "generated" else "",
        article_audio_status=Status.PROCESSING,
        article_audio_error="old error",
        article_audio_poll_at=timezone.now(),
        summary_status=Status.PENDING,
    )
    updates = {
        "hold": {"article_audio_status": Status.FAILED, "article_audio_error": "Held"},
        "kind": {"kind": ItemKind.QUOTE},
        "job": {
            "article_audio_job_id": "replacement",
            "article_audio_status": Status.PENDING,
            "article_audio_poll_at": timezone.now() + timedelta(minutes=5),
        },
        "source": {"original_url": "https://example.test/replacement"},
        "attempt": {"processing_started_at": timezone.now()},
    }[change]
    original = Item.has_generated_article_audio.fget
    paused = False

    def pause(stale):
        nonlocal paused
        result = original(stale)
        if stale is item and not paused:
            paused = True
            assert not connection.in_atomic_block
            update_on_other_connection(item.pk, updates)
        return result

    monkeypatch.setattr(Item, "has_generated_article_audio", property(pause))
    monkeypatch.setattr(
        services, "store_generated_article_audio", lambda **kw: pytest.fail("obsolete copy")
    )
    monkeypatch.setattr(
        services, "generate_item_article_audio", lambda **kw: pytest.fail("obsolete synthesis")
    )
    assert services.enrich_item_article_audio(item)
    current = Item.objects.get(pk=item.pk)
    for field, value in updates.items():
        assert getattr(current, field) == value
        assert getattr(item, field) == value
    assert current.article_audio_status == updates.get("article_audio_status", Status.PROCESSING)
    assert current.article_audio_error == updates.get("article_audio_error", "old error")
    assert not current.article_audio_generated
    assert not ProcessingEvent.objects.filter(item_id=item.pk).exists()


@pytest.mark.parametrize("path", ["generated", "unsupported", "not_ready"])
def test_current_article_audio_fastpaths_still_normalize(monkeypatch, path):
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.LINK if path == "unsupported" else ItemKind.ARTICLE,
        article_audio_artifact_path="/current.mp3" if path == "generated" else "",
        article_audio_status=Status.PROCESSING,
        article_audio_error="old error",
        article_audio_poll_at=timezone.now(),
        summary_status=Status.PENDING,
    )
    copies = []

    def copy(**kwargs):
        assert not connection.in_atomic_block
        copies.append(kwargs["item"].pk)

    monkeypatch.setattr(services, "store_generated_article_audio", copy)
    assert services.enrich_item_article_audio(item)
    item.refresh_from_db()
    assert item.article_audio_status == (Status.PENDING if path == "not_ready" else Status.COMPLETE)
    assert item.article_audio_error == ""
    assert item.article_audio_poll_at is None
    assert item.article_audio_generated == (path == "generated")
    assert copies == ([item.pk] if path == "generated" else [])


@pytest.mark.parametrize("change", ["hold", "kind", "job", "attempt"])
def test_generated_fastpath_copy_cannot_publish_over_new_authority(monkeypatch, settings, change):
    from email.message import Message
    from io import BytesIO
    from pathlib import Path

    from django.core.files.storage import storages

    from archive.models import PendingArchiveMediaUpload

    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/old.mp3",
        article_audio_status=Status.PENDING,
    )
    updates = {
        "hold": {"article_audio_status": Status.FAILED, "article_audio_error": "Held"},
        "kind": {"kind": ItemKind.QUOTE},
        "job": {"article_audio_job_id": "replacement", "article_audio_status": Status.PENDING},
        "attempt": {"processing_started_at": timezone.now()},
    }[change]

    class Response(BytesIO):
        headers = Message()
        headers["Content-Type"] = "audio/mpeg"

        def __enter__(self):
            assert not connection.in_atomic_block
            update_on_other_connection(item.pk, updates)
            return self

    monkeypatch.setattr(
        "archive.article_audio.urlopen", lambda *a, **kw: Response(b"ID3-synthetic")
    )
    assert services.enrich_item_article_audio(item)
    current = Item.objects.get(pk=item.pk)
    for field, value in updates.items():
        assert getattr(current, field) == value
    assert not current.article_audio_file
    assert not PendingArchiveMediaUpload.objects.exists()
    storage = storages["archive_media"]
    assert not any(path.is_file() for path in Path(storage.location).rglob("*"))
