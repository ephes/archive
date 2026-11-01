from __future__ import annotations

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from archive.management.commands.reprocess_items import (
    HOLD_ARTICLE_AUDIO_ERROR,
    _requeue,
    parse_item_ids,
)
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import claim_pending_item, enrich_item_article_audio

pytestmark = pytest.mark.django_db

FAILED = EnrichmentStatus.FAILED
PENDING = EnrichmentStatus.PENDING
COMPLETE = EnrichmentStatus.COMPLETE


def run(*args):
    out = io.StringIO()
    call_command("reprocess_items", *args, stdout=out)
    return out.getvalue()


def failed_summary_article(**overrides) -> Item:
    fields = {
        "kind": ItemKind.ARTICLE,
        "original_url": "https://example.com/article",
        "title": "Article",
        "notes": "Some notes",
        "enrichment_status": COMPLETE,
        "media_archive_status": COMPLETE,
        "transcript_status": COMPLETE,
        "summary_status": FAILED,
        "summary_error": "Summary API request failed: HTTP 429",
        "summary_retry_count": 4,
        "article_audio_status": PENDING,
    }
    fields.update(overrides)
    return Item.objects.create(**fields)


def failed_media_video(**overrides) -> Item:
    fields = {
        "kind": ItemKind.VIDEO,
        "original_url": "https://www.p99conf.io/session/talk/",
        "media_url": "https://www.youtube.com/watch?v=abc123def45",
        "title": "Talk",
        "enrichment_status": COMPLETE,
        "transcript_status": COMPLETE,
        "summary_status": COMPLETE,
        "media_archive_status": FAILED,
        "media_archive_error": "HTTP Error 403: Forbidden",
        "media_archive_retry_count": 4,
    }
    fields.update(overrides)
    return Item.objects.create(**fields)


def test_parse_item_ids_accepts_ids_ranges_and_commas() -> None:
    assert parse_item_ids(["318", "152-154", "153,7"]) == [7, 152, 153, 154, 318]


@pytest.mark.parametrize("token", ["abc", "5-3", "0", "-4", "3-x"])
def test_parse_item_ids_rejects_invalid_tokens(token) -> None:
    with pytest.raises(CommandError):
        parse_item_ids([token])


def test_summary_requeue_resets_only_the_summary_retry_state() -> None:
    item = failed_summary_article(
        transcript_status=FAILED, transcript_error="Transcription timed out"
    )
    before = timezone.now()

    out = run("--ids", str(item.pk), "--only", "summary")

    item.refresh_from_db()
    assert item.summary_status == FAILED
    assert item.summary_retry_count == 0
    assert before <= item.summary_retry_at <= timezone.now()
    # Other steps stay exactly as they were.
    assert item.transcript_status == FAILED
    assert item.transcript_error == "Transcription timed out"
    assert item.article_audio_status == PENDING
    assert item.enrichment_status == COMPLETE
    assert f"item {item.pk}: summary failed (retries 4) -> queued" in out
    assert "Re-queued summary for 1 item(s), skipped 0." in out


def test_requeued_summary_is_claimed_without_metadata_or_transcript() -> None:
    item = failed_summary_article(transcript_status=FAILED)
    run("--ids", str(item.pk), "--only", "summary")

    claimed = claim_pending_item()

    assert claimed is not None and claimed.pk == item.pk
    assert claimed.summary_status == EnrichmentStatus.PROCESSING
    assert claimed.summary_error == ""
    assert claimed.enrichment_status == COMPLETE
    assert claimed.transcript_status == FAILED


def test_stagger_spaces_the_retry_times_of_queued_items() -> None:
    first = failed_summary_article()
    skipped = failed_summary_article(summary_status=COMPLETE, article_audio_status=COMPLETE)
    second = failed_summary_article()
    third = failed_summary_article()

    run("--ids", f"{first.pk}-{third.pk}", "--only", "summary", "--stagger", "60")

    first.refresh_from_db()
    second.refresh_from_db()
    third.refresh_from_db()
    assert second.summary_retry_at - first.summary_retry_at == timedelta(seconds=60)
    assert third.summary_retry_at - first.summary_retry_at == timedelta(seconds=120)
    # Only the first is due now; the others become claimable later.
    claimed = claim_pending_item()
    assert claimed is not None and claimed.pk == first.pk
    assert claim_pending_item() is None
    skipped.refresh_from_db()
    assert skipped.summary_retry_at is None


def test_skips_items_whose_step_is_not_failed_and_missing_items() -> None:
    complete = failed_summary_article(summary_status=COMPLETE, summary_retry_count=0)
    pending = failed_summary_article(summary_status=PENDING, summary_retry_count=0)

    out = run("--ids", str(complete.pk), str(pending.pk), "99999", "--only", "summary")

    assert f"item {complete.pk}: skipped, summary is complete, not failed" in out
    assert f"item {pending.pk}: skipped, summary is pending, not failed" in out
    assert "item 99999: skipped, not found" in out
    assert "Re-queued summary for 0 item(s), skipped 3." in out
    pending.refresh_from_db()
    assert pending.summary_retry_at is None


def test_dry_run_changes_nothing_and_prints_no_content() -> None:
    item = failed_summary_article(title="Secret title")

    out = run("--ids", str(item.pk), "--only", "summary", "--dry-run", "--hold-article-audio")

    item.refresh_from_db()
    assert item.summary_retry_count == 4
    assert item.summary_retry_at is None
    assert item.article_audio_status == PENDING
    assert f"[dry-run] item {item.pk}: summary failed (retries 4) -> queued" in out
    assert "article audio pending -> held" in out
    assert "Would re-queue summary for 1 item(s)" in out
    assert "Secret title" not in out
    assert "HTTP 429" not in out
    assert "example.com" not in out


def test_media_archive_requeue_resets_only_media_archival() -> None:
    item = failed_media_video()

    out = run("--ids", str(item.pk), "--only", "media_archive")

    item.refresh_from_db()
    assert item.media_archive_status == FAILED
    assert item.media_archive_retry_count == 0
    assert item.media_archive_retry_at is not None
    assert item.summary_status == COMPLETE
    assert item.enrichment_status == COMPLETE
    assert "media_archive failed (retries 4) -> queued" in out
    claimed = claim_pending_item()
    assert claimed is not None and claimed.pk == item.pk
    assert claimed.media_archive_status == EnrichmentStatus.PROCESSING
    assert claimed.enrichment_status == COMPLETE


def test_media_archive_skips_items_without_an_archivable_source() -> None:
    item = failed_media_video(media_url="", original_url="https://example.com/page")

    out = run("--ids", str(item.pk), "--only", "media_archive")

    assert "skipped, media archival has nothing to archive" in out
    item.refresh_from_db()
    assert item.media_archive_retry_count == 4


def test_hold_article_audio_blocks_text_to_speech_after_summary() -> None:
    article = failed_summary_article()
    submitted = failed_summary_article(article_audio_job_id="job-1")

    out = run(
        "--ids", str(article.pk), str(submitted.pk), "--only", "summary", "--hold-article-audio"
    )

    article.refresh_from_db()
    submitted.refresh_from_db()
    assert article.article_audio_status == FAILED
    assert article.article_audio_error == HOLD_ARTICLE_AUDIO_ERROR
    assert article.summary_retry_count == 0
    # An already submitted job keeps polling.
    assert submitted.article_audio_status == PENDING
    assert "article audio held for 1." in out


def test_article_audio_release_requeues_held_audio() -> None:
    article = failed_summary_article(
        summary_status=COMPLETE,
        summary_retry_count=0,
        short_summary="Short",
        long_summary="Long",
        tags="tag",
        article_audio_status=FAILED,
        article_audio_error=HOLD_ARTICLE_AUDIO_ERROR,
    )

    out = run("--ids", str(article.pk), "--only", "article_audio")

    article.refresh_from_db()
    assert article.article_audio_status == PENDING
    assert article.article_audio_error == ""
    assert article.article_audio_poll_at is None
    assert f"item {article.pk}: article_audio failed -> pending" in out


def test_article_audio_skips_items_with_an_earlier_synthesis_job() -> None:
    article = failed_summary_article(article_audio_status=FAILED, article_audio_job_id="job-1")

    out = run("--ids", str(article.pk), "--only", "article_audio")

    assert "skipped, article audio has an earlier synthesis job" in out
    article.refresh_from_db()
    assert article.article_audio_status == FAILED


def test_article_audio_skips_non_articles() -> None:
    item = failed_media_video(article_audio_status=FAILED)

    out = run("--ids", str(item.pk), "--only", "article_audio")

    assert "skipped, article audio not applicable" in out


def test_rejects_invalid_option_combinations() -> None:
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "media_archive", "--hold-article-audio")
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "summary", "--stagger", "-1")
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "article_audio", "--stagger", "60")


def test_skips_items_the_worker_is_processing() -> None:
    item = failed_summary_article(enrichment_status=EnrichmentStatus.PENDING)
    claimed = claim_pending_item()
    assert claimed is not None and claimed.pk == item.pk

    out = run("--ids", str(item.pk), "--only", "summary", "--hold-article-audio")

    assert "skipped, the worker is processing it" in out
    item.refresh_from_db()
    assert item.summary_retry_count == 4
    assert item.article_audio_status == PENDING


def test_requeue_does_nothing_when_the_worker_claims_the_item_meanwhile() -> None:
    item = failed_summary_article()
    Item.objects.filter(pk=item.pk).update(enrichment_status=EnrichmentStatus.PROCESSING)

    assert not _requeue(item.pk, "summary", timezone.now(), True)

    item.refresh_from_db()
    assert item.summary_retry_count == 4
    assert item.article_audio_status == PENDING


def test_hold_survives_a_worker_finishing_a_failed_summary_pass() -> None:
    # The worker saved the failed summary and is about to run the article audio step
    # with its in-memory item when the hold lands.
    item = failed_summary_article()
    worker_copy = Item.objects.get(pk=item.pk)

    run("--ids", str(item.pk), "--only", "summary", "--hold-article-audio")
    enrich_item_article_audio(worker_copy)

    item.refresh_from_db()
    assert item.article_audio_status == FAILED
    assert item.article_audio_error == HOLD_ARTICLE_AUDIO_ERROR
