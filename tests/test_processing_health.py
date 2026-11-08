from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.db import connection
from test_media_publication import fake_video_download

from archive.models import (
    Item,
    ItemKind,
    ProcessingEvent,
    ProcessingFailureItem,
    ProcessingStep,
    ProcessingStepState,
)
from archive.processing_health import (
    media_archive_step,
    processing_health_snapshot,
    record_step_outcome,
)
from archive.services import enrich_item_media_archive, enrich_item_metadata

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
YOUTUBE = ProcessingStep.MEDIA_ARCHIVE_YOUTUBE
STEP_KEYS = {
    "successes",
    "failures",
    "last_success_at",
    "last_success_age_seconds",
    "last_failure_at",
    "failing_since",
    "failing_for_seconds",
    "failing_items",
}


def _record(step, succeeded, item_id, hours_ago):
    record_step_outcome(
        step, succeeded=succeeded, item_id=item_id, now=NOW - timedelta(hours=hours_ago)
    )


@pytest.mark.django_db
def test_snapshot_reports_every_step_with_empty_state() -> None:
    snapshot = processing_health_snapshot(now=NOW)

    assert snapshot["generated_at"] == NOW.isoformat()
    assert snapshot["window_hours"] == 24
    assert set(snapshot["steps"]) == set(ProcessingStep.values)
    for step in snapshot["steps"].values():
        assert step == {
            "successes": 0,
            "failures": 0,
            "last_success_at": None,
            "last_success_age_seconds": None,
            "last_failure_at": None,
            "failing_since": None,
            "failing_for_seconds": 0,
            "failing_items": 0,
        }


@pytest.mark.django_db
def test_window_counts_exclude_older_events() -> None:
    _record(YOUTUBE, True, 1, hours_ago=30)
    _record(YOUTUBE, True, 2, hours_ago=5)
    _record(YOUTUBE, False, 3, hours_ago=25)
    _record(YOUTUBE, False, 3, hours_ago=2)
    _record(ProcessingStep.METADATA, False, 4, hours_ago=1)

    steps = processing_health_snapshot(now=NOW)["steps"]

    assert steps["media_archive_youtube"]["successes"] == 1
    assert steps["media_archive_youtube"]["failures"] == 1
    assert steps["metadata"]["failures"] == 1
    assert steps["summary"]["failures"] == 0


@pytest.mark.django_db
def test_failing_since_tracks_failures_after_the_latest_success() -> None:
    _record(YOUTUBE, True, 1, hours_ago=100)
    _record(YOUTUBE, False, 2, hours_ago=50)
    # Retries of the same item count as one failing item.
    _record(YOUTUBE, False, 2, hours_ago=49)
    _record(YOUTUBE, False, 2, hours_ago=47)
    _record(YOUTUBE, False, 3, hours_ago=10)

    youtube = processing_health_snapshot(now=NOW)["steps"]["media_archive_youtube"]

    assert youtube["last_success_at"] == (NOW - timedelta(hours=100)).isoformat()
    assert youtube["last_success_age_seconds"] == 100 * 3600
    assert youtube["last_failure_at"] == (NOW - timedelta(hours=10)).isoformat()
    assert youtube["failing_since"] == (NOW - timedelta(hours=50)).isoformat()
    assert youtube["failing_for_seconds"] == 50 * 3600
    assert youtube["failing_items"] == 2
    assert youtube["failures"] == 1


@pytest.mark.django_db
def test_success_clears_the_failing_state() -> None:
    _record(YOUTUBE, False, 2, hours_ago=50)
    _record(YOUTUBE, False, 3, hours_ago=10)
    _record(YOUTUBE, True, 4, hours_ago=1)

    youtube = processing_health_snapshot(now=NOW)["steps"]["media_archive_youtube"]

    assert youtube["failing_since"] is None
    assert youtube["failing_for_seconds"] == 0
    assert youtube["failing_items"] == 0
    assert youtube["last_success_age_seconds"] == 3600


@pytest.mark.django_db
def test_events_are_pruned_after_retention_but_state_is_kept() -> None:
    _record(YOUTUBE, True, 1, hours_ago=31 * 24)
    _record(ProcessingStep.METADATA, True, 2, hours_ago=0)

    assert not ProcessingEvent.objects.filter(step=YOUTUBE).exists()
    youtube = processing_health_snapshot(now=NOW)["steps"]["media_archive_youtube"]
    assert youtube["last_success_age_seconds"] == 31 * 24 * 3600


@pytest.mark.django_db
def test_pruning_compacts_the_events_of_an_ongoing_failure_streak() -> None:
    _record(YOUTUBE, True, 1, hours_ago=40 * 24)
    _record(YOUTUBE, False, 2, hours_ago=39 * 24)
    _record(YOUTUBE, False, 3, hours_ago=38 * 24)
    _record(ProcessingStep.METADATA, True, 4, hours_ago=37 * 24)
    _record(ProcessingStep.METADATA, False, 5, hours_ago=36 * 24)
    _record(ProcessingStep.METADATA, True, 6, hours_ago=0)

    youtube = processing_health_snapshot(now=NOW)["steps"]["media_archive_youtube"]
    assert youtube["failing_items"] == 2
    assert youtube["failing_for_seconds"] == 39 * 24 * 3600
    # Events before the streak and of recovered steps still age out.
    assert list(ProcessingEvent.objects.values_list("item_id", flat=True)) == [6]
    assert set(
        ProcessingFailureItem.objects.filter(step=YOUTUBE).values_list("item_id", flat=True)
    ) == {2, 3}


@pytest.mark.django_db
def test_recording_errors_do_not_reach_the_processing_path(monkeypatch, caplog) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("archive.processing_health._record_step_outcome", broken)

    record_step_outcome(YOUTUBE, succeeded=True, item_id=1)

    assert "Could not record processing health" in caplog.text


@pytest.mark.django_db
def test_health_endpoint_is_public_and_exposes_counts_only(client) -> None:
    item = Item.objects.create(
        original_url="https://www.youtube.com/watch?v=secret123",
        title="Private looking title",
        kind=ItemKind.VIDEO,
    )
    record_step_outcome(YOUTUBE, succeeded=False, item_id=item.pk)

    response = client.get("/api/health/")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    assert response["Cache-Control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"generated_at", "window_hours", "steps"}
    assert set(payload["steps"]["media_archive_youtube"]) == STEP_KEYS
    assert payload["steps"]["media_archive_youtube"]["failures"] == 1
    assert payload["steps"]["media_archive_youtube"]["failing_items"] == 1
    body = response.content.decode()
    assert "secret123" not in body
    assert "Private looking title" not in body


@pytest.mark.django_db
def test_health_endpoint_rejects_writes(client) -> None:
    assert client.post("/api/health/").status_code == 405


@pytest.mark.django_db
def test_health_endpoint_query_count_is_bounded(client, django_assert_num_queries) -> None:
    for item_id in range(20):
        for step in ProcessingStep.values:
            record_step_outcome(step, succeeded=False, item_id=item_id)

    with django_assert_num_queries(3):
        assert client.get("/api/health/").status_code == 200


@pytest.mark.django_db
def test_media_archive_step_splits_youtube_pages_from_other_sources() -> None:
    youtube_page = Item(original_url="https://youtu.be/demo123", kind=ItemKind.VIDEO)
    youtube_media = Item(
        original_url="https://conference.example/talk",
        media_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
    )
    direct_audio = Item(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )

    assert media_archive_step(youtube_page) == YOUTUBE
    assert media_archive_step(youtube_media) == YOUTUBE
    assert media_archive_step(direct_audio) == ProcessingStep.MEDIA_ARCHIVE_OTHER


@pytest.mark.django_db
def test_youtube_archival_failure_is_recorded(monkeypatch) -> None:
    item = Item.objects.create(original_url="https://youtu.be/demo123", kind=ItemKind.VIDEO)

    def fail(item, timeout):
        raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setattr("archive.services.archive_item_audio", fail)

    assert enrich_item_media_archive(item) is False

    event = ProcessingEvent.objects.get()
    assert (event.step, event.succeeded, event.item_id) == (YOUTUBE, False, item.pk)
    assert ProcessingStepState.objects.get(step=YOUTUBE).failing_since is not None


@pytest.mark.django_db(transaction=True)
def test_other_media_archival_success_is_recorded(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )
    fake_video_download(monkeypatch, item)

    assert enrich_item_media_archive(item) is True

    event = ProcessingEvent.objects.get()
    assert (event.step, event.succeeded) == (ProcessingStep.MEDIA_ARCHIVE_OTHER, True)


@pytest.mark.django_db
def test_media_archival_without_an_attempt_records_nothing() -> None:
    item = Item.objects.create(original_url="https://example.com/article", kind=ItemKind.ARTICLE)

    assert enrich_item_media_archive(item) is True

    assert not ProcessingEvent.objects.exists()


@pytest.mark.django_db
def test_metadata_failure_is_recorded(monkeypatch) -> None:
    item = Item.objects.create(original_url="https://www.example.com/blocked")

    def blocked(url, timeout):
        raise RuntimeError("HTTP 403 while fetching metadata")

    monkeypatch.setattr("archive.services.extract_metadata_from_url", blocked)
    monkeypatch.setattr("archive.services.enrich_item_summaries", lambda **kwargs: True)
    monkeypatch.setattr("archive.services.enrich_item_article_audio", lambda **kwargs: True)

    enrich_item_metadata(item)

    assert ProcessingEvent.objects.filter(
        step=ProcessingStep.METADATA, succeeded=False, item_id=item.pk
    ).exists()


@pytest.mark.django_db
def test_seed_migration_starts_youtube_clock_at_newest_archived_youtube_item() -> None:
    seed = importlib.import_module(
        "archive.migrations.0017_seed_youtube_media_archive_success"
    ).seed_youtube_media_archive_success
    Item.objects.create(
        original_url="https://youtu.be/older",
        archived_audio_path="items/1/audio/source.mp3",
        shared_at=NOW - timedelta(days=20),
    )
    Item.objects.create(
        original_url="https://conference.example/talk",
        media_url="https://www.youtube.com/watch?v=newer",
        archived_audio_path="items/2/audio/source.mp3",
        shared_at=NOW - timedelta(days=5),
    )
    Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        archived_audio_path="items/3/audio/source.mp3",
        shared_at=NOW - timedelta(days=1),
    )
    Item.objects.create(
        original_url="https://youtu.be/unarchived", shared_at=NOW - timedelta(hours=1)
    )

    seed(django_apps, SimpleNamespace(connection=connection))

    state = ProcessingStepState.objects.get(step=YOUTUBE)
    assert state.last_success_at == NOW - timedelta(days=5)
    assert state.failing_since is None


@pytest.mark.django_db
@pytest.mark.parametrize("newer_success", [False, True])
def test_older_outcome_cannot_reverse_newer_projection(newer_success) -> None:
    _record(YOUTUBE, newer_success, 1, hours_ago=1)
    before = processing_health_snapshot(NOW)["steps"][YOUTUBE]
    _record(YOUTUBE, not newer_success, 2, hours_ago=2)
    after = processing_health_snapshot(NOW)["steps"][YOUTUBE]
    for key in STEP_KEYS - {"successes", "failures"}:
        assert after[key] == before[key]
    assert ProcessingEvent.objects.filter(step=YOUTUBE).count() == 2
    assert after["successes"] == after["failures"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize("success_first", [False, True])
def test_equal_timestamp_success_wins_and_failures_remain_events(success_first) -> None:
    for success in (success_first, not success_first):
        record_step_outcome(YOUTUBE, succeeded=success, item_id=1, now=NOW)
    record_step_outcome(YOUTUBE, succeeded=False, item_id=2, now=NOW)
    state = processing_health_snapshot(NOW)["steps"][YOUTUBE]
    assert state["failing_since"] is None
    assert state["failing_items"] == 0
    assert state["last_success_at"] == NOW.isoformat()
    assert state["last_failure_at"] == NOW.isoformat()
    assert ProcessingEvent.objects.filter(step=YOUTUBE).count() == 3


@pytest.mark.django_db
def test_equal_failure_observations_keep_distinct_streak_membership() -> None:
    for item_id in (1, 1, 2):
        record_step_outcome(YOUTUBE, succeeded=False, item_id=item_id, now=NOW)
    assert processing_health_snapshot(NOW)["steps"][YOUTUBE]["failing_items"] == 2


@pytest.mark.parametrize("restart", [False, True])
def test_snapshot_is_one_wal_read_transaction(tmp_path, restart) -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r"""
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from archive.processing_health import processing_health_snapshot, record_step_outcome

call_command('migrate', verbosity=0)
with connection.cursor() as cursor:
    cursor.execute('PRAGMA journal_mode=WAL')
    assert cursor.fetchone()[0] == 'wal'
now = datetime(2026, 11, 7, 12, tzinfo=UTC)
record_step_outcome('summary', succeeded=False, item_id=1, now=now-timedelta(hours=2))
before = processing_health_snapshot(now)
writer = sqlite3.connect(settings.DATABASES['default']['NAME'], timeout=2)
changed = False
restart = sys.argv[1] == 'True'
def interleave(execute, sql, params, many, context):
    global changed
    if not changed and 'SELECT' in sql and 'archive_processingevent' in sql:
        changed = True
        stamp = (now-timedelta(hours=1)).isoformat()
        with writer:
            writer.execute(
                'UPDATE archive_processingstepstate '
                'SET last_success_at=?, failing_since=NULL WHERE step=?', (stamp, 'summary'))
            writer.execute('DELETE FROM archive_processingfailureitem WHERE step=?', ('summary',))
            writer.execute(
                'INSERT INTO archive_processingevent (step,succeeded,item_id,occurred_at) '
                'VALUES (?,?,?,?)', ('summary', True, 2, stamp))
            if restart:
                writer.execute(
                    'UPDATE archive_processingstepstate '
                    'SET last_failure_at=?, failing_since=? WHERE step=?',
                    (stamp, stamp, 'summary'))
                for pk in (2, 3):
                    writer.execute(
                        'INSERT INTO archive_processingfailureitem (step,item_id) VALUES (?,?)',
                        ('summary', pk))
        assert not writer.in_transaction
    return execute(sql, params, many, context)
with connection.execute_wrapper(interleave):
    observed = processing_health_snapshot(now)
assert changed, 'writer interleaving never ran'
assert observed == before, (observed, before)
assert connection.get_autocommit()
after = processing_health_snapshot(now)['steps']['summary']
assert after['successes'] == 1
assert after['failing_items'] == (2 if restart else 0)
# Errors also release the read transaction and restore autocommit.
def broken(execute, sql, params, many, context):
    if 'SELECT' in sql and 'archive_processingevent' in sql:
        raise RuntimeError('synthetic read failure')
    return execute(sql, params, many, context)
try:
    with connection.execute_wrapper(broken):
        processing_health_snapshot(now)
except RuntimeError:
    pass
else:
    raise AssertionError('synthetic failure did not execute')
assert connection.get_autocommit()
assert not connection.connection.in_transaction
writer.close()
connection.close()
"""
    env = os.environ.copy()
    env.update(
        DJANGO_SETTINGS_MODULE="config.settings.test",
        DJANGO_DB_PATH=str(tmp_path / "health.sqlite3"),
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(restart)],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
