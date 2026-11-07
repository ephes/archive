from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from archive.management.commands import reprocess_items
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


def test_huge_selection_is_rejected_before_range_expansion_or_database_read(monkeypatch) -> None:
    def unexpected(*args, **kwargs):
        pytest.fail("oversized selection must fail before expansion or database access")

    monkeypatch.setattr(reprocess_items, "range", unexpected, raising=False)
    monkeypatch.setattr(Item.objects, "in_bulk", unexpected)
    with pytest.raises(CommandError, match="10,000"):
        run("--ids", "1-999999999999", "--only", "summary")


def test_selection_limit_counts_unique_ids_and_checks_before_each_expansion(monkeypatch) -> None:
    assert parse_item_ids(["1-10000", "5000-10000", "1,10000"]) == list(range(1, 10001))
    expanded = []

    def tracked_range(start, stop):
        expanded.append((start, stop))
        assert len(expanded) == 1, "cumulative limit must fail before the next expansion"
        return range(start, stop)

    monkeypatch.setattr(reprocess_items, "range", tracked_range, raising=False)
    with pytest.raises(CommandError, match="10,000"):
        parse_item_ids(["1-7500", "5000-10001"])
    assert expanded == [(1, 7501)]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("stagger", [10**30, 300_000_000_000])
def test_stagger_overflow_is_rejected_before_any_requeue(monkeypatch, dry_run, stagger) -> None:
    items = [failed_summary_article(), failed_summary_article()]
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 1, 1, tzinfo=UTC))

    def unexpected(*args, **kwargs):
        pytest.fail("invalid schedule must fail before any requeue")

    monkeypatch.setattr(reprocess_items, "_requeue", unexpected)
    args = [
        "--ids",
        *(str(item.pk) for item in items),
        "--only",
        "summary",
        "--stagger",
        str(stagger),
        "--hold-article-audio",
    ]
    if dry_run:
        args.append("--dry-run")
    with pytest.raises(CommandError, match="--stagger"):
        run(*args)
    for item in items:
        item.refresh_from_db()
        assert item.summary_retry_count == 4
        assert item.summary_retry_at is None
        assert item.article_audio_status == PENDING


def test_stagger_preflight_checks_last_eligible_retry(monkeypatch) -> None:
    items = [failed_summary_article() for _ in range(3)]
    monkeypatch.setattr(timezone, "now", lambda: datetime(9999, 12, 31, tzinfo=UTC))
    with pytest.raises(CommandError, match="--stagger"):
        run("--ids", *(str(item.pk) for item in items), "--only", "summary", "--stagger", "43200")
    for item in items:
        item.refresh_from_db()
        assert item.summary_retry_count == 4
        assert item.summary_retry_at is None


def test_stagger_preflight_excludes_missing_and_ineligible_items() -> None:
    item = failed_summary_article()
    skipped = failed_summary_article(summary_status=COMPLETE)
    out = run(
        "--ids",
        str(item.pk),
        str(skipped.pk),
        "99999",
        "--only",
        "summary",
        "--stagger",
        str(10**30),
    )
    assert "Re-queued summary for 1 item(s), skipped 2." in out
    item.refresh_from_db()
    assert item.summary_retry_count == 0


@pytest.mark.parametrize("dry_run", [False, True])
def test_zero_stagger_uses_the_same_due_time(monkeypatch, dry_run) -> None:
    items = [failed_summary_article(), failed_summary_article()]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)
    args = ["--ids", *(str(item.pk) for item in items), "--only", "summary"]
    if dry_run:
        args.append("--dry-run")
    out = run(*args)
    assert out.count(f"due {now.isoformat()}") == 2
    for item in items:
        item.refresh_from_db()
        assert item.summary_retry_at == (None if dry_run else now)


def test_concurrent_skip_does_not_consume_a_stagger_slot(monkeypatch) -> None:
    items = [failed_summary_article() for _ in range(3)]
    original_requeue = reprocess_items._requeue
    attempted = []

    def concurrent_requeue(item_id, *args, **kwargs):
        attempted.append(item_id)
        if item_id == items[1].pk:
            Item.objects.filter(pk=item_id).update(summary_status=COMPLETE)
        return original_requeue(item_id, *args, **kwargs)

    monkeypatch.setattr(reprocess_items, "_requeue", concurrent_requeue)
    out = run("--ids", *(str(item.pk) for item in items), "--only", "summary", "--stagger", "60")
    assert attempted == [item.pk for item in items]
    for item in items:
        item.refresh_from_db()
    assert items[2].summary_retry_at - items[0].summary_retry_at == timedelta(seconds=60)
    assert items[1].summary_retry_at is None
    assert "changed concurrently" in out


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


def transcribed_podcast(**overrides) -> Item:
    fields = {
        "kind": ItemKind.PODCAST_EPISODE,
        "original_url": "https://example.com/episode",
        "audio_url": "https://cdn.example.com/episode.mp3",
        "title": "Episode",
        "enrichment_status": COMPLETE,
        "summary_status": COMPLETE,
        "media_archive_status": COMPLETE,
        "transcript_status": COMPLETE,
        "transcript": "Only the first hour",
        "transcript_generated": True,
    }
    fields.update(overrides)
    return Item.objects.create(**fields)


def test_transcript_requeue_sets_a_failed_transcript_back_to_pending() -> None:
    item = transcribed_podcast(
        transcript="",
        transcript_generated=False,
        transcript_status=FAILED,
        transcript_error="Batch transcription job job-1 is incomplete",
    )

    out = run("--ids", str(item.pk), "--only", "transcript")

    item.refresh_from_db()
    assert item.transcript_status == PENDING
    assert item.transcript_error == ""
    assert f"item {item.pk}: transcript failed -> pending" in out
    claimed = claim_pending_item()
    assert claimed is not None and claimed.pk == item.pk
    assert claimed.transcript_status == EnrichmentStatus.PROCESSING
    assert claimed.summary_status == COMPLETE


def test_transcript_regenerate_clears_a_complete_generated_transcript() -> None:
    item = transcribed_podcast()

    skipped = run("--ids", str(item.pk), "--only", "transcript")
    item.refresh_from_db()
    assert "skipped, transcript is complete, not failed" in skipped
    assert item.transcript == "Only the first hour"

    out = run("--ids", str(item.pk), "--only", "transcript", "--regenerate")

    item.refresh_from_db()
    assert item.transcript == ""
    assert item.transcript_generated is False
    assert item.transcript_status == PENDING
    assert "transcript complete -> pending; generated transcript cleared" in out
    assert "Only the first hour" not in out


def test_transcript_regenerate_never_touches_hand_entered_transcripts() -> None:
    manual = transcribed_podcast(transcript="Typed by hand", transcript_generated=False)
    no_source = transcribed_podcast(audio_url="", original_url="https://example.com/notes")

    out = run("--ids", str(manual.pk), str(no_source.pk), "--only", "transcript", "--regenerate")

    assert f"item {manual.pk}: skipped, transcript was not generated by the archive" in out
    assert f"item {no_source.pk}: skipped, transcript has no transcribable media source" in out
    manual.refresh_from_db()
    assert manual.transcript == "Typed by hand"
    assert manual.transcript_status == COMPLETE
    assert not _requeue(manual.pk, "transcript", timezone.now(), False, regenerate=True)
    manual.refresh_from_db()
    assert manual.transcript == "Typed by hand"


def test_transcript_regenerate_dry_run_keeps_the_transcript() -> None:
    item = transcribed_podcast()

    out = run("--ids", str(item.pk), "--only", "transcript", "--regenerate", "--dry-run")

    item.refresh_from_db()
    assert item.transcript == "Only the first hour"
    assert item.transcript_status == COMPLETE
    assert "[dry-run] Would re-queue transcript for 1 item(s), skipped 0." in out


def test_rejects_invalid_option_combinations() -> None:
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "media_archive", "--hold-article-audio")
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "summary", "--stagger", "-1")
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "article_audio", "--stagger", "60")
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "transcript", "--stagger", "60")
    with pytest.raises(CommandError):
        run("--ids", "1", "--only", "summary", "--regenerate")


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
