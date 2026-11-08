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
