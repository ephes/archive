"""Processing health: per-step outcome counters for monitoring.

The background worker records one event per attempted run of a processing step
(metadata extraction, media archival split into YouTube pages and other sources,
transcription, summaries, article audio). ``processing_health_snapshot`` turns
them into the JSON served at ``/api/health/``: success and failure
counts over a recent window, the latest success and failure per step, and how
long and across how many distinct items a step has been failing without any
success in between. Only step names, counts and timestamps leave this module,
never item titles, URLs or error messages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from archive.media_archival import uses_supported_video_page_source
from archive.models import Item, ProcessingEvent, ProcessingStep, ProcessingStepState

logger = logging.getLogger(__name__)

HEALTH_WINDOW = timedelta(hours=24)
EVENT_RETENTION = timedelta(days=30)


def media_archive_step(item: Item) -> str:
    if uses_supported_video_page_source(item):
        return ProcessingStep.MEDIA_ARCHIVE_YOUTUBE
    return ProcessingStep.MEDIA_ARCHIVE_OTHER


def record_step_outcome(
    step: str,
    *,
    succeeded: bool,
    item_id: int | None,
    now: datetime | None = None,
) -> None:
    """Record one attempted step run; never raises into the processing path."""

    try:
        _record_step_outcome(step, succeeded=succeeded, item_id=item_id, now=now)
    except Exception:
        logger.exception("Could not record processing health for step %s", step)


def _record_step_outcome(
    step: str,
    *,
    succeeded: bool,
    item_id: int | None,
    now: datetime | None,
) -> None:
    occurred_at = now or timezone.now()
    with transaction.atomic():
        state, _ = ProcessingStepState.objects.select_for_update().get_or_create(step=step)
        if succeeded:
            state.last_success_at = occurred_at
            state.failing_since = None
        else:
            state.last_failure_at = occurred_at
            if state.failing_since is None:
                state.failing_since = occurred_at
        state.save()
        ProcessingEvent.objects.create(
            step=step, succeeded=succeeded, item_id=item_id, occurred_at=occurred_at
        )
        _prune_events(cutoff=occurred_at - EVENT_RETENTION)


def _prune_events(*, cutoff: datetime) -> None:
    """Delete events past retention, except those of a step's ongoing failure streak.

    ``failing_items`` counts the failures since ``failing_since``; pruning them would
    report a recovery that never happened.
    """
    streaks = Q()
    for step, failing_since in ProcessingStepState.objects.filter(
        failing_since__isnull=False
    ).values_list("step", "failing_since"):
        streaks |= Q(step=step, occurred_at__gte=failing_since)
    ProcessingEvent.objects.filter(occurred_at__lt=cutoff).exclude(streaks).delete()


def processing_health_snapshot(now: datetime | None = None) -> dict[str, Any]:
    now = now or timezone.now()
    window_start = now - HEALTH_WINDOW
    states = {state.step: state for state in ProcessingStepState.objects.all()}
    counts: dict[tuple[str, bool], int] = {
        (row["step"], row["succeeded"]): row["total"]
        for row in ProcessingEvent.objects.filter(occurred_at__gte=window_start)
        .values("step", "succeeded")
        .annotate(total=Count("id"))
    }

    steps: dict[str, dict[str, Any]] = {}
    for step in ProcessingStep.values:
        state = states.get(step)
        last_success_at = state.last_success_at if state else None
        last_failure_at = state.last_failure_at if state else None
        failing_since = state.failing_since if state else None
        steps[step] = {
            "successes": counts.get((step, True), 0),
            "failures": counts.get((step, False), 0),
            "last_success_at": _isoformat(last_success_at),
            "last_success_age_seconds": _age_seconds(last_success_at, now),
            "last_failure_at": _isoformat(last_failure_at),
            "failing_since": _isoformat(failing_since),
            "failing_for_seconds": _age_seconds(failing_since, now) or 0,
            "failing_items": _failing_items(step, failing_since),
        }

    return {
        "generated_at": now.isoformat(),
        "window_hours": int(HEALTH_WINDOW.total_seconds() // 3600),
        "steps": steps,
    }


def _failing_items(step: str, failing_since: datetime | None) -> int:
    if failing_since is None:
        return 0
    return (
        ProcessingEvent.objects.filter(step=step, succeeded=False, occurred_at__gte=failing_since)
        .exclude(item_id__isnull=True)
        .values("item_id")
        .distinct()
        .count()
    )


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))
