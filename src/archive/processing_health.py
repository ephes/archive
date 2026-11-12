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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

from archive.media_archival import uses_supported_video_page_source
from archive.models import (
    Item,
    ProcessingEvent,
    ProcessingFailureItem,
    ProcessingStep,
    ProcessingStepState,
)

logger = logging.getLogger(__name__)

HEALTH_WINDOW = timedelta(hours=24)
EVENT_RETENTION = timedelta(days=30)
EVENT_PRUNE_BATCH_SIZE = 100


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
    with transaction.atomic():
        state, _ = ProcessingStepState.objects.select_for_update().get_or_create(step=step)
        # Stamp only after acquiring the write lock, so ordinary observations cannot reorder.
        occurred_at = now or timezone.now()
        watermark = max(
            (stamp for stamp in (state.last_success_at, state.last_failure_at) if stamp),
            default=None,
        )
        # Retain delayed observations as events, but never reverse the current projection.
        # Success wins a timestamp tie; tied failures still deduplicate distinct items.
        accepted = watermark is None or occurred_at >= watermark
        if accepted and succeeded:
            state.last_success_at = occurred_at
            state.failing_since = None
            ProcessingFailureItem.objects.filter(step=step).delete()
        elif accepted:
            state.last_failure_at = occurred_at
            if occurred_at == state.last_success_at:
                # Keep both outcome watermarks deterministic when success wins a tie.
                pass
            elif state.failing_since is None:
                state.failing_since = occurred_at
            if item_id is not None and occurred_at != state.last_success_at:
                ProcessingFailureItem.objects.get_or_create(step=step, item_id=item_id)
        state.save()
        ProcessingEvent.objects.create(
            step=step, succeeded=succeeded, item_id=item_id, occurred_at=occurred_at
        )
        _prune_events(cutoff=occurred_at - EVENT_RETENTION)


def _prune_events(*, cutoff: datetime) -> None:
    """Prune old attempts; exact streak membership lives independently of retries."""
    expired = (
        ProcessingEvent.objects.filter(occurred_at__lt=cutoff)
        .order_by("occurred_at", "pk")
        .values("pk")[:EVENT_PRUNE_BATCH_SIZE]
    )
    ProcessingEvent.objects.filter(pk__in=expired).delete()


def processing_health_snapshot(now: datetime | None = None) -> dict[str, Any]:
    now = now or timezone.now()
    window_start = now - HEALTH_WINDOW
    with _snapshot_read_transaction():
        states = {state.step: state for state in ProcessingStepState.objects.all()}
        counts: dict[tuple[str, bool], int] = {
            (row["step"], row["succeeded"]): row["total"]
            for row in ProcessingEvent.objects.filter(occurred_at__gte=window_start)
            .values("step", "succeeded")
            .annotate(total=Count("id"))
        }

        active_steps = [step for step, state in states.items() if state.failing_since is not None]
        failing_counts = {}
        if active_steps:
            # Membership already enforces each state's cutoff and resets on success.
            failing_counts = {
                row["step"]: row["total"]
                for row in ProcessingFailureItem.objects.filter(step__in=active_steps)
                .values("step")
                .annotate(total=Count("item_id"))
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
            "failing_items": failing_counts.get(step, 0),
        }

    return {
        "generated_at": now.isoformat(),
        "window_hours": int(HEALTH_WINDOW.total_seconds() // 3600),
        "steps": steps,
    }


@contextmanager
def _snapshot_read_transaction() -> Iterator[None]:
    """Use a SQLite read snapshot without the configured IMMEDIATE writer lock."""
    if connection.in_atomic_block:
        # An existing transaction owns its snapshot and its commit/rollback lifecycle.
        yield
        return
    if not connection.get_autocommit():
        with transaction.atomic():
            yield
        return
    connection.set_autocommit(False)
    try:
        with connection.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.set_autocommit(True)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))
