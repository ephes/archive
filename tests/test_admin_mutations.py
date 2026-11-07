from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.urls import reverse

from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import enrich_item_summaries, enrich_item_transcript, request_item_reprocess
from archive.summaries import GeneratedSummary


def admin_data(response):
    form = response.context["adminform"].form
    return {name: form[name].value() for name in form.fields if form[name].value() is not None}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("stage", ["summary", "transcript"])
def test_admin_rejects_stale_generated_content(client, editor_user, monkeypatch, stage):
    item = Item.objects.create(
        original_url="https://example.test/episode.mp3",
        audio_url="https://example.test/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    client.force_login(editor_user)
    url = reverse("admin:archive_item_change", args=[item.pk])
    data = admin_data(client.get(url))
    data["notes"] = "Keep my submitted notes"
    if stage == "summary":
        monkeypatch.setattr(
            "archive.services.generate_item_summaries",
            lambda **kwargs: GeneratedSummary("New short", "New long", ("new",)),
        )
        assert enrich_item_summaries(item)
    else:
        monkeypatch.setattr(
            "archive.services.generate_item_transcript", lambda **kwargs: "New transcript"
        )
        assert enrich_item_transcript(item)
    before = Item.objects.values().get(pk=item.pk)

    response = client.post(url, data)

    assert response.status_code == 200
    assert "changed since you opened" in str(response.context["adminform"].form.errors)
    assert response.context["adminform"].form["notes"].value() == data["notes"]
    assert Item.objects.values().get(pk=item.pk) == before
    # Redisplaying a conflict must not silently bless the old submitted text.
    again = client.post(url, admin_data(response))
    assert again.status_code == 200
    assert Item.objects.values().get(pk=item.pk) == before

    fresh = admin_data(client.get(url))
    fresh["notes"] = "Reviewed fresh content"
    assert client.post(url, fresh).status_code == 302
    item.refresh_from_db()
    assert item.notes == "Reviewed fresh content"
    if stage == "summary":
        assert item.short_summary == "New short"
        assert item.short_summary_generated
    else:
        assert item.transcript == "New transcript"
        assert item.transcript_generated


@pytest.mark.django_db
@pytest.mark.parametrize("token", ["", "invalid", "another_item"])
def test_admin_requires_authentic_revision(client, editor_user, token):
    item = Item.objects.create(original_url="https://example.test/item")
    client.force_login(editor_user)
    url = reverse("admin:archive_item_change", args=[item.pk])
    data = admin_data(client.get(url))
    if token == "another_item":
        other = Item.objects.create(original_url="https://example.test/other")
        token = admin_data(client.get(reverse("admin:archive_item_change", args=[other.pk])))[
            "content_revision"
        ]
    data.update(content_revision=token, notes="Must not save")
    assert client.post(url, data).status_code == 200
    item.refresh_from_db()
    assert item.notes == ""


@pytest.mark.django_db(transaction=True)
def test_reprocess_uses_current_media_and_defers_only_retired_names(monkeypatch):
    stale = Item.objects.create(
        original_url="https://example.test/story",
        archived_audio_path="old/audio.mp3",
    )
    Item.objects.filter(pk=stale.pk).update(
        archived_audio_path="current/audio.mp3", archived_video_path="current/video.mp4"
    )
    retired = []
    monkeypatch.setattr(
        "archive.services.delete_unreferenced_archive_media_paths",
        lambda paths, **kwargs: retired.extend(paths),
    )
    request_item_reprocess(stale)
    stale.refresh_from_db()
    assert not stale.archived_audio_path
    assert not stale.archived_video_path
    assert set(retired) == {"current/audio.mp3", "current/video.mp4"}


@pytest.mark.django_db(transaction=True)
def test_reprocess_preserves_current_processing_attempt():
    stale = Item.objects.create(original_url="https://example.test/story")
    current = Item.objects.get(pk=stale.pk)
    current.media_archive_status = EnrichmentStatus.PROCESSING
    current.save()
    before = Item.objects.values().get(pk=stale.pk)
    request_item_reprocess(stale)
    assert Item.objects.values().get(pk=stale.pk) == before


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
@pytest.mark.parametrize("operation", ["admin", "reprocess"])
def test_admin_worker_mutations_on_file_sqlite(tmp_path, journal, operation):
    # Separate process and connections exercise the real IMMEDIATE write boundary.
    script = r"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.db import connections, transaction
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse
from archive.models import Item, ItemKind, EnrichmentStatus
from archive.services import request_item_reprocess
setup_test_environment()
call_command("migrate", verbosity=0)
with connections["default"].cursor() as cursor:
    cursor.execute("PRAGMA journal_mode=" + JOURNAL)
user = get_user_model().objects.create_superuser(username="editor")
client = Client()
client.force_login(user)
item = Item.objects.create(original_url="https://example.test/story",
    archived_audio_path="old/audio.mp3")
url = reverse("admin:archive_item_change", args=[item.pk])
response = client.get(url)
form = response.context["adminform"].form
data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
data["notes"] = "Submitted notes"
write_locked = Event()
mutation_started = Event()

def worker():
    db = connections["default"]
    def observe(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql == "BEGIN IMMEDIATE":
            write_locked.set()
            assert mutation_started.wait(5)
        return result
    try:
        with db.execute_wrapper(observe), transaction.atomic():
            Item.objects.filter(pk=item.pk).update(
                short_summary="Worker summary", short_summary_generated=True,
                summary_status=EnrichmentStatus.COMPLETE,
                kind=ItemKind.VIDEO, classification_rule="operator_override",
                media_url="https://example.test/new.mp4",
                archived_audio_path="new/audio.mp3",
                media_archive_status=EnrichmentStatus.COMPLETE)
    finally:
        db.close()

with ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(worker)
    assert write_locked.wait(5)
    db = connections["default"]
    def observe(execute, sql, params, many, context):
        if sql == "BEGIN IMMEDIATE":
            mutation_started.set()
        return execute(sql, params, many, context)
    # The stale instance/GET is already selected; worker holds the writer lock.
    # A reader without a write transaction would read the old committed row.
    if OPERATION == "reprocess":
        mutation_started.set()
    with db.execute_wrapper(observe):
        if OPERATION == "admin":
            response = client.post(url, data)
            assert response.status_code == 200
            assert "changed since you opened" in str(response.context["adminform"].form.errors)
        else:
            request_item_reprocess(item)
    future.result(timeout=10)
item.refresh_from_db()
assert item.short_summary == "Worker summary"
assert item.short_summary_generated
assert item.kind == ItemKind.VIDEO
assert item.classification_rule == "operator_override"
assert item.archived_audio_path == "new/audio.mp3"
connections.close_all()
"""
    script = f"JOURNAL = {journal!r}\nOPERATION = {operation!r}\n" + script
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "DJANGO_DB_PATH": str(tmp_path / "mutations.sqlite3"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_admin_add_and_manual_override(client, editor_user):
    client.force_login(editor_user)
    response = client.get(reverse("admin:archive_item_add"))
    data = admin_data(response)
    data.update(original_url="https://example.test/new", title="New item")
    assert client.post(reverse("admin:archive_item_add"), data).status_code == 302
    item = Item.objects.get()
    Item.objects.filter(pk=item.pk).update(short_summary="Generated", short_summary_generated=True)
    url = reverse("admin:archive_item_change", args=[item.pk])
    data = admin_data(client.get(url))
    data["short_summary"] = "Reviewed operator override"
    assert client.post(url, data).status_code == 302
    item.refresh_from_db()
    assert item.short_summary == "Reviewed operator override"
    assert not item.short_summary_generated


@pytest.mark.django_db(transaction=True)
def test_admin_reprocess_selection_preserves_later_kind_media_and_active_attempt(
    client, editor_user, monkeypatch
):
    client.force_login(editor_user)
    items = [Item.objects.create(original_url=f"https://example.test/{n}") for n in range(2)]
    selected = []

    def publish_then_reprocess(stale):
        selected.append(stale.pk)
        Item.objects.filter(pk=stale.pk).update(
            kind=ItemKind.VIDEO,
            classification_rule="operator_override",
            media_url="https://example.test/new.mp4",
            archived_audio_path="new/audio.mp3",
            media_archive_status=(
                EnrichmentStatus.PROCESSING
                if stale.pk == items[1].pk
                else EnrichmentStatus.COMPLETE
            ),
        )
        return request_item_reprocess(stale)

    monkeypatch.setattr("archive.admin.request_item_reprocess", publish_then_reprocess)
    response = client.post(
        reverse("admin:archive_item_changelist"),
        {"action": "reprocess_selected_items", "_selected_action": [item.pk for item in items]},
        follow=True,
    )
    assert set(selected) == {item.pk for item in items}
    assert b"Queued 1 item(s) for reprocessing. Skipped 1 active item(s)." in response.content
    for item in items:
        item.refresh_from_db()
        assert item.kind == ItemKind.VIDEO
        assert item.archived_audio_path == "new/audio.mp3"
    assert items[1].media_archive_status == EnrichmentStatus.PROCESSING


@pytest.mark.django_db
def test_admin_revision_is_not_an_edit_in_history(client, editor_user):
    import json

    from django.contrib.admin.models import LogEntry

    client.force_login(editor_user)
    item = Item.objects.create(original_url="https://example.test/history")
    url = reverse("admin:archive_item_change", args=[item.pk])
    data = admin_data(client.get(url))
    data["notes"] = "Notes only"
    assert client.post(url, data).status_code == 302
    assert json.loads(LogEntry.objects.latest("id").change_message) == [
        {"changed": {"fields": ["Notes"]}}
    ]
    assert client.post(url, admin_data(client.get(url))).status_code == 302
    assert json.loads(LogEntry.objects.latest("id").change_message) == []
