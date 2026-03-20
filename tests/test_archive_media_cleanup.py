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
    call_command("cleanup_archive_media_orphans", "--delete", stdout=stdout)
    output = stdout.getvalue()

    assert f"ORPHAN {orphaned_path}" in output
    assert "Deleted 1 orphaned archive media object." in output
    assert "deleted=1 mode=delete" in output
    assert storages["archive_media"].exists(referenced_audio_path) is True
    assert storages["archive_media"].exists(referenced_video_path) is True
    assert storages["archive_media"].exists(orphaned_path) is False

    _delete_archive_media(referenced_audio_path, referenced_video_path)
