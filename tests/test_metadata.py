from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from archive.article_audio import ArticleAudioJobUpdate
from archive.media_archival import ArchivedAudio
from archive.metadata import MetadataExtractionError, extract_metadata_from_html
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import (
    MEDIA_ARCHIVE_RETRY_DELAYS,
    SUMMARY_RETRY_DELAYS,
    claim_pending_item,
    enrich_item_article_audio,
    enrich_item_metadata,
    enrich_item_transcript,
    enrich_pending_items,
    prepare_item_for_enrichment,
    recover_processing_items,
)
from archive.summaries import GeneratedSummary


def stub_archive_audio(monkeypatch, size_bytes: int = 12345) -> None:
    monkeypatch.setattr(
        "archive.services.archive_item_audio",
        lambda item, timeout: ArchivedAudio(
            object_name=f"items/{item.pk}/audio/source.mp3",
            content_type="audio/mpeg",
            size_bytes=size_bytes,
        ),
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
    stub_archive_audio(monkeypatch)
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
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Short generated summary",
            long_summary="Long generated summary with more detail.",
            tags=("radio", "culture", "interview"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript paragraph one.\n\nTranscript paragraph two.",
    )

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
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.transcript == "Transcript paragraph one.\n\nTranscript paragraph two."
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Short generated summary"
    assert item.long_summary == "Long generated summary with more detail."
    assert item.tags == "radio\nculture\ninterview"
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.summary_error == ""


@pytest.mark.django_db
def test_api_items_are_public_immediately_and_join_feed_after_enrichment(
    client,
    settings,
    api_url: str,
    monkeypatch,
) -> None:
    stub_archive_audio(monkeypatch)
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
                <meta property="og:audio" content="https://cdn.example.com/audio.mp3">
              </head>
            </html>
            """,
            base_url=url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="A compact generated summary.",
            long_summary="A longer generated summary for the detail page.",
            tags=("example", "shared", "article"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript from audio.",
    )

    assert enrich_pending_items(limit=1) == 1

    item.refresh_from_db()
    assert item.title == "Extracted title"
    assert item.source == "Example Site"
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.transcript == "Transcript from audio."
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "A compact generated summary."
    assert b"Extracted title" in client.get(reverse("archive:rss-feed")).content
    assert b"A compact generated summary." in client.get(reverse("archive:rss-feed")).content


@pytest.mark.django_db
def test_enrich_item_metadata_marks_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/demo",
        kind=ItemKind.LINK,
        short_summary="Ready",
        long_summary="Already summarized.",
        tags="demo",
        summary_status=EnrichmentStatus.COMPLETE,
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
        short_summary="Ready",
        long_summary="Already summarized.",
        tags="ready",
        summary_status=EnrichmentStatus.COMPLETE,
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
        short_summary="Short",
        long_summary="Long",
        tags="complete",
        transcript="Transcript",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
    )

    prepare_item_for_enrichment(item)

    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_error == ""
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.summary_error == ""
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.transcript_error == ""


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
def test_recover_processing_items_requeues_summary_only_processing_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck-summary",
        title="Existing metadata",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PROCESSING,
        summary_retry_count=1,
        summary_retry_at=timezone.now() + timedelta(minutes=5),
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.enrichment_status == EnrichmentStatus.COMPLETE
    assert stuck.summary_status == EnrichmentStatus.PENDING
    assert stuck.summary_retry_at is None


@pytest.mark.django_db
def test_recover_processing_items_requeues_transcript_processing_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck-transcript.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.transcript_status == EnrichmentStatus.PENDING


@pytest.mark.django_db
def test_recover_processing_items_requeues_media_archive_processing_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck-audio.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        media_archive_status=EnrichmentStatus.PROCESSING,
        media_archive_retry_count=1,
        media_archive_retry_at=timezone.now() + timedelta(minutes=5),
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.media_archive_status == EnrichmentStatus.PENDING
    assert stuck.media_archive_retry_at is None


@pytest.mark.django_db
def test_enrich_item_media_archive_records_archived_audio(monkeypatch) -> None:
    stub_archive_audio(monkeypatch, size_bytes=4321)
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        title="Archived episode",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.media_archive_error == ""
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.archived_audio_content_type == "audio/mpeg"
    assert item.archived_audio_size_bytes == 4321


@pytest.mark.django_db
def test_enrich_item_media_archive_marks_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        title="Archived episode",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
    )
    monkeypatch.setattr(
        "archive.services.archive_item_audio",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("archive boom")),
    )

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.media_archive_error == "archive boom"
    assert item.media_archive_retry_count == 1
    assert item.media_archive_retry_at is not None


@pytest.mark.django_db
def test_failed_media_archive_is_not_retried_before_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-audio.mp3",
        title="Retry later",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_retry_count=1,
        media_archive_retry_at=timezone.now() + timedelta(minutes=1),
    )

    assert claim_pending_item() is None

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED


@pytest.mark.django_db
def test_failed_media_archive_is_retried_after_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-audio-now.mp3",
        title="Retry now",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_retry_count=1,
        media_archive_retry_at=timezone.now() - timedelta(seconds=1),
        media_archive_error="temporary outage",
    )

    claimed = claim_pending_item()

    assert claimed is not None
    assert claimed.pk == item.pk
    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.PROCESSING
    assert item.media_archive_error == ""
    assert item.media_archive_retry_at is None


@pytest.mark.django_db
def test_media_archive_failures_stop_retrying_after_retry_limit(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-limit-audio.mp3",
        title="Retry limit",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
        media_archive_retry_count=len(MEDIA_ARCHIVE_RETRY_DELAYS),
    )
    monkeypatch.setattr(
        "archive.services.archive_item_audio",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("still broken")),
    )

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.media_archive_retry_count == len(MEDIA_ARCHIVE_RETRY_DELAYS) + 1
    assert item.media_archive_retry_at is None
    assert claim_pending_item() is None


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
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary=f"Summary for {item.original_url}",
            long_summary=f"Long summary for {item.original_url}",
            tags=("queued", "summary", "test"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript",
    )

    assert enrich_pending_items(limit=1) == 1

    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.enrichment_status == EnrichmentStatus.COMPLETE
    assert older.transcript_status == EnrichmentStatus.COMPLETE
    assert older.summary_status == EnrichmentStatus.COMPLETE
    assert newer.enrichment_status == EnrichmentStatus.PENDING
    assert newer.summary_status == EnrichmentStatus.PENDING


@pytest.mark.django_db
def test_enrich_pending_items_runs_summary_backfill_without_refetching_metadata(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/backfill",
        title="Already enriched",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PENDING,
    )

    def unexpected_extract(url: str, timeout: int):
        raise AssertionError("metadata extraction should be skipped for summary backfill")

    monkeypatch.setattr("archive.services.extract_metadata_from_url", unexpected_extract)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Backfilled short summary",
            long_summary="Backfilled long summary",
            tags=("backfill", "summary", "test"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript for the item.",
    )

    assert enrich_pending_items(limit=1) == 1

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Backfilled short summary"


@pytest.mark.django_db
def test_enrich_item_metadata_keeps_manual_generated_values(monkeypatch) -> None:
    stub_archive_audio(monkeypatch)
    item = Item.objects.create(
        original_url="https://example.com/manual-summary",
        title="Manual summary item",
        short_summary="Manual short summary",
        tags="manual",
    )
    prepare_item_for_enrichment(item)
    item.save(update_fields=["enrichment_status", "summary_status", "summary_error"])

    monkeypatch.setattr(
        "archive.services.extract_metadata_from_url",
        lambda url, timeout: extract_metadata_from_html(
            html="""
            <html>
              <head>
                <meta property="og:site_name" content="Example Site">
                <meta name="author" content="Example Author">
                <meta property="article:published_time" content="2026-03-01T12:34:56+00:00">
                <meta property="og:audio" content="https://cdn.example.com/audio.mp3">
              </head>
            </html>
            """,
            base_url=url,
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Generated short summary should not overwrite",
            long_summary="Generated long summary should fill only the missing field.",
            tags=("generated", "manual", "test"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Generated transcript",
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.short_summary == "Manual short summary"
    assert item.long_summary == "Generated long summary should fill only the missing field."
    assert item.tags == "manual"
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.transcript == "Generated transcript"


@pytest.mark.django_db
def test_transcript_requeues_and_refreshes_generated_summaries(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        title="Transcript refresh",
        kind=ItemKind.PODCAST_EPISODE,
        short_summary="Old generated short summary",
        long_summary="Old generated long summary.",
        tags="old\ngenerated",
        short_summary_generated=True,
        long_summary_generated=True,
        tags_generated=True,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Fresh transcript text.",
    )
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Improved short summary",
            long_summary="Improved long summary",
            tags=("improved", "transcript", "refresh"),
        ),
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.transcript == "Fresh transcript text."
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Improved short summary"
    assert item.long_summary == "Improved long summary"
    assert item.tags == "improved\ntranscript\nrefresh"


@pytest.mark.django_db
def test_transcript_does_not_overwrite_manual_summary_fields(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/manual.mp3",
        title="Manual fields",
        kind=ItemKind.PODCAST_EPISODE,
        short_summary="Manual short summary",
        long_summary="Manual long summary",
        tags="manual",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Fresh transcript text.",
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.transcript == "Fresh transcript text."
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Manual short summary"
    assert item.long_summary == "Manual long summary"
    assert item.tags == "manual"


@pytest.mark.django_db
def test_enrich_item_metadata_marks_summary_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/summary-failure",
        title="Summary failure item",
        source="Example Site",
        author="Example Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("bad prompt")),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript text",
    )

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.summary_status == EnrichmentStatus.FAILED
    assert "bad prompt" in item.summary_error
    assert item.summary_retry_count == 1
    assert item.summary_retry_at is not None


@pytest.mark.django_db
def test_failed_summary_is_not_retried_before_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-window",
        title="Retry later",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.FAILED,
        summary_retry_count=1,
        summary_retry_at=timezone.now() + timedelta(minutes=1),
        transcript_status=EnrichmentStatus.COMPLETE,
    )

    assert claim_pending_item() is None

    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.FAILED


@pytest.mark.django_db
def test_failed_summary_is_retried_after_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-now",
        title="Retry now",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.FAILED,
        summary_retry_count=1,
        summary_retry_at=timezone.now() - timedelta(seconds=1),
        summary_error="temporary outage",
        transcript_status=EnrichmentStatus.COMPLETE,
    )

    claimed = claim_pending_item()

    assert claimed is not None
    assert claimed.pk == item.pk
    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.PROCESSING
    assert item.summary_error == ""
    assert item.summary_retry_at is None


@pytest.mark.django_db
def test_summary_failures_stop_retrying_after_retry_limit(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-limit",
        title="Retry limit",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PROCESSING,
        summary_retry_count=len(SUMMARY_RETRY_DELAYS),
        transcript_status=EnrichmentStatus.COMPLETE,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("still broken")),
    )
    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.FAILED
    assert item.summary_retry_count == len(SUMMARY_RETRY_DELAYS) + 1
    assert item.summary_retry_at is None
    assert claim_pending_item() is None


@pytest.mark.django_db
def test_enrich_item_transcript_marks_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/failure.mp3",
        title="Transcript failure",
        kind=ItemKind.PODCAST_EPISODE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("bad audio")),
    )

    assert enrich_item_transcript(item) is False

    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.transcript_error == "bad audio"


@pytest.mark.django_db
def test_enrich_item_article_audio_records_pending_job(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        title="Article headline",
        short_summary="Short summary",
        long_summary="Long summary for audio.",
        kind=ItemKind.ARTICLE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        article_audio_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_article_audio",
        lambda item, timeout: ArticleAudioJobUpdate(job_id="job-123", state="queued"),
    )

    assert enrich_item_article_audio(item) is True

    item.refresh_from_db()
    assert item.article_audio_status == EnrichmentStatus.PENDING
    assert item.article_audio_job_id == "job-123"
    assert item.article_audio_poll_at is not None


@pytest.mark.django_db
def test_enrich_pending_items_completes_generated_article_audio(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        title="Article headline",
        short_summary="Short summary",
        long_summary="Long summary for audio.",
        kind=ItemKind.ARTICLE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        article_audio_status=EnrichmentStatus.PENDING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_article_audio",
        lambda item, timeout: ArticleAudioJobUpdate(
            job_id="job-123",
            state="succeeded",
            artifact_path="/v1/jobs/job-123/artifacts/speech.mp3",
        ),
    )

    assert enrich_pending_items(limit=1) == 1

    item.refresh_from_db()
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_generated is True
    assert item.article_audio_artifact_path == "/v1/jobs/job-123/artifacts/speech.mp3"
