from __future__ import annotations

import pytest
from django.db import connection

from archive.metadata import extract_metadata_from_html
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import _enrich_item_metadata_fields


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operator_override", [False, True])
def test_metadata_publication_preserves_edits_during_fetch(monkeypatch, operator_override):
    item = Item.objects.create(original_url="https://example.test/article")

    def fetch(url, timeout):
        assert not connection.in_atomic_block
        updates = dict(title="Manual title", author="Manual author", source="Manual source")
        if operator_override:
            updates.update(kind=ItemKind.QUOTE, classification_rule="operator_override")
        Item.objects.filter(pk=item.pk).update(**updates)
        return extract_metadata_from_html(
            '<title>Fetched title</title><meta name="author" content="Fetched author">'
            '<meta property="og:site_name" content="Fetched source">'
            '<meta property="og:audio" content="https://example.test/audio.mp3">',
            url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fetch)
    assert _enrich_item_metadata_fields(item)
    item.refresh_from_db()
    assert (item.title, item.author, item.source) == (
        "Manual title",
        "Manual author",
        "Manual source",
    )
    assert item.audio_url == "https://example.test/audio.mp3"
    if operator_override:
        assert item.kind == ItemKind.QUOTE
        assert item.classification_rule == "operator_override"
        assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_status == EnrichmentStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_classifier_lookup_and_publication_share_write_transaction(client, monkeypatch, settings):
    from archive import views

    settings.ARCHIVE_API_TOKEN = "synthetic-token"
    item = Item.objects.create(
        original_url="https://example.test/item", archived_audio_path="synthetic/audio.mp3"
    )
    original_lookup = views.get_object_or_404
    original_save = Item.save
    seen = []

    def lookup(*args, **kwargs):
        # An operator has committed before the classifier gets its write reservation.
        Item.objects.filter(pk=item.pk).update(
            kind=ItemKind.PODCAST_EPISODE, classification_rule="operator_override"
        )
        assert connection.in_atomic_block
        seen.append("lookup")
        return original_lookup(*args, **kwargs)

    def save(current, *args, **kwargs):
        if current.pk == item.pk:
            assert connection.in_atomic_block
        return original_save(current, *args, **kwargs)

    monkeypatch.setattr(views, "get_object_or_404", lookup)
    monkeypatch.setattr(Item, "save", save)
    response = client.patch(
        f"/api/items/{item.pk}/",
        data='{"kind":"quote","classification_rule":"quote_classifier"}',
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer synthetic-token",
    )
    assert seen == ["lookup"]
    assert response.status_code == 409
    item.refresh_from_db()
    assert item.kind == ItemKind.PODCAST_EPISODE
    assert item.classification_rule == "operator_override"
    assert item.archived_audio_path == "synthetic/audio.mp3"
