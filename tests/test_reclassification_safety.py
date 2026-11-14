import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import transaction
from django.db.models.query import QuerySet

from archive.management.commands import reclassify_items
from archive.models import EnrichmentStatus, Item, ItemKind

pytestmark = pytest.mark.django_db


def test_replay_uses_edits_and_completed_work_after_selection(monkeypatch):
    item = Item.objects.create(original_url="https://example.com/story", kind=ItemKind.LINK)
    select = reclassify_items.Command._matching_items

    def interleave(self, options):
        selected = list(select(self, options))
        Item.objects.filter(pk=item.pk).update(
            kind=ItemKind.VIDEO,
            media_url="https://example.com/video.mp4",
            classification_rule="operator_override",
            transcript="new completed transcript",
            transcript_status=EnrichmentStatus.COMPLETE,
            archived_audio_path="new/audio.mp3",
            media_archive_status=EnrichmentStatus.COMPLETE,
        )
        return iter(selected)

    monkeypatch.setattr(reclassify_items.Command, "_matching_items", interleave)
    call_command(
        "reclassify_items", "--all", "--apply", "--normalize-downstream", stdout=StringIO()
    )
    item.refresh_from_db()
    assert item.kind == ItemKind.VIDEO
    assert item.classification_rule == "operator_override"
    assert item.media_url == "https://example.com/video.mp4"
    assert item.transcript == "new completed transcript"
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == "new/audio.mp3"
    assert item.media_archive_status == EnrichmentStatus.COMPLETE


def test_replay_decides_and_saves_in_one_transaction(monkeypatch):
    Item.objects.create(original_url="https://example.com/story")
    classify = reclassify_items.classify_item
    save = Item.save
    # pytest-django's outer test transaction is not the command's row transaction.
    depth = len(transaction.get_connection().atomic_blocks)
    phases = []

    def check_classify(**kwargs):
        phases.append("classify")
        assert len(transaction.get_connection().atomic_blocks) == depth + 1
        return classify(**kwargs)

    def check_save(self, *args, **kwargs):
        phases.append("save")
        assert len(transaction.get_connection().atomic_blocks) == depth + 1
        return save(self, *args, **kwargs)

    monkeypatch.setattr(reclassify_items, "classify_item", check_classify)
    monkeypatch.setattr(Item, "save", check_save)
    call_command("reclassify_items", "--all", "--apply", stdout=StringIO())
    assert phases == ["classify", "save"]


@pytest.mark.parametrize("size", [1, 501])
def test_selection_streams_only_ids_and_urls(size, monkeypatch, django_assert_num_queries):
    Item.objects.bulk_create(
        [
            Item(original_url=f"https://example.com/{n}", transcript="payload" * 10000)
            for n in range(size)
        ]
    )
    fetch_all = QuerySet._fetch_all
    classify = reclassify_items.classify_item
    batches = []
    classified = 0
    selection_sql = []

    def observe(self):
        is_selection = self._fields == ("id", "original_url") and self._result_cache is None
        result = fetch_all(self)
        if is_selection:
            batches.append(len(self._result_cache))
            selection_sql.append(str(self.query))
        return result

    def first_decision(**kwargs):
        nonlocal classified
        if classified == 0:
            assert batches == [min(200, size)]
        classified += 1
        return classify(**kwargs)

    monkeypatch.setattr(QuerySet, "_fetch_all", observe)
    monkeypatch.setattr(reclassify_items, "classify_item", first_decision)
    output = StringIO()
    call_command("reclassify_items", "--all", stdout=output)
    assert classified == size
    assert f"inspected={size}" in output.getvalue()
    assert sum(batches) == size
    assert max(batches) <= 200
    assert all('"transcript"' not in sql and "LIMIT 200" in sql for sql in selection_sql)
    options = {
        "item_ids": [],
        "rule": [],
        "empty_rule": False,
        "empty_evidence": False,
        "stale_only": False,
        "host": ["example.com"],
        "limit": None,
    }
    with django_assert_num_queries(size // 200 + 2):
        ids = list(reclassify_items.Command()._matching_items(options))
    assert len(ids) == size
    assert all(isinstance(pk, int) for pk in ids)


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_selection_releases_sqlite_cursor_before_concurrent_edit(tmp_path, journal):
    script = r"""
import django
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from django.db import connections
from django.core.management import call_command

django.setup()
from archive.management.commands.reclassify_items import Command
from archive.models import Item, ItemKind, EnrichmentStatus
call_command("migrate", verbosity=0)
with connections["default"].cursor() as cursor:
    cursor.execute("PRAGMA journal_mode=" + JOURNAL)
Item.objects.bulk_create([
    Item(original_url=f"https://example.com/{n}") for n in range(201)
])
target = Item.objects.order_by("id")[1]
original = Command._matching_items

def edit():
    try:
        Item.objects.filter(pk=target.pk).update(
            kind=ItemKind.VIDEO, classification_rule="operator_override",
            media_url="https://example.com/new.mp4", transcript="completed",
            transcript_status=EnrichmentStatus.COMPLETE,
            archived_audio_path="new/audio.mp3", media_archive_status=EnrichmentStatus.COMPLETE)
    finally:
        connections.close_all()

def selected(self, options):
    for index, pk in enumerate(original(self, options)):
        if index == 0:
            # Selection has started but its cursor must be closed before this
            # independent connection commits. The command then sees these edits.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(edit).result(timeout=10)
        yield pk
Command._matching_items = selected
call_command("reclassify_items", "--all", "--apply", "--normalize-downstream", stdout=StringIO())
target.refresh_from_db()
assert target.kind == ItemKind.VIDEO
assert target.classification_rule == "operator_override"
assert target.transcript == "completed"
assert target.media_archive_status == EnrichmentStatus.COMPLETE
assert target.archived_audio_path == "new/audio.mp3"
connections.close_all()
"""
    script = "JOURNAL = " + repr(journal) + "\n" + script
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "DJANGO_DB_PATH": str(tmp_path / "replay.sqlite3"),
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


@pytest.mark.parametrize("apply", [False, True])
def test_replay_diagnostics_hide_private_url_components(apply):
    url = "https://test-user:test-password@example.com/private-token.mp3?signature=secret-query#secret-frag"
    item = Item.objects.create(original_url=url, audio_url=url)
    output = StringIO()
    args = ["--all", *(["--apply"] if apply else [])]
    call_command("reclassify_items", *args, stdout=output)
    if not apply:
        call_command("reclassify_items", "--all", "--apply", stdout=StringIO())
    # Exercise UNCHANGED as well as WOULD UPDATE / UPDATED.
    call_command("reclassify_items", *args, stdout=output)
    text = output.getvalue()
    assert f"item {item.pk}" in text
    assert "https://example.com" in text
    assert "UNCHANGED" in text
    for secret in ("test-user", "test-password", "private-token", "secret-query", "secret-frag"):
        assert secret not in text


@pytest.mark.parametrize("apply", [False, True])
def test_replay_stops_at_starting_horizon_despite_new_batches(monkeypatch, apply):
    Item.objects.bulk_create([Item(original_url=f"https://example.com/{n}") for n in range(400)])
    original_ids = set(Item.objects.values_list("pk", flat=True))
    process = reclassify_items.Command._process_item
    processed = []

    def insert_while_processing(self, item, options):
        processed.append(item.pk)
        if len(processed) % 200 == 1 and len(processed) < 800:
            Item.objects.bulk_create(
                [
                    Item(original_url=f"https://example.com/later-{len(processed)}-{n}")
                    for n in range(200)
                ]
            )
        return process(self, item, options)

    monkeypatch.setattr(reclassify_items.Command, "_process_item", insert_while_processing)
    args = ["--all", *(["--apply"] if apply else [])]
    output = StringIO()
    call_command("reclassify_items", *args, stdout=output)
    assert set(processed) == original_ids
    assert "inspected=400" in output.getvalue()
    later_ids = set(Item.objects.values_list("pk", flat=True)) - original_ids
    processed.clear()
    monkeypatch.setattr(reclassify_items.Command, "_process_item", process)
    second = StringIO()
    call_command("reclassify_items", *args, stdout=second)
    assert f"inspected={len(original_ids) + len(later_ids)}" in second.getvalue()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("host_filter", [False, True])
def test_replay_malformed_legacy_url_has_fixed_diagnostic(apply, host_filter):
    from django.core.management.base import CommandError

    item = Item.objects.create(original_url="https://[private-token]/private-path?signature=secret")
    args = [
        "--all",
        *(["--apply"] if apply else []),
        *(["--host", "example.com"] if host_filter else []),
    ]
    with pytest.raises(CommandError) as error:
        call_command("reclassify_items", *args, stdout=StringIO())
    assert f"item {item.pk}" in str(error.value)
    assert "private" not in str(error.value)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
