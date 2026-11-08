from __future__ import annotations

from io import StringIO

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import call_command
from django.db import transaction

from archive.models import Item


@pytest.fixture(autouse=True)
def isolated_archive_media_storage(settings, tmp_path) -> None:
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "archive-media")},
        },
    }


def _save_archive_media(path: str, payload: bytes = b"test-payload") -> None:
    _delete_archive_media(path)
    storages["archive_media"].save(path, ContentFile(payload))


def _delete_archive_media(*paths: str) -> None:
    for path in paths:
        if path and storages["archive_media"].exists(path):
            storages["archive_media"].delete(path)


@pytest.mark.django_db(transaction=True)
def test_item_delete_removes_archived_media_after_commit() -> None:
    item = Item.objects.create(
        original_url="https://example.com/delete-me",
        archived_audio_path="items/cleanup-delete/audio/source.mp3",
        archived_video_path="items/cleanup-delete/video/source.mp4",
    )
    _save_archive_media(item.archived_audio_path, b"audio")
    _save_archive_media(item.archived_video_path, b"video")

    with transaction.atomic():
        Item.objects.filter(pk=item.pk).delete()
        assert storages["archive_media"].exists(item.archived_audio_path) is True
        assert storages["archive_media"].exists(item.archived_video_path) is True

    assert storages["archive_media"].exists(item.archived_audio_path) is False
    assert storages["archive_media"].exists(item.archived_video_path) is False


@pytest.mark.django_db(transaction=True)
def test_item_delete_keeps_archive_media_when_still_referenced() -> None:
    shared_audio_path = "items/shared/audio/source.mp3"
    item = Item.objects.create(
        original_url="https://example.com/shared-a",
        archived_audio_path=shared_audio_path,
    )
    Item.objects.create(
        original_url="https://example.com/shared-b",
        archived_audio_path=shared_audio_path,
    )
    _save_archive_media(shared_audio_path, b"shared-audio")

    with transaction.atomic():
        item.delete()

    assert storages["archive_media"].exists(shared_audio_path) is True
    _delete_archive_media(shared_audio_path)


@pytest.mark.django_db
def test_item_delete_rollback_keeps_archived_media() -> None:
    item = Item.objects.create(
        original_url="https://example.com/delete-rollback",
        archived_audio_path="items/cleanup-rollback/audio/source.mp3",
    )
    item_pk = item.pk
    _save_archive_media(item.archived_audio_path, b"audio")

    with pytest.raises(RuntimeError, match="rollback"):
        with transaction.atomic():
            item.delete()
            raise RuntimeError("rollback")

    assert Item.objects.filter(pk=item_pk).exists() is True
    assert storages["archive_media"].exists(item.archived_audio_path) is True
    _delete_archive_media(item.archived_audio_path)


@pytest.mark.django_db
def test_cleanup_archive_media_orphans_dry_run_reports_without_deleting() -> None:
    referenced_path = "items/cleanup-command/audio/source.mp3"
    orphaned_path = "items/orphaned/audio/stale.mp3"
    Item.objects.create(
        original_url="https://example.com/cleanup-command",
        archived_audio_path=referenced_path,
    )
    _save_archive_media(referenced_path, b"referenced")
    _save_archive_media(orphaned_path, b"orphaned")

    stdout = StringIO()
    call_command("cleanup_archive_media_orphans", stdout=stdout)
    output = stdout.getvalue()

    assert f"ORPHAN {orphaned_path}" in output
    assert "Dry-run mode: reporting orphaned archive media objects only." in output
    assert "Summary: referenced=1 objects=2 orphaned=1 deleted=0 mode=dry-run" in output
    assert storages["archive_media"].exists(referenced_path) is True
    assert storages["archive_media"].exists(orphaned_path) is True

    _delete_archive_media(referenced_path, orphaned_path)


@pytest.mark.django_db
def test_cleanup_archive_media_orphans_reports_no_orphans() -> None:
    referenced_path = "items/cleanup-no-orphans/audio/source.mp3"
    Item.objects.create(
        original_url="https://example.com/cleanup-no-orphans",
        archived_audio_path=referenced_path,
    )
    _save_archive_media(referenced_path, b"referenced")

    stdout = StringIO()
    call_command("cleanup_archive_media_orphans", stdout=stdout)
    output = stdout.getvalue()

    assert "No orphaned archive media objects found." in output
    assert "Summary: referenced=1 objects=1 orphaned=0 deleted=0 mode=dry-run" in output
    assert storages["archive_media"].exists(referenced_path) is True

    _delete_archive_media(referenced_path)


@pytest.mark.django_db
def test_cleanup_archive_media_orphans_delete_removes_only_orphans() -> None:
    referenced_audio_path = "items/cleanup-delete-command/audio/source.mp3"
    referenced_video_path = "items/cleanup-delete-command/video/source.mp4"
    orphaned_path = "items/orphaned/video/stale.mp4"
    Item.objects.create(
        original_url="https://example.com/cleanup-delete-command",
        archived_audio_path=referenced_audio_path,
        archived_video_path=referenced_video_path,
    )
    _save_archive_media(referenced_audio_path, b"referenced-audio")
    _save_archive_media(referenced_video_path, b"referenced-video")
    _save_archive_media(orphaned_path, b"orphaned-video")

    stdout = StringIO()
    call_command(
        "cleanup_archive_media_orphans", "--delete", "--min-age-seconds", "0", stdout=stdout
    )
    output = stdout.getvalue()

    assert f"ORPHAN {orphaned_path}" in output
    assert "Deleted 1 orphaned archive media object." in output
    assert "deleted=1 mode=delete" in output
    assert storages["archive_media"].exists(referenced_audio_path) is True
    assert storages["archive_media"].exists(referenced_video_path) is True
    assert storages["archive_media"].exists(orphaned_path) is False

    _delete_archive_media(referenced_audio_path, referenced_video_path)


@pytest.mark.django_db(transaction=True)
def test_post_delete_storage_failure_is_recoverable(monkeypatch, caplog):
    path = "items/failed-cleanup/audio.mp3"
    item = Item.objects.create(
        original_url="https://example.com/cleanup-fault", archived_audio_path=path
    )
    pk = item.pk
    _save_archive_media(path)
    storage = storages["archive_media"]
    original_delete = storage.delete
    faults, callbacks = [], []

    def fail_delete(name):
        faults.append(name)
        raise RuntimeError("private-backend-details")

    monkeypatch.setattr(storage, "delete", fail_delete)
    with transaction.atomic():
        item.delete()
        transaction.on_commit(lambda: callbacks.append(True))
    assert faults == [path]
    assert callbacks == [True]
    assert not Item.objects.filter(pk=pk).exists()
    assert storage.exists(path)
    assert "orphan sweep" in caplog.text
    assert "private-backend-details" not in caplog.text
    assert path not in caplog.text

    monkeypatch.setattr(storage, "delete", original_delete)
    call_command(
        "cleanup_archive_media_orphans", "--delete", "--min-age-seconds", "0", stdout=StringIO()
    )
    assert not storage.exists(path)


@pytest.mark.django_db
def test_orphan_inventory_bounds_live_candidates_and_reference_queries(monkeypatch):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    count = 5000
    live = peak = 0

    class Candidate(str):
        def __new__(cls, value):
            nonlocal live, peak
            result = super().__new__(cls, value)
            live += 1
            peak = max(peak, live)
            return result

        def __del__(self):
            nonlocal live
            live -= 1

    def listing():
        for number in range(count):
            yield Candidate(f"items/{number:05}/audio.mp3")

    class Sink(StringIO):
        def write(self, value):
            # Keep summary lines only, so the test output cannot grow with inventory.
            if not value.startswith("ORPHAN "):
                return super().write(value)
            return len(value)

    Item.objects.create(
        original_url="https://example.com/inventory",
        archived_audio_path="items/00000/audio.mp3",
        archived_video_path="items/00000/audio.mp3",
    )
    monkeypatch.setattr(
        "archive.management.commands.cleanup_archive_media_orphans.iter_archive_media_object_names",
        listing,
    )
    output = Sink()
    with CaptureQueriesContext(connection) as queries:
        call_command("cleanup_archive_media_orphans", stdout=output)
    assert peak < 500, f"retained {peak} candidates at once"
    assert "referenced=1 objects=5000 orphaned=4999 deleted=0 mode=dry-run" in output.getvalue()
    assert len(queries) < 100  # Batches, rather than one query per object.
    assert not any(q["sql"].startswith(("INSERT", "UPDATE", "DELETE")) for q in queries)


@pytest.mark.django_db
def test_orphan_report_is_sorted_unique_and_counts_all_references(monkeypatch):
    Item.objects.create(
        original_url="https://example.com/refs-a",
        archived_audio_path="shared",
        archived_video_path="missing",
    )
    Item.objects.create(original_url="https://example.com/refs-b", article_audio_file="shared")
    monkeypatch.setattr(
        "archive.management.commands.cleanup_archive_media_orphans.iter_archive_media_object_names",
        lambda: iter(["z", "shared", "a", "z"]),
    )
    output = StringIO()
    call_command("cleanup_archive_media_orphans", stdout=output)
    assert [s for s in output.getvalue().splitlines() if s.startswith("ORPHAN ")] == [
        "ORPHAN a",
        "ORPHAN z",
    ]
    assert "referenced=2 objects=3 orphaned=2 deleted=0" in output.getvalue()


@pytest.mark.django_db(transaction=True)
def test_incomplete_inventory_never_starts_deletion(monkeypatch):
    deleted = []

    def listing():
        yield "orphan"
        raise ValueError("incomplete listing")

    monkeypatch.setattr(
        "archive.management.commands.cleanup_archive_media_orphans.iter_archive_media_object_names",
        listing,
    )
    monkeypatch.setattr(
        "archive.management.commands.cleanup_archive_media_orphans.delete_unreferenced_archive_media_paths",
        lambda paths: deleted.extend(paths),
    )
    with pytest.raises(ValueError, match="incomplete listing"):
        call_command(
            "cleanup_archive_media_orphans", "--delete", "--min-age-seconds", "0", stdout=StringIO()
        )
    assert deleted == []


@pytest.mark.parametrize("delete", [False, True])
def test_negative_orphan_age_is_rejected_before_any_work(monkeypatch, delete):
    from django.core.management.base import CommandError

    def unexpected(*args, **kwargs):
        pytest.fail("negative age performed storage or database work")

    monkeypatch.setattr(
        "archive.management.commands.cleanup_archive_media_orphans.iter_archive_media_object_names",
        unexpected,
    )
    args = ["--min-age-seconds", "-1"] + (["--delete"] if delete else [])
    with pytest.raises(CommandError, match="non-negative"):
        call_command("cleanup_archive_media_orphans", *args, stdout=StringIO())


@pytest.mark.django_db(transaction=True)
def test_orphan_age_errors_retain_objects_and_continue(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from archive.management.commands import cleanup_archive_media_orphans as command

    now = datetime(2026, 1, 2, tzinfo=UTC)
    old = now - timedelta(seconds=3601)
    cutoff = now - timedelta(seconds=3600)

    class OverflowingTime(datetime):
        def __rsub__(self, other):
            raise OverflowError("unusable backend timestamp")

    times = {
        "naive": datetime(2020, 1, 1),
        "not-date": "yesterday",
        "null": None,
        "overflow": OverflowingTime(2020, 1, 1, tzinfo=UTC),
        "old": old,
        "cutoff": cutoff,
        "young": now,
        "future": now + timedelta(days=1),
    }
    storage = storages["archive_media"]
    for name in times:
        _save_archive_media(name)
    monkeypatch.setattr(command.timezone, "now", lambda: now)
    monkeypatch.setattr(storage, "get_modified_time", times.__getitem__)
    call_command("cleanup_archive_media_orphans", "--delete", stdout=StringIO())
    assert {name for name in times if not storage.exists(name)} == {"old", "cutoff"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("padding", ["\t", "\n", "\x1c", "\u3000"])
def test_sweep_preserves_whitespace_normalized_references(monkeypatch, padding):
    path = "items/padded/audio.mp3"
    Item.objects.create(
        original_url="https://example.com/padded", archived_audio_path=padding + path + padding
    )
    _save_archive_media(path)
    out = StringIO()
    call_command("cleanup_archive_media_orphans", "--delete", "--min-age-seconds", "0", stdout=out)
    assert storages["archive_media"].exists(path)
    assert "referenced=1 objects=1 orphaned=0 deleted=0" in out.getvalue()
