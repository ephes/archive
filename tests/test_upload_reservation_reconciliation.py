"""Retiring upload reservations whose owning processing attempt is provably over."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO, StringIO

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import call_command
from django.core.management.base import CommandError

from archive.media_archival import save_reserved_media_file
from archive.models import EnrichmentStatus, Item, ItemKind, PendingArchiveMediaUpload

ATTEMPT = datetime(2026, 11, 8, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }


def _processing_item(**extra) -> Item:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        media_archive_status=EnrichmentStatus.PROCESSING,
        **extra,
    )
    Item.objects.filter(pk=item.pk).update(processing_started_at=ATTEMPT)
    item.refresh_from_db()
    return item


def _upload(item: Item, name: str = "source.mp3") -> str:
    return save_reserved_media_file(
        target_name=f"items/{item.pk}/audio/{name}",
        upload_name=name,
        file_handle=BytesIO(b"bytes"),
        using="default",
        owner=item,
    )


def _reconcile(*args: str) -> str:
    out = StringIO()
    call_command("reconcile_archive_media_reservations", *args, stdout=out, stderr=out)
    return out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_reservation_records_owner_item_and_attempt():
    item = _processing_item()
    stored = _upload(item)

    reservation = PendingArchiveMediaUpload.objects.get()
    assert stored.startswith(reservation.prefix)
    assert reservation.owner_item_id == item.pk
    assert reservation.owner_attempt == ATTEMPT


@pytest.mark.django_db(transaction=True)
def test_live_generation_remains_protected_from_reconciliation_and_orphan_sweep(monkeypatch):
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    monkeypatch.setattr(storage, "get_modified_time", lambda path: datetime(2000, 1, 1, tzinfo=UTC))

    output = _reconcile("--apply")
    call_command("cleanup_archive_media_orphans", "--delete", stdout=StringIO())

    assert "live=1 retired=0" in output
    assert PendingArchiveMediaUpload.objects.count() == 1
    assert storage.exists(stored)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("retire", ["settled", "recovered-and-reclaimed", "deleted"])
def test_retired_attempt_is_released_and_its_unreferenced_objects_deleted(retire):
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    if retire == "settled":
        Item.objects.filter(pk=item.pk).update(
            media_archive_status=EnrichmentStatus.FAILED, processing_started_at=None
        )
    elif retire == "recovered-and-reclaimed":
        Item.objects.filter(pk=item.pk).update(
            processing_started_at=datetime(2026, 11, 8, 13, tzinfo=UTC)
        )
    else:
        Item.objects.filter(pk=item.pk).delete()

    dry_run = _reconcile()
    assert "retired=1" in dry_run and "released=0 deleted=0 mode=dry-run" in dry_run
    assert f"WOULD-DELETE {stored}" in dry_run
    assert storage.exists(stored)
    assert PendingArchiveMediaUpload.objects.count() == 1

    applied = _reconcile("--apply")
    assert "released=1 deleted=1 mode=apply failed=0" in applied
    assert not storage.exists(stored)
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_retired_reservation_without_object_is_released():
    item = _processing_item()
    PendingArchiveMediaUpload.objects.create(
        prefix=f"items/{item.pk}/audio/{'a' * 32}/",
        owner_item_id=item.pk,
        owner_attempt=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert "released=1 deleted=0" in _reconcile("--apply")
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_unowned_legacy_reservation_is_reported_but_never_released():
    storage = storages["archive_media"]
    prefix = "items/9/audio/" + "b" * 32 + "/"
    PendingArchiveMediaUpload.objects.create(prefix=prefix)
    storage.save(prefix + "source.mp3", ContentFile(b"legacy"))

    output = _reconcile("--apply")

    assert f"UNOWNED {prefix}" in output
    assert "unowned=1" in output and "released=0" in output
    assert PendingArchiveMediaUpload.objects.count() == 1
    assert storage.exists(prefix + "source.mp3")


@pytest.mark.django_db(transaction=True)
def test_referenced_object_under_retired_prefix_is_kept():
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    Item.objects.create(original_url="https://example.com/other", archived_audio_path=stored)
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)

    output = _reconcile("--apply")

    assert f"KEEP-REFERENCED {stored}" in output
    assert storage.exists(stored)
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_storage_failure_keeps_reservation_and_fails_the_command(monkeypatch):
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)

    def failing_delete(name):
        raise OSError("backend down")

    monkeypatch.setattr(storage, "delete", failing_delete)
    out = StringIO()
    with pytest.raises(CommandError, match="incomplete"):
        call_command("reconcile_archive_media_reservations", "--apply", stdout=out, stderr=out)

    assert "storage_delete_failed" in out.getvalue()
    assert "backend down" not in out.getvalue()
    assert PendingArchiveMediaUpload.objects.count() == 1
    assert storage.exists(stored)

    monkeypatch.undo()
    isolated = storages["archive_media"]
    assert "released=1" in _reconcile("--apply")
    assert not isolated.exists(stored)


@pytest.mark.django_db(transaction=True)
def test_oversized_prefix_is_left_for_manual_recovery(monkeypatch):
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    prefix = stored.rsplit("/", 1)[0] + "/"
    for index in range(17):
        storage.save(f"{prefix}extra-{index}.mp3", ContentFile(b"x"))
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)

    output = _reconcile("--apply")

    assert "oversized=1" in output and "released=0" in output
    assert PendingArchiveMediaUpload.objects.count() == 1
    assert storage.exists(stored)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("padding", [" ", "\t", "\n"])
def test_whitespace_padded_reference_still_protects_the_object(padding):
    from archive.media_storage import delete_unreferenced_archive_media_paths

    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    Item.objects.create(
        original_url="https://example.com/other",
        archived_audio_path=f"{padding}{stored}{padding}",
    )
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)

    output = _reconcile("--apply")
    assert f"KEEP-REFERENCED {stored}" in output
    assert storage.exists(stored)
    assert delete_unreferenced_archive_media_paths((stored,)) == []
    assert storage.exists(stored)


@pytest.mark.django_db(transaction=True)
def test_reference_containing_the_path_as_substring_does_not_protect_it():
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    Item.objects.create(original_url="https://example.com/other", archived_audio_path=stored + "x")
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)

    assert "released=1 deleted=1" in _reconcile("--apply")
    assert not storage.exists(stored)
