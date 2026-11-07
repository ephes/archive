import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from archive.models import Item, ProcessingEvent, ProcessingFailureItem, ProcessingStepState
from archive.processing_health import processing_health_snapshot, record_step_outcome

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=40)


@pytest.mark.django_db
@pytest.mark.parametrize("limit", [0, -1, -200])
def test_limit_rejected_before_database_or_storage_work(
    limit, django_assert_num_queries, monkeypatch
):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid limit reached storage")

    monkeypatch.setattr("archive.services.delete_unreferenced_archive_media_paths", unexpected)
    with django_assert_num_queries(0), pytest.raises(CommandError, match="strictly positive"):
        call_command(
            "reclassify_items",
            "--all",
            "--limit",
            str(limit),
            "--apply",
            "--normalize-downstream",
            stdout=StringIO(),
        )


@pytest.mark.django_db
@pytest.mark.parametrize("limit, count", [(1, 1), (None, 3)])
def test_positive_and_omitted_limit_are_deterministic(limit, count):
    items = [Item.objects.create(original_url=f"https://example.com/{n}") for n in range(3)]
    args = ["--all", "--apply"]
    if limit is not None:
        args += ["--limit", str(limit)]
    output = StringIO()
    call_command("reclassify_items", *args, stdout=output)
    assert list(
        Item.objects.exclude(classification_rule="").order_by("id").values_list("pk", flat=True)
    ) == [item.pk for item in items[:count]]
    assert f"inspected={count}" in output.getvalue()


@pytest.mark.django_db
def test_thousands_of_old_retries_compact_without_losing_exact_streaks():
    ProcessingStepState.objects.create(step="summary", failing_since=OLD, last_failure_at=OLD)
    ProcessingFailureItem.objects.bulk_create(
        [ProcessingFailureItem(step="summary", item_id=pk) for pk in (1, 2, 3)]
    )
    ProcessingEvent.objects.bulk_create(
        [
            ProcessingEvent(step="summary", succeeded=False, item_id=n % 3 + 1, occurred_at=OLD)
            for n in range(3000)
        ]
    )
    record_step_outcome("summary", succeeded=False, item_id=1, now=NOW - timedelta(hours=1))
    record_step_outcome("summary", succeeded=False, item_id=None, now=NOW)
    snapshot = processing_health_snapshot(NOW)["steps"]["summary"]
    assert ProcessingEvent.objects.count() == 2
    assert ProcessingFailureItem.objects.count() == 3
    assert snapshot["failing_items"] == 3
    assert snapshot["failing_since"] == OLD.isoformat()
    assert snapshot["failures"] == 2
    assert snapshot["last_failure_at"] == NOW.isoformat()
    # Re-importing the service cannot discard durable membership.
    import importlib

    from archive import processing_health

    importlib.reload(processing_health)
    assert processing_health.processing_health_snapshot(NOW)["steps"]["summary"] == snapshot
    record_step_outcome("summary", succeeded=True, item_id=9, now=NOW)
    assert ProcessingFailureItem.objects.count() == 0
    assert processing_health_snapshot(NOW)["steps"]["summary"]["failing_items"] == 0
    record_step_outcome("summary", succeeded=False, item_id=4, now=NOW + timedelta(seconds=1))
    assert list(ProcessingFailureItem.objects.values_list("item_id", flat=True)) == [4]
    assert processing_health_snapshot(NOW)["steps"]["summary"]["failing_items"] == 1


def test_membership_migration_backfills_active_streak_and_prunes_old_attempts(tmp_path):
    script = r"""
import django
import importlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

django.setup()
from archive.models import ProcessingEvent, ProcessingFailureItem
from archive.processing_health import processing_health_snapshot
NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
OLD = NOW - timedelta(days=40)
before = ("archive", "0017_seed_youtube_media_archive_success")
after = ("archive", "0018_processingfailureitem")
executor = MigrationExecutor(connection)
executor.migrate([before])
old_apps = executor.loader.project_state([before]).apps
State = old_apps.get_model("archive", "ProcessingStepState")
Event = old_apps.get_model("archive", "ProcessingEvent")
State.objects.all().delete()
State.objects.create(step="summary", failing_since=OLD, last_failure_at=NOW)
State.objects.create(step="metadata", last_success_at=NOW)
Event.objects.bulk_create([
    Event(step="summary", succeeded=False, item_id=1, occurred_at=OLD),
    Event(step="summary", succeeded=False, item_id=1, occurred_at=OLD),
    Event(step="summary", succeeded=False, item_id=2, occurred_at=NOW),
    Event(step="summary", succeeded=False, item_id=None, occurred_at=OLD),
    Event(step="summary", succeeded=False, item_id=99, occurred_at=OLD-timedelta(days=1)),
    Event(step="metadata", succeeded=False, item_id=88, occurred_at=OLD),
])
executor = MigrationExecutor(connection)
migration = importlib.import_module("archive.migrations.0018_processingfailureitem")
with patch.object(migration.timezone, "now", return_value=NOW):
    executor.migrate([after])
assert set(ProcessingFailureItem.objects.values_list("step", "item_id")) == {
    ("summary", 1), ("summary", 2)}
assert ProcessingEvent.objects.count() == 1
snapshot = processing_health_snapshot(NOW)["steps"]["summary"]
assert snapshot["failing_items"] == 2
assert snapshot["failing_since"] == OLD.isoformat()
assert snapshot["failures"] == 1
# Rolling back keeps distinct streak evidence without duplicating recent events.
executor = MigrationExecutor(connection)
executor.migrate([before])
assert set(Event.objects.filter(step="summary", succeeded=False,
    occurred_at__gte=OLD).values_list("item_id", flat=True)) == {1, 2}
assert Event.objects.filter(occurred_at__gte=NOW-timedelta(hours=24)).count() == 1
# Reapplying is lossless for the same memberships.
executor = MigrationExecutor(connection)
with patch.object(migration.timezone, "now", return_value=NOW):
    executor.migrate([after])
assert set(ProcessingFailureItem.objects.values_list("step", "item_id")) == {
    ("summary", 1), ("summary", 2)}
connections.close_all()
"""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DJANGO_DB_PATH": str(tmp_path / "migration.sqlite3"),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_backdated_failure_outside_current_streak_does_not_add_member():
    record_step_outcome("summary", succeeded=False, item_id=1, now=NOW)
    record_step_outcome("summary", succeeded=False, item_id=2, now=NOW - timedelta(seconds=1))
    assert processing_health_snapshot(NOW)["steps"]["summary"]["failing_items"] == 1
