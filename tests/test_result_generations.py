from datetime import timedelta

import pytest
from django.utils import timezone
from test_attempt_ownership import idle_item

from archive import services
from archive.models import EnrichmentStatus as Status
from archive.models import Item, ItemKind, ProcessingEvent
from archive.summaries import GeneratedSummary

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("stage", ["summary", "transcript"])
@pytest.mark.parametrize("change", ["source", "input", "attempt", "settled"])
def test_old_generated_result_cannot_publish_into_changed_generation(monkeypatch, stage, change):
    item = idle_item(
        kind=ItemKind.PODCAST_EPISODE,
        audio_url="https://example.test/old.mp3",
        **{f"{stage}_status": Status.PROCESSING},
    )
    updates = {"original_url": "https://example.test/new"}
    if change == "input":
        updates = (
            {"transcript": "Current transcript"}
            if stage == "summary"
            else {"audio_url": "https://example.test/new.mp3", "kind": ItemKind.VIDEO}
        )
    elif change == "attempt":
        updates = {"processing_started_at": timezone.now() + timedelta(seconds=1)}
    elif change == "settled":
        updates = {f"{stage}_status": Status.COMPLETE, "processing_started_at": None}

    def generate(**kwargs):
        Item.objects.filter(pk=item.pk).update(**updates)
        return (
            GeneratedSummary("Old short", "Old long", ("old",))
            if stage == "summary"
            else "Old words"
        )

    monkeypatch.setattr(
        services, f"generate_item_{'summaries' if stage == 'summary' else 'transcript'}", generate
    )
    assert getattr(services, f"enrich_item_{'summaries' if stage == 'summary' else 'transcript'}")(
        item
    )
    item.refresh_from_db()
    assert not item.short_summary
    assert not item.tags
    assert item.transcript == updates.get("transcript", "")
    assert getattr(item, f"{stage}_status") == (
        Status.PENDING
        if change in {"source", "input"}
        else updates.get(f"{stage}_status", Status.PROCESSING)
    )
    assert not ProcessingEvent.objects.filter(item_id=item.pk).exists()


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("change", ["source", "attempt", "success", "unchanged"])
def test_metadata_settles_only_its_url_and_attempt(monkeypatch, failure, change):
    from archive.metadata import ExtractedMetadata

    item = idle_item(enrichment_status=Status.PROCESSING)
    updates = {}
    if change == "source":
        updates = {"original_url": "https://example.test/new"}
    elif change == "attempt":
        updates = {"processing_started_at": timezone.now() + timedelta(seconds=1)}
    elif change == "success":
        updates = {
            "enrichment_status": Status.COMPLETE,
            "enrichment_error": "current",
            "title": "Current",
        }

    def extract(url, **kwargs):
        assert url == item.original_url
        Item.objects.filter(pk=item.pk).update(**updates) if updates else None
        if failure:
            raise OSError("synthetic current failure")
        return ExtractedMetadata(
            title="Old title", source="Old source", audio_url="https://example.test/old.mp3"
        )

    monkeypatch.setattr(services, "extract_metadata_from_url", extract)
    result = services._enrich_item_metadata_fields(item)
    item.refresh_from_db()
    if change == "unchanged":
        assert result is not failure
        assert ProcessingEvent.objects.filter(item_id=item.pk, succeeded=not failure).count() == 1
        assert item.title == ("" if failure else "Old title")
        assert item.enrichment_error == ("synthetic current failure" if failure else "")
    else:
        assert result
        assert item.title == updates.get("title", "")
        assert not item.audio_url
        assert item.enrichment_error == updates.get("enrichment_error", "")
        assert item.enrichment_status == (
            Status.PENDING
            if change == "source" and not failure
            else updates.get("enrichment_status", Status.PROCESSING)
        )
        assert not ProcessingEvent.objects.filter(item_id=item.pk).exists()


@pytest.mark.parametrize("stage", ["summary", "transcript"])
@pytest.mark.parametrize("change", ["eligibility", "attempt"])
def test_stale_no_work_instance_cannot_complete_current_work(stage, change, monkeypatch):
    item = idle_item(
        short_summary="Manual",
        long_summary="Manual long",
        tags="manual",
        transcript="Manual words",
        **{f"{stage}_status": Status.PROCESSING},
    )
    updates = {"processing_started_at": timezone.now() + timedelta(seconds=1)}
    if change == "eligibility":
        updates = (
            {"short_summary": "", "long_summary": "", "tags": ""}
            if stage == "summary"
            else {"transcript": "", "audio_url": "https://example.test/new.mp3"}
        )
    Item.objects.filter(pk=item.pk).update(**updates)
    function = (
        services.enrich_item_summaries if stage == "summary" else services.enrich_item_transcript
    )
    assert function(item)
    item.refresh_from_db()
    assert getattr(item, f"{stage}_status") == Status.PROCESSING
    assert item.transcript == updates.get("transcript", "Manual words")
    assert item.short_summary == updates.get("short_summary", "Manual")
    assert not ProcessingEvent.objects.filter(item_id=item.pk).exists()


@pytest.mark.parametrize("stage", ["summary", "transcript", "enrichment"])
@pytest.mark.parametrize("settle_new", [False, True])
def test_recovered_result_cannot_publish_over_reclaimed_attempt(monkeypatch, stage, settle_new):
    from archive.metadata import ExtractedMetadata

    item = idle_item(
        kind=ItemKind.PODCAST_EPISODE,
        audio_url="https://example.test/a.mp3",
        **{f"{stage}_status": Status.PROCESSING},
    )
    old_stamp = item.processing_started_at
    newer = []

    def generate(*args, **kwargs):
        assert services.recover_processing_item(item.pk, expected_started_at=old_stamp) == 1
        claim = services.claim_pending_item()
        assert claim is not None and claim.pk == item.pk
        assert claim.processing_started_at != old_stamp
        if settle_new:
            setattr(claim, f"{stage}_status", Status.COMPLETE)
            claim.save(update_fields=[f"{stage}_status"])
        newer.append(claim)
        return {
            "summary": GeneratedSummary("Old short", "Old long", ("old",)),
            "transcript": "Old words",
            "enrichment": ExtractedMetadata(title="Old title"),
        }[stage]

    generator, enrich = {
        "summary": ("generate_item_summaries", services.enrich_item_summaries),
        "transcript": ("generate_item_transcript", services.enrich_item_transcript),
        "enrichment": ("extract_metadata_from_url", services._enrich_item_metadata_fields),
    }[stage]
    monkeypatch.setattr(services, generator, generate)
    assert enrich(item)
    item.refresh_from_db()
    assert not item.short_summary and not item.transcript and not item.title
    assert getattr(item, f"{stage}_status") == (
        Status.COMPLETE if settle_new else Status.PROCESSING
    )
    assert item.processing_started_at == newer[0].processing_started_at
    assert not ProcessingEvent.objects.filter(item_id=item.pk).exists()


def test_url_changed_metadata_can_be_enriched_by_a_stable_retry(monkeypatch):
    from archive.metadata import ExtractedMetadata

    item = idle_item(enrichment_status=Status.PROCESSING)
    fetched = []

    def stale(url, **kwargs):
        fetched.append(url)
        Item.objects.filter(pk=item.pk).update(original_url="https://example.test/current")
        return ExtractedMetadata(title="Obsolete title", kind_hint="video")

    monkeypatch.setattr(services, "extract_metadata_from_url", stale)
    assert services._enrich_item_metadata_fields(item)
    item.refresh_from_db()
    assert item.enrichment_status == Status.PENDING
    assert item.kind == ItemKind.LINK
    assert not item.classification_evidence

    def current(url, **kwargs):
        fetched.append(url)
        return ExtractedMetadata(title="Current title")

    monkeypatch.setattr(services, "extract_metadata_from_url", current)
    assert services._enrich_item_metadata_fields(item)
    item.refresh_from_db()
    assert item.title == "Current title"
    assert item.enrichment_status == Status.COMPLETE
    assert fetched == ["https://example.test/item", "https://example.test/current"]
    assert ProcessingEvent.objects.filter(item_id=item.pk, succeeded=True).count() == 1
