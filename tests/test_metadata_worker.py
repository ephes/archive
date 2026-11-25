from contextlib import nullcontext
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from archive.management.commands import run_metadata_worker as worker
from archive.models import EnrichmentStatus, Item


@pytest.fixture(autouse=True)
def fake_worker_signals(monkeypatch):
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker.Command, "_item_processing_timeout", lambda *args: nullcontext())


@pytest.mark.django_db
def test_startup_preserves_another_workers_fresh_processing_lease(monkeypatch):
    now = timezone.now()
    fresh = Item.objects.create(
        original_url="https://example.com/fresh",
        enrichment_status=EnrichmentStatus.PROCESSING,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        processing_started_at=now,
    )
    expired = Item.objects.create(
        original_url="https://example.com/expired",
        enrichment_status=EnrichmentStatus.PROCESSING,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        processing_started_at=now - timedelta(seconds=121),
    )
    claimed = []

    def enrich(*, item, **kwargs):
        claimed.append(item.pk)
        item.enrichment_status = EnrichmentStatus.COMPLETE
        item.save(update_fields=["enrichment_status"])
        return True

    monkeypatch.setattr(worker, "enrich_item_metadata", enrich)
    call_command("run_metadata_worker", once=True, stale_processing_after=120)
    fresh.refresh_from_db()
    assert fresh.enrichment_status == EnrichmentStatus.PROCESSING
    assert fresh.processing_started_at == now
    assert claimed == [expired.pk]


@pytest.mark.django_db
@pytest.mark.parametrize("outcomes", [[], [True], [False], [False, True], ["stalled", True]])
def test_once_exit_status_and_continued_processing(monkeypatch, outcomes):
    items = [
        Item.objects.create(original_url=f"https://example.com/{i}") for i in range(len(outcomes))
    ]
    queue = iter([*items, None])
    attempted = []
    recovered = []
    monkeypatch.setattr(worker, "claim_pending_item", lambda **kwargs: next(queue))
    monkeypatch.setattr(
        worker, "recover_processing_item", lambda pk, **kwargs: recovered.append(pk) or 1
    )

    def enrich(*, item, **kwargs):
        outcome = outcomes[len(attempted)]
        attempted.append(item.pk)
        if outcome == "stalled":
            raise worker.ItemProcessingStalled()
        return outcome

    monkeypatch.setattr(worker, "enrich_item_metadata", enrich)
    stdout = StringIO()
    if any(outcome is False or outcome == "stalled" for outcome in outcomes):
        with pytest.raises(CommandError, match="1 unsuccessful item"):
            call_command("run_metadata_worker", once=True, stdout=stdout)
    else:
        call_command("run_metadata_worker", once=True, stdout=stdout)
    assert attempted == [item.pk for item in items]
    assert recovered == ([items[0].pk] if "stalled" in outcomes else [])
    assert f"Processed {len(items)} item(s)." in stdout.getvalue()


@pytest.mark.django_db
def test_continuous_worker_keeps_running_after_failure(monkeypatch):
    command = worker.Command()
    item = Item.objects.create(original_url="https://example.com/failure")
    claims = []

    def claim(**kwargs):
        claims.append(True)
        if len(claims) == 1:
            return item
        command._shutdown_requested.set()
        return None

    monkeypatch.setattr(worker, "claim_pending_item", claim)
    monkeypatch.setattr(worker, "enrich_item_metadata", lambda **kwargs: False)
    call_command(command, limit=1, interval=1)
    assert len(claims) == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("handoff", [False, True])
def test_stall_recovery_matches_the_claimed_processing_pass(monkeypatch, handoff):
    from test_attempt_ownership import idle_item, other_worker_claim

    from archive import services
    from archive.models import ItemKind

    item = idle_item(
        enrichment_status=EnrichmentStatus.PENDING, transcript_status=EnrichmentStatus.PENDING
    )
    attempts = []

    def metadata(item, timeout):
        attempts.append(item.processing_started_at)
        item.enrichment_status = EnrichmentStatus.COMPLETE
        item.kind = ItemKind.PODCAST_EPISODE
        item.audio_url = "https://example.test/audio.mp3"
        item.save(update_fields=["enrichment_status", "kind", "audio_url"])
        if handoff:
            assert other_worker_claim() == item.pk
            raise worker.ItemProcessingStalled()
        return True

    def transcribe(item, timeout):
        assert item.processing_started_at == attempts[0]
        raise worker.ItemProcessingStalled()

    monkeypatch.setattr(services, "_enrich_item_metadata_fields", metadata)
    monkeypatch.setattr(services, "enrich_item_transcript", transcribe)
    with pytest.raises(CommandError):
        call_command("run_metadata_worker", once=True, limit=1)
    item.refresh_from_db()
    if handoff:
        assert item.transcript_status == EnrichmentStatus.PROCESSING
        assert item.processing_started_at != attempts[0]
    else:
        assert item.transcript_status == EnrichmentStatus.PENDING
        assert item.processing_started_at is None


@pytest.mark.django_db(transaction=True)
def test_stall_recovers_claim_that_only_needs_stage_normalization(monkeypatch):
    from test_attempt_ownership import idle_item

    from archive import services

    item = idle_item(media_archive_status=EnrichmentStatus.PENDING)
    observed = []

    def stall(item, timeout):
        observed.append(item.processing_started_at)
        assert item.media_archive_status == EnrichmentStatus.PROCESSING
        raise worker.ItemProcessingStalled()

    monkeypatch.setattr(services, "enrich_item_media_archive", stall)
    with pytest.raises(CommandError):
        call_command("run_metadata_worker", once=True, limit=1)
    assert observed[0] is not None
    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.PENDING
    assert item.processing_started_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("outcome", ["blank_title", "url_title", "failure"])
def test_worker_output_uses_item_identity_without_url_secrets(monkeypatch, caplog, capsys, outcome):
    from archive.metadata import ExtractedMetadata, MetadataExtractionError

    url = "https://USER_SENTINEL:PASS_SENTINEL@example.com/page?private=QUERY_SENTINEL"
    item = Item.objects.create(
        original_url=url,
        title=url if outcome == "url_title" else "",
        enrichment_status=EnrichmentStatus.PENDING,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.COMPLETE,
        article_audio_status=EnrichmentStatus.COMPLETE,
        summary_error=url,
        transcript_error=url,
        media_archive_error=url,
        article_audio_error=url,
    )
    fetched = []

    def fetch(original_url, *, timeout):
        fetched.append(original_url)
        if outcome == "failure":
            raise MetadataExtractionError(f"Synthetic fetch failure for {original_url}")
        return ExtractedMetadata()

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fetch)
    stdout, stderr = StringIO(), StringIO()
    if outcome == "failure":
        with pytest.raises(CommandError, match="1 unsuccessful item"):
            call_command("run_metadata_worker", once=True, limit=1, stdout=stdout, stderr=stderr)
        assert "metadata_status=failed" in stderr.getvalue()
        assert "MetadataExtractionError" in caplog.text
    else:
        call_command("run_metadata_worker", once=True, limit=1, stdout=stdout, stderr=stderr)
        assert f"Completed item {item.pk}" in stdout.getvalue()
    captured = capsys.readouterr()
    output = stdout.getvalue() + stderr.getvalue() + caplog.text + captured.out + captured.err
    assert fetched == [url]
    assert f"Processing item {item.pk}" in stdout.getvalue()
    assert "Processed 1 item(s)." in stdout.getvalue()
    for sentinel in (url, "USER_SENTINEL", "PASS_SENTINEL", "QUERY_SENTINEL"):
        assert sentinel not in output


@pytest.mark.django_db
@pytest.mark.parametrize("due_during_metadata", [False, True])
def test_worker_checks_article_audio_poll_deadline_after_metadata(
    monkeypatch, settings, due_during_metadata
):
    from test_attempt_ownership import idle_item

    from archive import article_audio, services
    from archive.models import ItemKind

    now = timezone.now()
    poll_at = now + timedelta(seconds=60)
    item = idle_item(
        kind=ItemKind.ARTICLE,
        enrichment_status=EnrichmentStatus.PENDING,
        long_summary="Synthetic article summary",
        article_audio_status=EnrichmentStatus.PENDING,
        article_audio_job_id="saved-job",
        article_audio_poll_at=poll_at,
    )
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic-test-token"
    polls = []
    monkeypatch.setattr(services.timezone, "now", lambda: now)

    def finish_metadata(item, timeout):
        nonlocal now
        assert item.article_audio_status == EnrichmentStatus.PENDING
        item.enrichment_status = EnrichmentStatus.COMPLETE
        item.save(update_fields=["enrichment_status"])
        if due_during_metadata:
            now = poll_at
        return True

    def poll(*, job_id, **kwargs):
        polls.append(job_id)
        return article_audio.ArticleAudioJobUpdate(job_id, "running")

    monkeypatch.setattr(services, "_enrich_item_metadata_fields", finish_metadata)
    monkeypatch.setattr(article_audio, "_poll_synthesis_job", poll)
    call_command("run_metadata_worker", once=True, limit=1)
    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_status == EnrichmentStatus.PENDING
    if not due_during_metadata:
        assert polls == []
        assert item.article_audio_poll_at == poll_at
        now = poll_at + timedelta(seconds=1)
        call_command("run_metadata_worker", once=True, limit=1)
        item.refresh_from_db()

    assert polls == ["saved-job"]
    assert item.article_audio_job_id == "saved-job"
    assert item.article_audio_poll_at == now + timedelta(
        seconds=settings.ARCHIVE_ARTICLE_AUDIO_POLL_SECONDS
    )


@pytest.mark.django_db
def test_worker_skips_blank_article_audio_sources_until_edited(monkeypatch):
    from test_attempt_ownership import idle_item

    from archive import article_audio, services
    from archive.models import ItemKind

    older = idle_item(
        original_url="https://example.test/older",
        shared_at=timezone.now() - timedelta(days=1),
        kind=ItemKind.ARTICLE,
        title="A headline alone is insufficient",
        long_summary=" \t\n",
        short_summary="\u2003\xa0\x85",
        notes="\x1f\u3000\r",
        article_audio_job_id=" \t",
        article_audio_status=EnrichmentStatus.PENDING,
    )
    newer = idle_item(
        original_url="https://example.test/newer",
        kind=ItemKind.ARTICLE,
        notes="Usable source",
        article_audio_status=EnrichmentStatus.PENDING,
    )
    submitted = []

    def submit(*, item, timeout):
        assert article_audio.can_generate_article_audio(item)
        submitted.append(item.pk)
        return article_audio.ArticleAudioJobUpdate(f"job-{item.pk}", "queued")

    monkeypatch.setattr(services, "generate_item_article_audio", submit)
    for _ in range(2):
        call_command("run_metadata_worker", once=True, limit=1)
    assert submitted == [newer.pk]
    older.refresh_from_db()
    assert older.article_audio_status == EnrichmentStatus.PENDING
    assert older.processing_started_at is None

    older.notes = "\u2003Now usable\xa0"
    older.save(update_fields=["notes"])
    call_command("run_metadata_worker", once=True, limit=1)
    assert submitted == [newer.pk, older.pk]


@pytest.mark.django_db
def test_worker_polls_saved_article_audio_job_with_blank_sources(monkeypatch, settings):
    from test_attempt_ownership import idle_item

    from archive import article_audio, services
    from archive.models import ItemKind

    now = timezone.now()
    poll_at = now + timedelta(seconds=30)
    item = idle_item(
        kind=ItemKind.ARTICLE,
        notes="\t\u2003",
        article_audio_status=EnrichmentStatus.PENDING,
        article_audio_job_id=" saved-job ",
        article_audio_poll_at=poll_at,
    )
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic-test-token"
    polls = []
    monkeypatch.setattr(services.timezone, "now", lambda: now)

    def poll(*, job_id, **kwargs):
        polls.append(job_id)
        return article_audio.ArticleAudioJobUpdate(job_id, "running")

    monkeypatch.setattr(article_audio, "_poll_synthesis_job", poll)
    call_command("run_metadata_worker", once=True, limit=1)
    assert polls == []
    now = poll_at
    call_command("run_metadata_worker", once=True, limit=1)
    assert polls == ["saved-job"]
    item.refresh_from_db()
    assert item.article_audio_status == EnrichmentStatus.PENDING
    assert item.article_audio_poll_at > now
