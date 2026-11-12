from datetime import timedelta

import pytest
from django.utils import timezone
from test_attempt_ownership import STAGES, idle_item

from archive import services
from archive.models import EnrichmentStatus as Status
from archive.models import Item


@pytest.mark.django_db
def test_stale_recovery_is_bounded_ordered_and_resumable():
    cutoff = timezone.now() - timedelta(hours=1)
    stale = [
        idle_item(
            **{f"{stage}_status": Status.PROCESSING for stage in STAGES},
            processing_started_at=cutoff - timedelta(seconds=i),
            summary_retry_at=cutoff,
            media_archive_retry_at=cutoff,
            article_audio_poll_at=cutoff,
        )
        for i in range(205)
    ]
    fresh = idle_item(
        summary_status=Status.PROCESSING,
        processing_started_at=cutoff + timedelta(microseconds=1),
    )
    legacy = idle_item(summary_status=Status.PROCESSING)
    Item.objects.filter(pk=legacy.pk).update(processing_started_at=None)
    boundary = idle_item(summary_status=Status.PROCESSING, processing_started_at=cutoff)
    ordered = sorted([*stale, boundary], key=lambda row: (row.processing_started_at, row.pk))
    for batch, remaining in enumerate((106, 6, 0)):
        assert services.recover_stale_processing_items(stale_before=cutoff) == min(
            100, 206 - batch * 100
        )
        assert (
            Item.objects.filter(
                pk__in=[row.pk for row in ordered], processing_started_at__isnull=False
            ).count()
            == remaining
        )
        for row in ordered[: min(206, (batch + 1) * 100)]:
            row.refresh_from_db()
            assert row.processing_started_at is None
            assert all(getattr(row, f"{stage}_status") != Status.PROCESSING for stage in STAGES)
            assert (
                row.summary_retry_at
                is row.media_archive_retry_at
                is row.article_audio_poll_at
                is None
            )
    assert services.recover_stale_processing_items(stale_before=cutoff) == 0
    fresh.refresh_from_db()
    legacy.refresh_from_db()
    assert fresh.processing_started_at == cutoff + timedelta(microseconds=1)
    assert fresh.summary_status == legacy.summary_status == Status.PROCESSING


@pytest.mark.django_db
def test_stale_recovery_does_not_reset_a_reclaimed_attempt(monkeypatch):
    cutoff = timezone.now()
    row = idle_item(
        summary_status=Status.PROCESSING, processing_started_at=cutoff - timedelta(hours=2)
    )
    recover = services._recover_processing_items
    interleaved = []
    replacement = cutoff - timedelta(hours=1)  # Still stale, but a different attempt.

    def reclaim(query):
        interleaved.append(True)
        Item.objects.filter(pk=row.pk).update(processing_started_at=replacement)
        return recover(query)

    monkeypatch.setattr(services, "_recover_processing_items", reclaim)
    assert services.recover_stale_processing_items(stale_before=cutoff) == 0
    assert interleaved == [True]
    row.refresh_from_db()
    assert row.processing_started_at == replacement
    assert row.summary_status == Status.PROCESSING
