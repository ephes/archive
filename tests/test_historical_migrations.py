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
