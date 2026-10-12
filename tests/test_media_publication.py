from datetime import UTC, datetime

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from test_media_archival import _fake_ffmpeg_run, _FakeResponse

from archive.management.commands.run_metadata_worker import ItemProcessingStalled
from archive.media_archival import MediaArchivalError, archive_item_audio
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
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda *a, **k: _FakeResponse(
            [b"video"], content_type="video/mp4", final_url=item.media_url
        ),
    )
    monkeypatch.setattr("archive.media_archival.subprocess.run", _fake_ffmpeg_run)
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
    from archive.media_storage import iter_archive_media_object_names

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
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda *a, **k: _FakeResponse(
            [b"video"], content_type="video/mp4", final_url=item.media_url
        ),
    )
    monkeypatch.setattr("archive.media_archival.subprocess.run", _fake_ffmpeg_run)
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
        from archive.media_storage import iter_archive_media_object_names

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
    from django.core.management import call_command

    from archive.models import PendingArchiveMediaUpload

    item = old_video()
    storage = storages["archive_media"]
    monkeypatch.setattr(
        "archive.media_archival.urlopen",
        lambda *a, **k: _FakeResponse(
            [b"video"], content_type="video/mp4", final_url=item.media_url
        ),
    )
    monkeypatch.setattr("archive.media_archival.subprocess.run", _fake_ffmpeg_run)
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
    from django.core.management import call_command

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
