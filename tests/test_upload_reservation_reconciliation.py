"""Retiring upload reservations whose owning processing attempt is provably over."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connections

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
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_reconciliation_inventory_writer_lock(monkeypatch, apply):
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)
    listdir = storage.listdir
    writes_during_listing = []

    def update_owner():
        connection = connections["default"]
        try:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA busy_timeout = 0")
            return Item.objects.filter(pk=item.pk).update(title="Updated during inventory")
        except OperationalError as exc:
            assert "locked" in str(exc)
            return 0
        finally:
            connection.close()

    def listdir_during_update(prefix):
        with ThreadPoolExecutor(max_workers=1) as executor:
            writes_during_listing.append(executor.submit(update_owner).result(timeout=5))
        return listdir(prefix)

    monkeypatch.setattr(storage, "listdir", listdir_during_update)
    if apply:
        assert f"DELETED {stored}" in _reconcile("--apply")
        assert writes_during_listing == [0]
        assert not storage.exists(stored)
        assert not PendingArchiveMediaUpload.objects.exists()
    else:
        assert f"WOULD-DELETE {stored}" in _reconcile()
        assert writes_during_listing == [1]
        assert storage.exists(stored)
        assert PendingArchiveMediaUpload.objects.exists()


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
@pytest.mark.parametrize("problem", ["missing-subtree", "malformed-child"])
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_incomplete_listing_keeps_reservation_and_allows_later_reservation(
    monkeypatch, problem, apply
):
    first = _processing_item()
    second = _processing_item()
    first_name = _upload(first)
    second_name = _upload(second)
    Item.objects.filter(pk__in=[first.pk, second.pk]).update(processing_started_at=None)
    first_prefix = first_name.rsplit("/", 1)[0]
    storage = storages["archive_media"]
    listdir = storage.listdir

    def incomplete_listdir(prefix):
        if prefix == first_prefix:
            child = "missing" if problem == "missing-subtree" else "invalid/child"
            return [child], [first_name.rsplit("/", 1)[1]]
        return listdir(prefix)

    monkeypatch.setattr(storage, "listdir", incomplete_listdir)
    out = StringIO()
    err = StringIO()
    args = ("--apply",) if apply else ()
    with pytest.raises(CommandError, match="incomplete"):
        call_command("reconcile_archive_media_reservations", *args, stdout=out, stderr=err)

    assert "Storage listing failed for reservation 1 (storage_listing_failed)." in err.getvalue()
    assert first_prefix not in err.getvalue()
    assert "invalid/child" not in err.getvalue()
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=first.pk).exists()
    assert storage.exists(first_name)
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=second.pk).exists() is not apply
    assert storage.exists(second_name) is not apply
    assert f"{'DELETED' if apply else 'WOULD-DELETE'} {second_name}" in out.getvalue()
    assert f"released={int(apply)} deleted={int(apply)}" in out.getvalue()
    assert "failed=1" in out.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "error",
    [
        PermissionError("backend down"),
        ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "backend down"}}, "ListObjectsV2"
        ),
        EndpointConnectionError(endpoint_url="https://backend-down.invalid"),
    ],
    ids=["permission", "client-error", "botocore-error"],
)
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_failed_listing_is_reported_as_listing_failure(monkeypatch, error, apply):
    first = _processing_item()
    second = _processing_item()
    first_name = _upload(first)
    second_name = _upload(second)
    Item.objects.filter(pk__in=[first.pk, second.pk]).update(processing_started_at=None)
    first_prefix = first_name.rsplit("/", 1)[0]
    storage = storages["archive_media"]
    listdir = storage.listdir

    def failing_listdir(prefix):
        if prefix == first_prefix:
            raise error
        return listdir(prefix)

    monkeypatch.setattr(storage, "listdir", failing_listdir)
    out = StringIO()
    err = StringIO()
    args = ("--apply",) if apply else ()
    with pytest.raises(CommandError, match="incomplete"):
        call_command("reconcile_archive_media_reservations", *args, stdout=out, stderr=err)

    assert "Storage listing failed for reservation 1 (storage_listing_failed)." in err.getvalue()
    assert "storage_delete_failed" not in err.getvalue()
    assert "backend" not in err.getvalue()
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=first.pk).exists()
    assert storage.exists(first_name)
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=second.pk).exists() is not apply
    assert storage.exists(second_name) is not apply
    assert f"released={int(apply)} deleted={int(apply)}" in out.getvalue()
    assert "failed=1" in out.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("deletes_before_failure", [0, 1])
def test_storage_failure_keeps_reservation_and_fails_the_command(
    monkeypatch, deletes_before_failure
):
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    extra = stored.rsplit("/", 1)[0] + "/z-extra.mp3"
    storage.save(extra, ContentFile(b"extra"))
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)

    delete = storage.delete
    deleted = []

    def failing_delete(name):
        if len(deleted) == deletes_before_failure:
            raise OSError("backend down")
        delete(name)
        deleted.append(name)

    monkeypatch.setattr(storage, "delete", failing_delete)
    out = StringIO()
    with pytest.raises(CommandError, match="incomplete"):
        call_command("reconcile_archive_media_reservations", "--apply", stdout=out, stderr=out)

    assert "storage_delete_failed" in out.getvalue()
    assert "backend down" not in out.getvalue()
    assert PendingArchiveMediaUpload.objects.count() == 1
    assert storage.exists(stored) == (deletes_before_failure == 0)
    assert storage.exists(extra)
    assert f"released=0 deleted={deletes_before_failure} mode=apply failed=1" in out.getvalue()

    monkeypatch.setattr(storage, "delete", delete)
    assert f"released=1 deleted={2 - deletes_before_failure} mode=apply failed=0" in _reconcile(
        "--apply"
    )
    assert not storage.exists(stored)
    assert not storage.exists(extra)
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", ["delete", "commit", "commit-ack"])
def test_database_failure_preserves_completed_storage_deletes(monkeypatch, failure):
    first = _processing_item()
    first_name = _upload(first)
    extra_name = first_name.rsplit("/", 1)[0] + "/extra.mp3"
    storage = storages["archive_media"]
    storage.save(extra_name, ContentFile(b"extra"))
    second = _processing_item()
    second_name = _upload(second)
    Item.objects.filter(pk__in=[first.pk, second.pk]).update(processing_started_at=None)
    first_reservation = PendingArchiveMediaUpload.objects.get(owner_item_id=first.pk)

    if failure == "delete":
        delete = PendingArchiveMediaUpload.delete

        def failing_delete(reservation):
            target = reservation.pk == first_reservation.pk
            result = delete(reservation)
            if target:
                raise OperationalError("database details")
            return result

        monkeypatch.setattr(PendingArchiveMediaUpload, "delete", failing_delete)
    else:
        connection = connections["default"]
        commit = connection.commit

        def fail_first_commit():
            monkeypatch.setattr(connection, "commit", commit)
            if failure == "commit-ack":
                commit()
            raise OperationalError("database details")

        monkeypatch.setattr(connection, "commit", fail_first_commit)

    out = StringIO()
    err = StringIO()
    with pytest.raises(CommandError, match="incomplete") as error:
        call_command("reconcile_archive_media_reservations", "--apply", stdout=out, stderr=err)

    assert "reservation_release_failed" in err.getvalue()
    assert "database details" not in err.getvalue()
    assert "Confirmed object deletes: 2." in err.getvalue()
    assert f"DELETED {second_name}" in out.getvalue()
    assert "released=1 deleted=3 mode=apply failed=1" in out.getvalue()
    assert not any(storage.exists(name) for name in (first_name, extra_name, second_name))
    assert not PendingArchiveMediaUpload.objects.filter(owner_item_id=second.pk).exists()
    if failure == "commit-ack":
        assert not PendingArchiveMediaUpload.objects.filter(pk=first_reservation.pk).exists()
        assert "Verify the reservation state" in str(error.value)
        assert "remains for retry" not in err.getvalue()
    else:
        assert PendingArchiveMediaUpload.objects.filter(
            pk=first_reservation.pk, prefix=first_reservation.prefix
        ).exists()
        assert "Reservation remains for retry" in err.getvalue()
        assert "Retry the command" in str(error.value)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_oversized_prefix_is_left_for_manual_recovery(apply):
    item = _processing_item()
    storage = storages["archive_media"]
    stored = _upload(item)
    prefix = stored.rsplit("/", 1)[0] + "/"
    for index in range(17):
        storage.save(f"{prefix}extra-{index}.mp3", ContentFile(b"x"))
    sibling = _processing_item()
    sibling_name = _upload(sibling)
    Item.objects.filter(pk__in=[item.pk, sibling.pk]).update(processing_started_at=None)

    if apply:
        out = StringIO()
        with pytest.raises(CommandError, match="incomplete.*manual recovery") as error:
            call_command("reconcile_archive_media_reservations", "--apply", stdout=out)
        assert "retry" not in str(error.value).lower()
        output = out.getvalue()
    else:
        output = _reconcile()

    assert f"OVERSIZED {prefix} item={item.pk}" in output
    assert f"oversized=1 released={int(apply)} deleted={int(apply)}" in output
    assert "failed=0" in output
    assert f"{'DELETED' if apply else 'WOULD-DELETE'} {sibling_name}" in output
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=item.pk).exists()
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=sibling.pk).exists() is not apply
    assert storage.exists(stored)
    assert storage.exists(sibling_name) is not apply


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("shape", "children", "expected_listings", "oversized"),
    [("wide", 64, 1, True), ("deep", 64, 64, True), ("wide", 63, 64, False)],
    ids=["wide-overflow", "deep-overflow", "exact-limit"],
)
def test_retired_reservation_directory_budget(
    monkeypatch, shape, children, expected_listings, oversized
):
    retired = _processing_item()
    stored = _upload(retired)
    Item.objects.filter(pk=retired.pk).update(processing_started_at=None)
    live = _processing_item()
    live_name = _upload(live)

    storage = storages["archive_media"]
    prefix = stored.rsplit("/", 1)[0]
    directory = Path(storage.path(prefix))
    for index in range(children):
        child = directory / f"child-{index}"
        child.mkdir()
        if shape == "deep":
            directory = child

    listdir = storage.listdir
    listings = []

    def record_listdir(path):
        listings.append(path)
        return listdir(path)

    monkeypatch.setattr(storage, "listdir", record_listdir)
    if oversized:
        out = StringIO()
        with pytest.raises(CommandError, match="incomplete.*manual recovery"):
            call_command("reconcile_archive_media_reservations", "--apply", stdout=out)
        output = out.getvalue()
    else:
        output = _reconcile("--apply")

    assert len(listings) == expected_listings
    assert all(path == prefix or path.startswith(prefix + "/") for path in listings)
    assert f"live=1 retired={int(not oversized)}" in output
    assert f"oversized={int(oversized)} released={int(not oversized)}" in output
    assert storage.exists(stored) == oversized
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=retired.pk).exists() == oversized
    assert storage.exists(live_name)
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=live.pk).exists()


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


@pytest.mark.django_db(transaction=True)
def test_flat_file_budget_is_checked_before_visiting_names(monkeypatch):
    class OversizedFiles(list):
        def __iter__(self):
            pytest.fail("oversized listing was traversed")

    item = _processing_item()
    stored = _upload(item)
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)
    storage = storages["archive_media"]
    monkeypatch.setattr(storage, "listdir", lambda path: ([], OversizedFiles(["x"] * 1000)))

    with pytest.raises(CommandError, match="oversized reservations require manual recovery"):
        _reconcile("--apply")

    assert storage.exists(stored)
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=item.pk).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("extra", [0, 1], ids=["exact-limit", "cumulative-overflow"])
def test_nested_file_budget_and_order(extra):
    item = _processing_item()
    stored = _upload(item)
    Item.objects.filter(pk=item.pk).update(processing_started_at=None)
    prefix = stored.rsplit("/", 1)[0]
    storage = storages["archive_media"]
    nested = [f"{prefix}/nested/{index:02d}.mp3" for index in range(15 + extra)]
    for name in reversed(nested):
        storage.save(name, ContentFile(b"bytes"))

    if extra:
        with pytest.raises(CommandError, match="oversized reservations require manual recovery"):
            _reconcile("--apply")
        assert all(storage.exists(name) for name in [stored, *nested])
        assert PendingArchiveMediaUpload.objects.filter(owner_item_id=item.pk).exists()
    else:
        output = _reconcile("--apply")
        assert [line.strip() for line in output.splitlines() if line.startswith("  DELETED ")] == [
            f"DELETED {name}" for name in [stored, *nested]
        ]
        assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", ["write", "flush"])
@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_output_failure_preserves_reconciliation_progress(failure, apply):
    class BrokenReceipt(StringIO):
        def write(self, message: str) -> int:
            if failure == "write" and message.startswith("RETIRED"):
                raise BrokenPipeError("detail write failed")
            return super().write(message)

        def flush(self) -> None:
            if failure == "flush" and "RETIRED " in self.getvalue():
                raise BrokenPipeError("detail flush failed")
            super().flush()

    first = _processing_item()
    first_name = _upload(first)
    second = _processing_item()
    second_name = _upload(second)
    Item.objects.filter(pk__in=[first.pk, second.pk]).update(processing_started_at=None)
    mode = "apply" if apply else "dry-run"
    count = int(apply)

    with pytest.raises(
        CommandError, match=f"output failed.*mode={mode}.*released={count} deleted={count}"
    ) as error:
        call_command(
            "reconcile_archive_media_reservations",
            *(("--apply",) if apply else ()),
            stdout=BrokenReceipt(),
        )

    assert isinstance(error.value.__cause__, BrokenPipeError)
    storage = storages["archive_media"]
    assert storage.exists(first_name) is not apply
    assert storage.exists(second_name)
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=first.pk).exists() is not apply
    assert PendingArchiveMediaUpload.objects.filter(owner_item_id=second.pk).exists()
