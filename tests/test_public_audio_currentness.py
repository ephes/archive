"""Visibility/artifact races at slow download and storage-open boundaries."""

import io
from concurrent.futures import ThreadPoolExecutor
from email.message import Message

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import connections
from django.urls import reverse

from archive.article_audio import store_generated_article_audio
from archive.models import Item, ItemKind

pytestmark = pytest.mark.django_db(transaction=True)


class AudioResponse(io.BytesIO):
    headers: Message


@pytest.fixture
def article():
    return Item.objects.create(
        original_url="https://example.com/synthetic",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/speech.mp3",
    )


def change_from_another_connection(pk, change):
    def change_row():
        try:
            if change == "delete":
                Item.objects.filter(pk=pk).delete()
            else:
                updates = {
                    "hide": {"is_public": False},
                    "artifact": {"article_audio_artifact_path": "/replacement.mp3"},
                    "file": {"article_audio_file": "article-audio/replacement.mp3"},
                    "replacement": {
                        "article_audio_artifact_path": "/replacement.mp3",
                        "article_audio_file": "article-audio/replacement.mp3",
                    },
                }
                Item.objects.filter(pk=pk).update(**updates[change])
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(change_row).result(timeout=5)


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("change", ["hide", "delete", "artifact", "replacement"])
def test_public_audio_rechecks_after_download(
    client, article, settings, monkeypatch, method, change
):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic-key"
    reads = []

    class Response(io.BytesIO):
        headers = Message()

        def read(self, size=-1):
            if not reads:
                change_from_another_connection(article.pk, change)
            reads.append(size)
            return super().read(size)

    upstream = Response(b"ID3-synthetic")
    upstream.headers["Content-Type"] = "audio/mpeg"
    monkeypatch.setattr("archive.article_audio.urlopen", lambda *a, **kw: upstream)
    opened = []
    storage = storages["archive_media"]
    original_open = storage.open

    def track_open(*a, **kw):
        handle = original_open(*a, **kw)
        opened.append(handle)
        return handle

    monkeypatch.setattr(storage, "open", track_open)
    response = getattr(client, method)(reverse("archive:item-article-audio", args=[article.pk]))
    assert response.status_code == 404
    assert not response.streaming
    assert upstream.closed
    assert not opened


@pytest.mark.parametrize("change", ["hide", "delete", "artifact", "file"])
def test_public_audio_closes_opened_handle_if_retired_during_open(
    client, article, monkeypatch, change
):
    article.article_audio_file.save("speech.mp3", ContentFile(b"ID3-synthetic"), save=False)
    article.article_audio_size_bytes = 13
    article.save()
    storage = storages["archive_media"]
    original_open = storage.open
    opened = []

    def retire_during_open(*a, **kw):
        handle = original_open(*a, **kw)
        opened.append(handle)
        change_from_another_connection(article.pk, change)
        return handle

    monkeypatch.setattr(storage, "open", retire_during_open)
    response = client.get(reverse("archive:item-article-audio", args=[article.pk]))
    try:
        assert response.status_code == 404
        assert opened and all(handle.closed for handle in opened)
    finally:
        response.close()


def test_worker_can_store_private_article(article, settings, monkeypatch):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic-key"
    Item.objects.filter(pk=article.pk).update(is_public=False)
    upstream = AudioResponse(b"ID3-synthetic")
    upstream.headers = Message()
    upstream.headers["Content-Type"] = "audio/mpeg"
    monkeypatch.setattr("archive.article_audio.urlopen", lambda *a, **kw: upstream)
    store_generated_article_audio(article)
    article.refresh_from_db()
    assert not article.is_public
    assert article.article_audio_file
