from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from archive.metadata import MetadataExtractionError, extract_metadata_from_html
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import (
    claim_pending_item,
    enrich_item_metadata,
    enrich_pending_items,
    prepare_item_for_enrichment,
    recover_processing_items,
)


@pytest.mark.django_db
def test_extract_metadata_from_html_prefers_structured_fields() -> None:
    html = """
    <html>
      <head>
        <title>Fallback title</title>
        <meta property="og:title" content="OG Title">
        <meta property="og:site_name" content="OG Site">
        <meta name="author" content="Meta Author">
        <meta property="article:published_time" content="2026-03-01T12:34:56+00:00">
        <meta property="og:audio" content="/media/audio.mp3">
        <script type="application/ld+json">
          {"@type":"Article","headline":"LD Title","publisher":{"name":"LD Publisher"}}
        </script>
      </head>
      <body></body>
    </html>
    """

    metadata = extract_metadata_from_html(html=html, base_url="https://example.com/articles/demo")

    assert metadata.title == "OG Title"
    assert metadata.source == "OG Site"
    assert metadata.author == "Meta Author"
    assert metadata.media_url == "https://example.com/media/audio.mp3"
    assert metadata.audio_url == "https://example.com/media/audio.mp3"
    assert metadata.kind_hint == ItemKind.ARTICLE
    assert metadata.original_published_at == datetime(
        2026,
        3,
        1,
        12,
        34,
        56,
        tzinfo=UTC,
    )


@pytest.mark.django_db
def test_enrich_item_metadata_updates_missing_fields_without_overwriting_existing_ones(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/demo",
        title="Manual title",
        kind=ItemKind.LINK,
    )

    def fake_extract(url: str, timeout: int):
        assert url == item.original_url
        assert timeout == 15
        return extract_metadata_from_html(
            html="""
            <html>
              <head>
                <meta property="og:title" content="Fetched title">
                <meta property="og:site_name" content="Example Site">
                <meta name="author" content="Example Author">
                <meta property="article:published_time" content="2026-03-01T12:34:56+00:00">
                <meta property="og:audio" content="https://cdn.example.com/audio.mp3">
              </head>
            </html>
            """,
            base_url=item.original_url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.title == "Manual title"
    assert item.source == "Example Site"
    assert item.author == "Example Author"
    assert item.original_published_at == datetime(
        2026,
        3,
        1,
        12,
        34,
        56,
        tzinfo=UTC,
    )
    assert item.media_url == "https://cdn.example.com/audio.mp3"
    assert item.audio_url == "https://cdn.example.com/audio.mp3"
    assert item.kind == ItemKind.PODCAST_EPISODE
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_error == ""


@pytest.mark.django_db
def test_api_items_are_public_immediately_and_join_feed_after_enrichment(
    client,
    settings,
    api_url: str,
    monkeypatch,
) -> None:
    settings.ARCHIVE_API_TOKEN = "test-token"

    response = client.post(
        api_url,
        data='{"url":"https://example.com/shared"}',
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 201
    item = Item.objects.get(pk=response.json()["id"])
    assert item.title == ""
    assert item.enrichment_status == EnrichmentStatus.PENDING
    assert b"https://example.com/shared" in client.get(reverse("archive:overview")).content
    assert b"<item>" not in client.get(reverse("archive:rss-feed")).content

    def fake_extract(url: str, timeout: int):
        return extract_metadata_from_html(
            html="""
            <html>
              <head>
                <meta property="og:title" content="Extracted title">
                <meta property="og:site_name" content="Example Site">
              </head>
            </html>
            """,
            base_url=url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)

    assert enrich_pending_items(limit=1) == 1

    item.refresh_from_db()
    assert item.title == "Extracted title"
    assert item.source == "Example Site"
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert b"Extracted title" in client.get(reverse("archive:rss-feed")).content


@pytest.mark.django_db
def test_enrich_item_metadata_marks_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/demo",
        kind=ItemKind.LINK,
    )

    def fake_extract(url: str, timeout: int):
        raise MetadataExtractionError("boom")

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.FAILED
    assert item.enrichment_error == "boom"


@pytest.mark.django_db
def test_enrich_item_metadata_keeps_feed_ready_items_complete_when_fetch_fails(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/demo",
        title="Already ready",
        kind=ItemKind.LINK,
        enrichment_status=EnrichmentStatus.PROCESSING,
    )

    def fake_extract(url: str, timeout: int):
        raise MetadataExtractionError("boom")

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_error == "boom"


@pytest.mark.django_db
def test_prepare_item_for_enrichment_marks_fully_populated_items_complete() -> None:
    item = Item(
        original_url="https://example.com/demo",
        title="Complete item",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
    )

    prepare_item_for_enrichment(item)

    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_error == ""


@pytest.mark.django_db
def test_recover_processing_items_requeues_stale_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck",
        enrichment_status=EnrichmentStatus.PROCESSING,
    )
    Item.objects.create(
        original_url="https://example.com/pending",
        enrichment_status=EnrichmentStatus.PENDING,
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.enrichment_status == EnrichmentStatus.PENDING
    assert claim_pending_item() is not None


@pytest.mark.django_db
def test_enrich_pending_items_claims_oldest_pending_item(monkeypatch) -> None:
    older = Item.objects.create(
        original_url="https://example.com/older",
        title="",
        shared_at=timezone.now() - timedelta(minutes=1),
    )
    newer = Item.objects.create(
        original_url="https://example.com/newer",
        title="",
        shared_at=timezone.now(),
    )
    prepare_item_for_enrichment(older)
    older.save(update_fields=["enrichment_status"])
    prepare_item_for_enrichment(newer)
    newer.save(update_fields=["enrichment_status"])

    def fake_extract(url: str, timeout: int):
        return extract_metadata_from_html(
            html=f"""
            <html>
              <head>
                <title>{url}</title>
              </head>
            </html>
            """,
            base_url=url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)

    assert enrich_pending_items(limit=1) == 1

    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.enrichment_status == EnrichmentStatus.COMPLETE
    assert newer.enrichment_status == EnrichmentStatus.PENDING
