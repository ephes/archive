"""Real historical schemas, isolated from pytest's application database."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_journey(tmp_path, script):
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "DJANGO_DB_PATH": str(tmp_path / "default.sqlite3"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_public_feed_index_populated_upgrade_rollback_and_work(tmp_path):
    run_journey(tmp_path, r'''
import django
from datetime import UTC, datetime, timedelta
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

django.setup()
from archive.models import Item
from archive.views import _public_feed_items

pre = [("archive", "0022_item_transcript_job")]
executor = MigrationExecutor(connection)
latest = executor.loader.graph.leaf_nodes("archive")
executor.migrate(pre)
now = datetime(2026, 11, 1, tzinfo=UTC)
# Deliberately tie publication times, with ineligible rows among the newest ids.
rows = [Item(original_url=f"https://example.test/recent/{n}", title=f"Synthetic {n}",
             published_at=now, is_public=n != 58) for n in range(63)]
rows[59].title = "   "
rows[60].published_at = None
Item.objects.bulk_create(rows)
expected = [row.pk for row in reversed(rows)
            if row.is_public and row.published_at is not None and row.title.strip()][:50]

def selection():
    query = _public_feed_items()[:50]
    sql, params = query.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN QUERY PLAN " + sql, params)
        plan = str(cursor.fetchall())
    ticks = 0
    def progress():
        nonlocal ticks
        ticks += 1
        return 0
    connection.connection.set_progress_handler(progress, 1)
    try:
        ids = [row.pk for row in query]
    finally:
        connection.connection.set_progress_handler(None, 0)
    assert ids == expected
    return plan, ticks

indexed_work = []
for start, stop in ((0, 1000), (1000, 10000)):
    Item.objects.bulk_create([
        Item(original_url=f"https://example.test/history/{n}", title="Old synthetic",
             published_at=now - timedelta(days=1), shared_at=now - timedelta(days=1))
        for n in range(start, stop)
    ])
    total = _public_feed_items().count()
    baseline_plan, baseline_work = selection()
    assert "USE TEMP B-TREE FOR ORDER BY" in baseline_plan, baseline_plan
    MigrationExecutor(connection).migrate(latest)
    plan, work = selection()
    assert "archive_public_feed_order" in plan, plan
    assert "USE TEMP B-TREE" not in plan, plan
    assert work < baseline_work / 3, (work, baseline_work)
    indexed_work.append(work)
    assert _public_feed_items().count() == total
    MigrationExecutor(connection).migrate(pre)
    assert selection()[0] == baseline_plan
    assert _public_feed_items().count() == total
assert indexed_work[1] <= indexed_work[0] * 1.1, indexed_work
MigrationExecutor(connection).migrate(latest)
# Adding/removing the index must leave the populated FTS installation working.
with connection.cursor() as cursor:
    cursor.execute("SELECT count(*) FROM archive_item_fts WHERE archive_item_fts MATCH 'Synthetic'")
    assert cursor.fetchone()[0] == Item.objects.count() - 1
connections.close_all()
''')


def test_public_week_index_populated_upgrade_rollback_and_work(tmp_path):
    run_journey(tmp_path, r'''
import json
from datetime import UTC, datetime, timedelta

import django
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

django.setup()
from archive.models import Item
from archive.views import weekly_items_json

pre = [("archive", "0023_item_archive_public_feed_order")]
executor = MigrationExecutor(connection)
latest = executor.loader.graph.leaf_nodes("archive")
executor.migrate(pre)
now = datetime(2026, 7, 1, 12, tzinfo=UTC)
rows = Item.objects.bulk_create([
    Item(original_url=f"https://example.test/week/{n}", title=f"Synthetic {n}",
         shared_at=now + timedelta(seconds=n // 4),
         published_at=now, is_public=n % 120 == 0 or n == 5881)
    for n in range(8000)
])
expected = [row for row in rows if row.is_public]
assert expected[49].shared_at == expected[50].shared_at
factory = RequestFactory()

def read_week():
    url = "/feeds/week/2026-W27.json"
    ids = []
    measurements = []
    while url:
        ticks = 0
        def progress():
            nonlocal ticks
            ticks += 1
            return 0

        connection.connection.set_progress_handler(progress, 100)
        try:
            with CaptureQueriesContext(connection) as queries:
                response = weekly_items_json(factory.get(url), "2026-W27")
        finally:
            connection.connection.set_progress_handler(None, 0)
        assert response.status_code == 200
        assert len(queries) == 1
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + queries[0]["sql"])
            plan = str(cursor.fetchall())
        measurements.append((plan, ticks))

        payload = json.loads(response.content)
        ids.extend(item["id"] for item in payload["items"])
        url = payload["next"]
        if url:
            assert len(ids) == 50
    assert len(measurements) == 2
    assert ids == [row.pk for row in expected]
    return measurements

baseline = read_week()
MigrationExecutor(connection).migrate(latest)
for (plan, ticks), (baseline_plan, baseline_ticks) in zip(read_week(), baseline, strict=True):
    assert "archive_public_week_order" not in baseline_plan, baseline_plan
    assert "archive_public_week_order" in plan, plan
    assert "USE TEMP B-TREE" not in plan, plan
    assert ticks * 3 < baseline_ticks, (ticks, baseline_ticks)

MigrationExecutor(connection).migrate(pre)
assert [plan for plan, _ in read_week()] == [plan for plan, _ in baseline]
connection.close()
''')


@pytest.mark.parametrize("number", [5, 6, 7, 8, 17])
def test_historical_data_migrations_use_selected_database(tmp_path, number):
    run_journey(tmp_path, r'''
import copy
import importlib
import django
from datetime import UTC, datetime
from django.conf import settings
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

django.setup()
settings.DATABASES["secondary"] = copy.deepcopy(settings.DATABASES["default"])
settings.DATABASES["secondary"]["NAME"] += ".secondary"
number = NUMBER
for alias in ("default", "secondary"):
    executor = MigrationExecutor(connections[alias])
    target = next(key for key in executor.loader.disk_migrations
                  if key[0] == "archive" and key[1].startswith(f"{number:04d}_"))
    executor.migrate([target])
apps = executor.loader.project_state([target]).apps
Item = apps.get_model("archive", "Item")
module = importlib.import_module("archive.migrations." + target[1])
functions = {5: "initialize_transcript_state", 6: "initialize_article_audio_state",
             7: "queue_existing_archivable_audio", 8: "queue_existing_archivable_video",
             17: "seed_youtube_media_archive_success"}
# Same identities, opposing inputs: accidental default reads AND writes are observable.
for alias in ("default", "secondary"):
    selected = alias == "secondary"
    fields = {"original_url": "https://example.test/item", "kind": "link"}
    if number == 5:
        fields.update(transcript="synthetic transcript" if selected else "",
                      transcript_status="pending" if selected else "failed")
    elif number == 6:
        fields.update(article_audio_artifact_path="synthetic.mp3" if selected else "",
                      article_audio_status="pending" if selected else "failed")
    elif number in (7, 8):
        fields.update(original_url="https://example.test/file." +
                      (("mp3" if number == 7 else "mp4?x=1") if selected else "txt"),
                      media_archive_status="complete")
    else:
        fields.update(original_url="https://youtu.be/synthetic",
                      archived_audio_path="synthetic.mp3",
                      shared_at=datetime(2026, 1, 2 if selected else 1, tzinfo=UTC))
    Item.objects.using(alias).create(**fields)
before = list(Item.objects.using("default").values())
if number == 17:
    State = apps.get_model("archive", "ProcessingStepState")
    State.objects.using("default").create(step="media_archive_youtube")
    state_before = list(State.objects.using("default").values())
with connections["secondary"].schema_editor() as editor:
    getattr(module, functions[number])(apps, editor)
assert list(Item.objects.using("default").values()) == before
row = Item.objects.using("secondary").get()
if number == 5:
    assert row.transcript_status == "complete" and row.transcript_generated
elif number == 6:
    assert row.article_audio_status == "complete" and row.article_audio_generated
elif number in (7, 8):
    assert row.media_archive_status == "pending"
else:
    assert list(State.objects.using("default").values()) == state_before
    assert State.objects.using("secondary").get().last_success_at == row.shared_at
connections.close_all()
'''.replace("NUMBER", str(number)))


def test_populated_fts_upgrade_rollback_upgrade(tmp_path):
    run_journey(tmp_path, r'''
import django
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

django.setup()
pre = ("archive", "0010_item_classification_engine_version")
executor = MigrationExecutor(connection)
latest = executor.loader.graph.leaf_nodes("archive")
executor.migrate([pre])
Item = executor.loader.project_state([pre]).apps.get_model("archive", "Item")
content = dict(title="Titlemarker", short_summary="Shortmarker", long_summary="Longmarker",
               notes="Notesmarker café", transcript="Transcriptmarker 日本語")
row = Item.objects.create(original_url="https://example.test/seed", **content)

def migrate(target):
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    return executor.loader.project_state(target).apps.get_model("archive", "Item")

def matches(term, public=False):
    with connection.cursor() as cursor:
        cursor.execute("SELECT f.rowid FROM archive_item_fts f JOIN archive_item i ON i.id=f.rowid "
                       "WHERE archive_item_fts MATCH %s" + (" AND i.is_public" if public else ""),
                       [term])
        return {r[0] for r in cursor.fetchall()}

def exercise(Item):
    assert {k: getattr(Item.objects.get(pk=row.pk), k) for k in content} == content
    for term in ("Titlemarker", "Shortmarker", "Longmarker", "Notesmarker", "Transcriptmarker"):
        assert matches(term) == {row.pk}
    other = Item.objects.create(original_url="https://example.test/new", **content)
    assert matches("Transcriptmarker") == {row.pk, other.pk}
    Item.objects.filter(pk=other.pk).update(is_public=False)
    assert matches("Titlemarker", public=True) == {row.pk}
    Item.objects.filter(pk=other.pk).update(is_public=True, title="Replacementmarker")
    assert matches("Titlemarker") == {row.pk}
    assert matches("Replacementmarker", public=True) == {other.pk}
    other.delete()
    assert matches("Replacementmarker") == set()
    assert matches("Transcriptmarker") == {row.pk}

exercise(migrate(latest))
# Cross every later Item schema change backwards, then remove the FTS installation itself.
exercise(migrate([("archive", "0011_item_search_fts")]))
migrate([pre])
assert {k: getattr(Item.objects.get(pk=row.pk), k) for k in content} == content
exercise(migrate(latest))
connections.close_all()
''')


def test_capture_digest_populated_migration_rehearsal(tmp_path):
    run_journey(tmp_path, r'''
import copy
import django
from django.conf import settings
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

django.setup()
from archive.capture import capture_item
from archive.capture_identity import capture_url_digest
from archive.models import Item as CurrentItem
pre = [("archive", "0019_fts_content_update_guard")]
settings.DATABASES["secondary"] = copy.deepcopy(settings.DATABASES["default"])
settings.DATABASES["secondary"]["NAME"] += ".secondary"
for alias in ("default", "secondary"):
    db = connections[alias]
    executor = MigrationExecutor(db)
    latest = executor.loader.graph.leaf_nodes("archive")
    executor.migrate(pre)
    Item = executor.loader.project_state(pre).apps.get_model("archive", "Item")
    urls = ["HTTPS://USER:Pass@Example.COM:443/Path?x=%31&&y=+#frag",
            "http://[ABCD::1]:80/x", "https://example.com/x?", "https://example.com:bad/legacy"]
    urls += [f"https://example.com/{alias}/{n}" for n in range(501)]
    Item.objects.using(alias).bulk_create([
        Item(original_url=url, title="Migrationmarker", notes="synthetic") for url in urls])
    before = list(Item.objects.using(alias).order_by("pk").values_list(
        "pk", "original_url", "notes"))
    # Upgrade / rollback / upgrade on populated files, preserving FTS triggers.
    for target in (latest, pre, latest):
        executor = MigrationExecutor(db)
        executor.migrate(target)
        Model = executor.loader.project_state(target).apps.get_model("archive", "Item")
        assert list(Model.objects.using(alias).order_by("pk").values_list(
            "pk", "original_url", "notes")) == before
        with db.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM archive_item_fts WHERE archive_item_fts MATCH %s",
                           ["Migrationmarker"])
            assert cursor.fetchone()[0] == len(urls)
        added = Model.objects.using(alias).create(
            original_url="https://example.com/new", title="Insertedmarker")
        with db.cursor() as cursor:
            cursor.execute("SELECT rowid FROM archive_item_fts WHERE archive_item_fts MATCH %s",
                           ["Insertedmarker"])
            assert cursor.fetchall() == [(added.pk,)]
        Model.objects.using(alias).filter(pk=added.pk).update(title="Updatedmarker")
        with db.cursor() as cursor:
            cursor.execute("SELECT rowid FROM archive_item_fts WHERE archive_item_fts MATCH %s",
                           ["Updatedmarker"])
            assert cursor.fetchall() == [(added.pk,)]
        added.delete(using=alias)
        with db.cursor() as cursor:
            cursor.execute("SELECT rowid FROM archive_item_fts WHERE archive_item_fts MATCH %s",
                           ["Updatedmarker"])
            assert cursor.fetchall() == []
        if target == latest:
            assert all(row.capture_url_digest == capture_url_digest(row.original_url)
                       for row in Model.objects.using(alias).all())
            with db.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN SELECT id, original_url FROM archive_item "
                               "WHERE capture_url_digest=%s AND shared_at >= %s "
                               "ORDER BY shared_at DESC, id DESC",
                               [capture_url_digest(urls[0]), "2000-01-01"])
                assert "archive_capture_digest_time" in str(cursor.fetchall())
# Runtime capture finds the pre-existing migrated item without enrichment/network.
existing = CurrentItem.objects.order_by("pk").first()
row, created = capture_item(CurrentItem(original_url=existing.original_url))
assert not created and row.pk == existing.pk
connections.close_all()
''')


def test_capture_digest_migration_accepts_pre_0020_writers(tmp_path):
    run_journey(tmp_path, r'''
import django
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

django.setup()
from archive.capture import capture_item
from archive.models import Item as CurrentItem
executor = MigrationExecutor(connection)
latest = executor.loader.graph.leaf_nodes("archive")
executor.migrate(latest)
pre = [("archive", "0019_fts_content_update_guard")]
Legacy = executor.loader.project_state(pre).apps.get_model("archive", "Item")
# An old writer still running during a migrate-then-restart deploy omits the column.
row = Legacy.objects.create(original_url="https://example.com/legacy-writer", title="Legacy")
assert CurrentItem.objects.get(pk=row.pk).capture_url_digest == ""
# The empty placeholder never matches a real digest: a new capture is not merged.
fresh, created = capture_item(CurrentItem(original_url="https://example.com/legacy-writer"))
assert created and fresh.pk != row.pk
connections.close_all()
''')


def test_legacy_video_migration_bounds_candidate_work(tmp_path):
    run_journey(tmp_path, r'''
import copy
import sqlite3
from unittest.mock import patch
import django
from django.conf import settings
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.db.models.query import QuerySet

django.setup()
pre = [("archive", "0007_item_archived_audio_content_type_and_more")]
target = [("archive", "0008_item_archived_video_content_type_and_more")]
settings.DATABASES["secondary"] = copy.deepcopy(settings.DATABASES["default"])
settings.DATABASES["secondary"]["NAME"] += ".secondary"
for alias in ("default", "secondary"):
    executor = MigrationExecutor(connections[alias])
    executor.migrate(pre)
Item = executor.loader.project_state(pre).apps.get_model("archive", "Item")
Item.objects.using("default").create(
    original_url="https://example.test/untouched.mp4", media_archive_status="complete")
before = list(Item.objects.using("default").values())
rows = []
expected = set()
for n in range(1205):
    fields = dict(original_url=f"https://example.test/{n}.txt", title="Syntheticmarker",
                  media_archive_status="complete")
    case = n % 6
    if case == 0:
        fields["original_url"] = f"https://example.test/{n}.MP4?download=1#part"
    elif case == 1:
        fields["media_url"] = f"https://example.test/{n}.webm"
    elif case == 2:
        fields["original_url"] = f"https://example.test/{n}.mov"
        fields["archived_audio_path"] = "kept.mp3"
    elif case == 3:
        fields["original_url"] = f"https://example.test/{n}.m4v"
        fields["media_archive_status"] = "failed"
    elif case == 4:
        fields["original_url"] += "?name=movie.mp4"
    rows.append(Item(**fields))
Item.objects.using("secondary").bulk_create(rows)
expected = {row.pk for n, row in enumerate(rows) if n % 6 in (0, 1)}
db = connections["secondary"]
sizes, selects, params_seen = [], [], []
fetch_all = QuerySet._fetch_all
def bounded_cache(queryset):
    fetch_all(queryset)
    if queryset.model._meta.label == "archive.Item" and queryset._result_cache is not None:
        sizes.append(len(queryset._result_cache))
def inspect(execute, sql, params, many, context):
    params_seen.append(len(params or ()))
    if sql.startswith("SELECT") and '"archive_item"' in sql:
        selects.append(sql)
    if sql.startswith("UPDATE") and '"media_archive_status"' in sql:
        assert db.in_atomic_block
    return execute(sql, params, many, context)
def no_default(*args):
    raise AssertionError("migration touched default database")
old_limit = db.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 250)
try:
    with patch.object(QuerySet, "_fetch_all", bounded_cache), db.execute_wrapper(inspect), \
            connections["default"].execute_wrapper(no_default):
        executor = MigrationExecutor(db)
        executor.migrate(target)
finally:
    db.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)
assert sizes and max(sizes) <= 200, sizes
assert len(selects) >= 4 and all("LIMIT 200" in sql and "OFFSET" not in sql for sql in selects)
assert max(params_seen) <= 250
assert set(Item.objects.using("secondary").filter(
    media_archive_status="pending").values_list("pk", flat=True)) == expected
for row in rows:
    actual = Item.objects.using("secondary").get(pk=row.pk)
    assert (actual.original_url, actual.media_url, actual.title, actual.archived_audio_path) == (
        row.original_url, row.media_url, row.title, row.archived_audio_path)
assert list(Item.objects.using("default").values()) == before
connections.close_all()
''')


def test_legacy_video_migration_isolates_malformed_urls(tmp_path):
    run_journey(tmp_path, r'''
import contextlib
import io
import django
from django.db import connection, connections
from django.db.migrations.executor import MigrationExecutor

django.setup()
pre = [("archive", "0007_item_archived_audio_content_type_and_more")]
target = [("archive", "0008_item_archived_video_content_type_and_more")]
executor = MigrationExecutor(connection)
executor.migrate(pre)
Item = executor.loader.project_state(pre).apps.get_model("archive", "Item")
cases = [
    ("http://[private-sentinel", "https://example.test/clip.MOV?x=1", "pending"),
    ("http://[private-sentinel", "", "complete"),
    ("http://[private-sentinel", "http://[also-broken", "complete"),
    ("https://example.test/clip.webm", "http://[private-sentinel", "pending"),
    ("https://example.test/story", "http://[private-sentinel", "complete"),
    ("https://example.test/clip.mp4", "", "pending"),
    ("https://example.test/story", "", "complete"),
]
for original, media, status in cases:
    Item.objects.create(original_url=original, media_url=media,
                        media_archive_status="complete", notes="synthetic evidence", kind="link")
output = io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
    MigrationExecutor(connection).migrate(target)
assert "private-sentinel" not in output.getvalue()
for row, (original, media, status) in zip(Item.objects.order_by("pk"), cases, strict=True):
    assert (row.original_url, row.media_url, row.media_archive_status, row.notes, row.kind) == (
        original, media, status, "synthetic evidence", "link")
connections.close_all()
''')


@pytest.mark.parametrize("count", [205, 2205])
def test_failure_streak_downgrade_batches_membership(tmp_path, count):
    run_journey(tmp_path, r'''
import copy
import sqlite3
from datetime import timedelta
import django
from django.conf import settings
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

django.setup()
pre = [("archive", "0017_seed_youtube_media_archive_success")]
target = [("archive", "0018_processingfailureitem")]
settings.DATABASES["secondary"] = copy.deepcopy(settings.DATABASES["default"])
settings.DATABASES["secondary"]["NAME"] += ".secondary"
count = COUNT
now = timezone.now()
since = now - timedelta(days=60)
steps = ("metadata", "summary")
for alias in ("default", "secondary"):
    executor = MigrationExecutor(connections[alias])
    executor.migrate(pre)
    apps = executor.loader.project_state(pre).apps
    State = apps.get_model("archive", "ProcessingStepState")
    Event = apps.get_model("archive", "ProcessingEvent")
    for step in steps:
        State.objects.using(alias).create(step=step, failing_since=since)
    State.objects.using(alias).create(step="transcript")
    Event.objects.using(alias).bulk_create([
        Event(step=step, item_id=n, succeeded=False, occurred_at=since)
        for step in steps for n in range(1, count + 1) for retry in range(2)
    ])
    # Some current failures survive pruning; old failures and successes do not
    # substitute for missing current-streak failures.
    Event.objects.using(alias).bulk_create([
        Event(step=step, item_id=n, succeeded=succeeded, occurred_at=when)
        for step in steps for n in range(1, count + 1)
        for succeeded, when in [(False, since - timedelta(days=1)), (True, now)]
    ] + [Event(step=step, item_id=n, succeeded=False, occurred_at=now)
         for step in steps for n in range(3, count + 1, 3) for retry in range(2)])
    Event.objects.using(alias).create(step="metadata", succeeded=False, occurred_at=now)
    executor = MigrationExecutor(connections[alias])
    executor.migrate(target)
    Member = executor.loader.project_state(target).apps.get_model(
        "archive", "ProcessingFailureItem")
    for step in steps:
        assert set(Member.objects.using(alias).filter(step=step).values_list(
            "item_id", flat=True)) == set(range(1, count + 1))
    # Ignore stale membership for an inactive state.
    Member.objects.using(alias).create(step="transcript", item_id=999999)
before_default = [list(model.objects.using("default").order_by("pk").values())
                  for model in (State, Event, Member)]
db = connections["secondary"]
before_events = list(Event.objects.using("secondary").order_by("pk").values())
queries, params_seen, pages = [], [], []
def inspect(execute, sql, params, many, context):
    queries.append(sql)
    params_seen.append(len(params or ()))
    if sql.startswith("SELECT") and '"archive_processingfailureitem"' in sql:
        pages.append(sql)
    return execute(sql, params, many, context)
def no_default(*args):
    raise AssertionError("downgrade touched default database")
old_limit = db.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
try:
    executor = MigrationExecutor(db)
    with db.execute_wrapper(inspect), connections["default"].execute_wrapper(no_default):
        executor.migrate(pre)
finally:
    db.connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)
batch_count = (count + 199) // 200
assert len(queries) <= 6 * batch_count + 30, len(queries)
assert pages and all("LIMIT 200" in sql and "OFFSET" not in sql for sql in pages), pages
assert max(params_seen) <= 999
for step in steps:
    events = Event.objects.using("secondary").filter(
        step=step, succeeded=False, occurred_at__gte=since).exclude(item_id=None)
    assert set(events.values_list("item_id", flat=True)) == set(range(1, count + 1))
    for n in range(1, count + 1):
        times = list(events.filter(item_id=n).values_list("occurred_at", flat=True))
        assert times == ([now, now] if n % 3 == 0 else [since])
    assert State.objects.using("secondary").get(step=step).failing_since == since
assert not Event.objects.using("secondary").filter(step="transcript").exists()
assert list(Event.objects.using("secondary").filter(
    pk__in=[event["id"] for event in before_events]).order_by("pk").values()) == before_events
assert [list(model.objects.using("default").order_by("pk").values())
        for model in (State, Event, Member)] == before_default
# Re-upgrading retains exactly the active distinct membership.
executor = MigrationExecutor(db)
executor.migrate(target)
Member = executor.loader.project_state(target).apps.get_model("archive", "ProcessingFailureItem")
for step in steps:
    assert set(Member.objects.using("secondary").filter(step=step).values_list(
        "item_id", flat=True)) == set(range(1, count + 1))
connections.close_all()
'''.replace("COUNT", str(count)))


def test_worker_recovers_processing_row_created_before_claim_timestamp(tmp_path):
    run_journey(tmp_path, r'''
import django
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from unittest.mock import patch

django.setup()
from archive.models import EnrichmentStatus as Status, Item

pre = [("archive", "0011_item_search_fts")]
executor = MigrationExecutor(connection)
executor.migrate(pre)
HistoricalItem = executor.loader.project_state(pre).apps.get_model("archive", "Item")
legacy_id = HistoricalItem.objects.create(
    original_url="https://example.test/legacy", enrichment_status="processing",
    summary_status="complete", transcript_status="complete", media_archive_status="complete",
    notes="Saved legacy notes",
).pk
MigrationExecutor(connection).migrate(executor.loader.graph.leaf_nodes("archive"))
assert Item.objects.get(pk=legacy_id).processing_started_at is None

now = timezone.now()
active = Item.objects.create(
    original_url="https://example.test/active", enrichment_status=Status.PROCESSING,
    processing_started_at=now,
)
claimed = []

def finish(*, item, **kwargs):
    claimed.append(item.pk)
    assert item.processing_started_at is not None
    item.enrichment_status = Status.COMPLETE
    item.save(update_fields=["enrichment_status"])
    return True

with patch("archive.management.commands.run_metadata_worker.enrich_item_metadata", finish):
    call_command("run_metadata_worker", once=True, stale_processing_after=120)
assert claimed == [legacy_id], claimed
legacy = Item.objects.get(pk=legacy_id)
assert legacy.enrichment_status == Status.COMPLETE
assert legacy.notes == "Saved legacy notes"
active.refresh_from_db()
assert active.enrichment_status == Status.PROCESSING
assert active.processing_started_at == now
''')
