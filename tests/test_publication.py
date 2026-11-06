from __future__ import annotations

import pytest
from django.db import connection

from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import enrich_item_summaries, enrich_item_transcript
from archive.summaries import GeneratedSummary


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("field", ["short_summary", "long_summary", "tags"])
@pytest.mark.parametrize("generated_flag", [False, True])
def test_summary_publication_preserves_changed_field(monkeypatch, field, generated_flag):
    item = Item.objects.create(original_url="https://example.test/article")

    def generate(item, timeout):
        assert not connection.in_atomic_block
        Item.objects.filter(pk=item.pk).update(
            **{field: "Newer value", f"{field}_generated": generated_flag}
        )
        return GeneratedSummary("Generated short", "Generated long", ("generated",))

    monkeypatch.setattr("archive.services.generate_item_summaries", generate)
    assert enrich_item_summaries(item)
    item.refresh_from_db()
    assert getattr(item, field) == "Newer value"
    assert getattr(item, f"{field}_generated") == generated_flag
    for other in {"short_summary", "long_summary", "tags"} - {field}:
        assert getattr(item, other)
        assert getattr(item, f"{other}_generated") is True
    assert item.summary_status == EnrichmentStatus.COMPLETE


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("conflict", [False, True])
def test_transcript_publication_uses_current_transcript_and_summaries(monkeypatch, conflict):
    item = Item.objects.create(
        original_url="https://example.test/audio.mp3",
        audio_url="https://example.test/audio.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        short_summary="Old short",
        long_summary="Old long",
        tags="old",
        short_summary_generated=True,
        long_summary_generated=True,
        tags_generated=True,
        summary_status=EnrichmentStatus.COMPLETE,
    )

    def transcribe(item, timeout):
        updates = dict(
            short_summary="Manual short",
            long_summary="Manual long",
            tags="manual",
            short_summary_generated=False,
            long_summary_generated=False,
            tags_generated=False,
        )
        if conflict:
            updates.update(transcript="Manual transcript", transcript_generated=False)
        Item.objects.filter(pk=item.pk).update(**updates)
        return "Generated transcript"

    monkeypatch.setattr("archive.services.generate_item_transcript", transcribe)
    assert enrich_item_transcript(item)
    item.refresh_from_db()
    assert item.transcript == ("Manual transcript" if conflict else "Generated transcript")
    assert item.transcript_generated is (not conflict)
    assert item.short_summary == "Manual short"
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.transcript_status == EnrichmentStatus.COMPLETE
