from datetime import UTC, datetime

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import call_command
from django.db import connections, transaction
from test_media_archival import _fake_ffmpeg_run, _FakeResponse

from archive.management.commands.run_metadata_worker import ItemProcessingStalled
from archive.media_archival import MediaArchivalError, archive_item_audio
from archive.media_storage import (
    delete_unreferenced_archive_media_paths,
    iter_archive_media_object_names,
)
from archive.models import Item, ItemKind, PendingArchiveMediaUpload
from archive.services import enrich_item_media_archive


@pytest.fixture(autouse=True)
def isolated_storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }


def fake_video_download(monkeypatch, item):
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"video"], content_type="video/mp4", final_url=item.media_url
        ),
    )
    monkeypatch.setattr("archive.media_archival.subprocess.run", _fake_ffmpeg_run)


def assert_committed_references_exist(item):
    committed = Item.objects.get(pk=item.pk)
    storage = storages["archive_media"]
    for path in (committed.archived_audio_path, committed.archived_video_path):
        assert path
        assert storage.exists(path), f"committed reference lost its object: {path}"
    return committed


def old_video():
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://example.com/video.mp4",
        kind=ItemKind.VIDEO,
        archived_video_path="items/old/video.mp4",
    )
    storages["archive_media"].save(item.archived_video_path, ContentFile(b"old video"))
    return item


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure", [OSError, ItemProcessingStalled])
def test_second_upload_failure_preserves_previous_objects(monkeypatch, failure):
    item = old_video()
    item.archived_audio_path = "items/old/audio.mp3"
    item.save()
    storage = storages["archive_media"]
    storage.save(item.archived_audio_path, ContentFile(b"old audio"))
    fake_video_download(monkeypatch, item)
    save = storage.save
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure("second upload failed")
        return save(*args, **kwargs)

    monkeypatch.setattr(storage, "save", fail_second)
    with pytest.raises(
        MediaArchivalError if failure is OSError else ItemProcessingStalled,
        match="second upload failed",
    ):
        archive_item_audio(item)
    assert storage.open(item.archived_video_path).read() == b"old video"
    assert storage.open(item.archived_audio_path).read() == b"old audio"
    assert not PendingArchiveMediaUpload.objects.exists()
    assert set(iter_archive_media_object_names()) == {
        item.archived_audio_path,
        item.archived_video_path,
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("fail_save", [True, False])
@pytest.mark.parametrize("failure", [RuntimeError, ItemProcessingStalled])
def test_publication_preserves_old_until_database_commit(monkeypatch, fail_save, failure):
    item = old_video()
    old_path = item.archived_video_path
    storage = storages["archive_media"]
    fake_video_download(monkeypatch, item)
    save = item.save

    def check_save(*args, **kwargs):
        assert storage.open(old_path).read() == b"old video"
        if fail_save:
            raise failure("database save failed")
        save(*args, **kwargs)

    monkeypatch.setattr(item, "save", check_save)
    if fail_save:
        with pytest.raises(failure, match="database save failed"):
            enrich_item_media_archive(item)
        item.refresh_from_db()
        assert not PendingArchiveMediaUpload.objects.exists()
        assert item.archived_video_path == old_path
        assert list(iter_archive_media_object_names()) == [old_path]
    else:
        assert enrich_item_media_archive(item)
        item.refresh_from_db()
        assert item.archived_video_path != old_path
        assert not storage.exists(old_path)
        assert storage.exists(item.archived_audio_path)
        assert storage.exists(item.archived_video_path)


@pytest.mark.django_db(transaction=True)
def test_cleanup_during_upload_keeps_pending_object(monkeypatch):
    item = old_video()
    storage = storages["archive_media"]
    fake_video_download(monkeypatch, item)
    save = storage.save
    monkeypatch.setattr(storage, "get_modified_time", lambda path: datetime(2000, 1, 1, tzinfo=UTC))

    def cleanup_after_save(*args, **kwargs):
        path = save(*args, **kwargs)
        call_command("cleanup_archive_media_orphans", "--delete")
        assert storage.exists(path)
        return path

    monkeypatch.setattr(storage, "save", cleanup_after_save)
    assert enrich_item_media_archive(item)
    assert storage.exists(item.archived_audio_path)
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_cleanup_rechecks_reference_after_enumeration(monkeypatch):
    path = "items/race/audio.mp3"
    storage = storages["archive_media"]
    storage.save(path, ContentFile(b"published"))
    monkeypatch.setattr(storage, "get_modified_time", lambda path: datetime(2000, 1, 1, tzinfo=UTC))

    def enumerate_and_publish():
        Item.objects.create(original_url="https://example.com/race", archived_audio_path=path)
        return iter([path])

    monkeypatch.setattr(
        "archive.management.commands.cleanup_archive_media_orphans.iter_archive_media_object_names",
        enumerate_and_publish,
    )
    call_command("cleanup_archive_media_orphans", "--delete")
    assert storage.exists(path)


@pytest.mark.django_db(transaction=True)
def test_stall_after_commit_keeps_published_media(monkeypatch):
    item = old_video()
    old_path = item.archived_video_path
    fake_video_download(monkeypatch, item)
    connection = connections["default"]
    commit = connection.commit
    save = item.save
    armed = False

    def arm_after_save(*args, **kwargs):
        nonlocal armed
        save(*args, **kwargs)
        armed = True

    def stall_after_commit():
        nonlocal armed
        commit()
        if armed:
            armed = False
            raise ItemProcessingStalled("stalled after COMMIT")

    monkeypatch.setattr(item, "save", arm_after_save)
    monkeypatch.setattr(connection, "commit", stall_after_commit)
    with pytest.raises(ItemProcessingStalled, match="stalled after COMMIT"):
        enrich_item_media_archive(item)

    committed = assert_committed_references_exist(item)
    assert committed.archived_video_path != old_path
    assert item.archived_video_path == committed.archived_video_path
    assert item.archived_audio_path == committed.archived_audio_path
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_stall_during_post_commit_cleanup_keeps_published_media(monkeypatch):
    item = old_video()
    old_path = item.archived_video_path
    storage = storages["archive_media"]
    fake_video_download(monkeypatch, item)
    delete = storage.delete

    def stall_on_superseded(path):
        if path == old_path:
            raise ItemProcessingStalled("stalled deleting superseded media")
        delete(path)

    monkeypatch.setattr(storage, "delete", stall_on_superseded)
    with pytest.raises(ItemProcessingStalled, match="stalled deleting superseded"):
        enrich_item_media_archive(item)
    monkeypatch.setattr(storage, "delete", delete)

    committed = assert_committed_references_exist(item)
    assert committed.archived_video_path != old_path
    # The superseded object is merely orphaned and a later sweep collects it.
    assert storage.exists(old_path)
    call_command("cleanup_archive_media_orphans", "--delete", "--min-age-seconds", "0")
    assert not storage.exists(old_path)
    assert_committed_references_exist(item)


@pytest.mark.django_db(transaction=True)
def test_failed_discard_after_failed_publication_deletes_nothing(monkeypatch):
    item = old_video()
    old_path = item.archived_video_path
    fake_video_download(monkeypatch, item)

    def fail_save(*args, **kwargs):
        raise RuntimeError("database save failed")

    def fail_verification(*args, **kwargs):
        raise RuntimeError("verification unavailable")

    monkeypatch.setattr(item, "save", fail_save)
    monkeypatch.setattr(
        "archive.services.delete_unreferenced_archive_media_paths", fail_verification
    )
    with pytest.raises(RuntimeError, match="database save failed"):
        enrich_item_media_archive(item)

    # Unverifiable uploads stay behind, still reserved, and survive a sweep.
    assert item.archived_video_path == old_path
    assert len(set(iter_archive_media_object_names())) == 3
    assert PendingArchiveMediaUpload.objects.count() == 2
    monkeypatch.undo()
    call_command("cleanup_archive_media_orphans", "--delete", "--min-age-seconds", "0")
    assert len(set(iter_archive_media_object_names())) == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("owns_reservation", [False, True])
def test_delete_never_removes_committed_reference(owns_reservation):
    path = "items/7/audio/abc/source.mp3"
    storage = storages["archive_media"]
    storage.save(path, ContentFile(b"published"))
    Item.objects.create(original_url="https://example.com/live", archived_audio_path=path)
    PendingArchiveMediaUpload.objects.create(prefix="items/7/audio/abc/")

    assert not delete_unreferenced_archive_media_paths((path,), owns_reservation=owns_reservation)
    assert storage.exists(path)


@pytest.mark.django_db(transaction=True)
def test_delete_refuses_to_run_inside_a_caller_transaction():
    path = "items/7/audio/abc/source.mp3"
    storage = storages["archive_media"]
    storage.save(path, ContentFile(b"published"))
    item = Item.objects.create(original_url="https://example.com/live", archived_audio_path=path)

    with pytest.raises(RuntimeError, match="durable"), transaction.atomic():
        # The uncommitted removal of the reference must not authorise deletion.
        Item.objects.filter(pk=item.pk).update(archived_audio_path="")
        delete_unreferenced_archive_media_paths((path,))
    assert storage.exists(path)
    assert Item.objects.get(pk=item.pk).archived_audio_path == path


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("table", "publish_when"),
    [
        # Publication commits just before the reservation check: the reference
        # check that follows must see the committed reference.
        ("archive_pendingarchivemediaupload", "before"),
        # Publication commits just after the reservation check: the reservation
        # that was still visible must already have protected the object.
        ("archive_pendingarchivemediaupload", "after"),
        # An unreserved object gains a reference after candidate enumeration.
        ("archive_item", "before"),
    ],
)
def test_publication_during_cleanup_verification_keeps_object(table, publish_when):
    prefix = "items/9/audio/abc/"
    path = prefix + "source.mp3"
    storage = storages["archive_media"]
    storage.save(path, ContentFile(b"in flight"))
    if table == "archive_pendingarchivemediaupload":
        PendingArchiveMediaUpload.objects.create(prefix=prefix)
    item = Item.objects.create(original_url="https://example.com/in-flight")
    published = False

    def publish():
        nonlocal published
        published = True
        Item.objects.filter(pk=item.pk).update(archived_audio_path=path)
        PendingArchiveMediaUpload.objects.filter(prefix=prefix).delete()

    def publish_around_verification(execute, sql, params, many, context):
        verifying = not published and sql.startswith("SELECT") and f'FROM "{table}"' in sql
        verifying = verifying and (prefix in params or path in params)
        if verifying and publish_when == "before":
            publish()
        result = execute(sql, params, many, context)
        if verifying and publish_when == "after":
            publish()
        return result

    def enumerate_candidates():
        # Enumeration saw the object while it was still unreferenced.
        return iter([path])

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "archive.management.commands.cleanup_archive_media_orphans"
            ".iter_archive_media_object_names",
            enumerate_candidates,
        )
        with connections["default"].execute_wrapper(publish_around_verification):
            call_command("cleanup_archive_media_orphans", "--delete", "--min-age-seconds", "0")

    assert published
    assert storage.exists(path)
    assert Item.objects.get(pk=item.pk).archived_audio_path == path


@pytest.mark.django_db(transaction=True)
def test_cleanup_continues_when_object_age_is_unavailable(monkeypatch):
    class BackendError(Exception):
        """Stands in for a storage client error such as an S3 404."""

    storage = storages["archive_media"]
    vanished = "items/3/audio/gone.mp3"
    orphan = "items/3/audio/orphan.mp3"
    storage.save(vanished, ContentFile(b"vanishing"))
    storage.save(orphan, ContentFile(b"orphan"))

    def modified_time(path):
        if path == vanished:
            raise BackendError("404")
        return datetime(2000, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(storage, "get_modified_time", modified_time)
    call_command("cleanup_archive_media_orphans", "--delete")

    assert storage.exists(vanished)
    assert not storage.exists(orphan)
