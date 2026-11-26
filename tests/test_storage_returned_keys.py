"""Storage backends must keep saved objects inside the reserved upload namespace."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import call_command
from test_media_archival import _FakeResponse

from archive.media_archival import (
    ArchiveMediaStorageKeyError,
    MediaArchivalError,
    save_reserved_media_file,
)
from archive.models import EnrichmentStatus, Item, ItemKind, PendingArchiveMediaUpload
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


def _relocating_save(monkeypatch, relocate, *, during=None):
    storage = storages["archive_media"]
    save = storage.save
    calls: list[tuple[str, str]] = []

    def relocated_save(name, content, *args, **kwargs):
        actual = relocate(name)
        if actual is None:
            calls.append((name, ""))
            return None
        stored = save(actual, content, *args, **kwargs)
        calls.append((name, stored))
        if during is not None:
            during(stored)
        return stored

    monkeypatch.setattr(storage, "save", relocated_save)
    return calls


def _old_mtime(monkeypatch):
    storage = storages["archive_media"]
    monkeypatch.setattr(storage, "get_modified_time", lambda path: datetime(2000, 1, 1, tzinfo=UTC))


def _podcast_item() -> Item:
    return Item.objects.create(
        original_url="https://example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
    )


@pytest.mark.django_db(transaction=True)
def test_relocated_key_is_refused_before_publication_while_sweeper_runs(monkeypatch):
    item = _podcast_item()
    storage = storages["archive_media"]
    _old_mtime(monkeypatch)
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse([b"audio"]),
    )

    def sweep_while_paused(stored):
        call_command("cleanup_archive_media_orphans", "--delete")

    calls = _relocating_save(
        monkeypatch,
        lambda name: f"items/{item.pk}/relocated/source.mp3",
        during=sweep_while_paused,
    )

    assert enrich_item_media_archive(item) is False

    item.refresh_from_db()
    assert calls, "storage.save was not reached"
    assert item.archived_audio_path == ""
    assert item.media_archive_status != EnrichmentStatus.COMPLETE
    assert "storage key" in item.media_archive_error
    assert not PendingArchiveMediaUpload.objects.exists()
    # Nothing references a missing or unprotected object.
    for path in (item.archived_audio_path, item.archived_video_path):
        assert not path or storage.exists(path)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "relocate",
    [
        pytest.param(
            lambda name: name.rsplit("/", 2)[0] + "/other/source.mp3", id="sibling-prefix"
        ),
        pytest.param(lambda name: name.rsplit("/", 1)[0] + "/nested/source.mp3", id="nested"),
        pytest.param(lambda name: name.rsplit("/", 1)[0] + "/../x/source.mp3", id="parent-segment"),
        pytest.param(lambda name: "unrelated/source.mp3", id="unrelated"),
        pytest.param(lambda name: " " + name, id="untrimmed"),
        pytest.param(lambda name: None, id="none"),
    ],
)
def test_keys_outside_the_reserved_prefix_are_refused(monkeypatch, relocate):
    storage = storages["archive_media"]
    save = storage.save
    intended: list[str] = []

    def misreporting_save(name, content, *args, **kwargs):
        intended.append(save(name, content, *args, **kwargs))
        return relocate(name)

    monkeypatch.setattr(storage, "save", misreporting_save)

    with pytest.raises(ArchiveMediaStorageKeyError, match="storage key"):
        save_reserved_media_file(
            target_name="items/1/audio/source.mp3",
            upload_name="source.mp3",
            file_handle=BytesIO(b"audio"),
            using="default",
        )

    assert issubclass(ArchiveMediaStorageKeyError, MediaArchivalError)
    assert not PendingArchiveMediaUpload.objects.exists()
    # The object written inside the reserved prefix is provably ours and removed.
    assert intended and not storage.exists(intended[0])


@pytest.mark.django_db(transaction=True)
def test_refusal_never_touches_another_uploads_reservation_or_object(monkeypatch):
    storage = storages["archive_media"]
    live_prefix = "items/1/audio/" + "f" * 32 + "/"
    live_object = live_prefix + "source.mp3"
    PendingArchiveMediaUpload.objects.create(prefix=live_prefix)
    storage.save(live_object, ContentFile(b"live upload"))
    # A misbehaving backend reports the live upload's key as our result.
    monkeypatch.setattr(storage, "save", lambda name, content, *a, **k: live_object)

    with pytest.raises(ArchiveMediaStorageKeyError):
        save_reserved_media_file(
            target_name="items/1/audio/source.mp3",
            upload_name="source.mp3",
            file_handle=BytesIO(b"audio"),
            using="default",
        )

    assert list(PendingArchiveMediaUpload.objects.values_list("prefix", flat=True)) == [live_prefix]
    assert storage.exists(live_object)


@pytest.mark.django_db(transaction=True)
def test_storage_key_refusal_survives_warning_failure(monkeypatch, caplog):
    item = _podcast_item()
    storage = storages["archive_media"]
    unrelated = storage.save("items/unrelated/source.mp3", ContentFile(b"unrelated audio"))
    save = storage.save
    intended: list[str] = []

    def misreporting_save(name, content, *args, **kwargs):
        intended.append(save(name, content, *args, **kwargs))
        return unrelated

    def fail_warning(*args, **kwargs):
        raise OSError("synthetic logging failure")

    monkeypatch.setattr(storage, "save", misreporting_save)
    monkeypatch.setattr("archive.media_archival.logger.warning", fail_warning)
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse([b"audio"]),
    )

    assert enrich_item_media_archive(item) is False
    item.refresh_from_db()
    assert (
        item.media_archive_error
        == "Archive media storage key escaped its reserved upload namespace"
    )
    assert any(
        record.exc_info and isinstance(record.exc_info[1], ArchiveMediaStorageKeyError)
        for record in caplog.records
    )
    assert not item.archived_audio_path
    assert not PendingArchiveMediaUpload.objects.exists()
    assert intended and not storage.exists(intended[0])
    with storage.open(unrelated) as uploaded:
        assert uploaded.read() == b"unrelated audio"


class _FailingHandler(logging.Handler):
    def emit(self, record):
        raise OSError("synthetic shared handler failure")


@pytest.mark.django_db(transaction=True)
def test_storage_key_refusal_is_recorded_when_a_shared_log_handler_fails(monkeypatch):
    item = _podcast_item()
    item.media_archive_status = EnrichmentStatus.PROCESSING
    item.save()
    storage = storages["archive_media"]
    unrelated = storage.save("items/unrelated/source.mp3", ContentFile(b"unrelated audio"))
    save = storage.save

    def misreporting_save(name, content, *args, **kwargs):
        save(name, content, *args, **kwargs)
        return unrelated

    monkeypatch.setattr(storage, "save", misreporting_save)
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse([b"audio"]),
    )
    handler = _FailingHandler()
    shared = logging.getLogger("archive")
    shared.addHandler(handler)
    try:
        assert enrich_item_media_archive(item) is False
    finally:
        shared.removeHandler(handler)
    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert (
        item.media_archive_error
        == "Archive media storage key escaped its reserved upload namespace"
    )
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_available_name_suffix_inside_the_prefix_is_accepted(monkeypatch):
    storage = storages["archive_media"]
    calls = _relocating_save(
        monkeypatch, lambda name: name.replace("source.mp3", "source_AbC123.mp3")
    )

    stored = save_reserved_media_file(
        target_name="items/1/audio/source.mp3",
        upload_name="source.mp3",
        file_handle=BytesIO(b"audio"),
        using="default",
    )

    assert stored == calls[0][1]
    assert stored.endswith("/source_AbC123.mp3")
    assert storage.exists(stored)
    (reservation,) = PendingArchiveMediaUpload.objects.values_list("prefix", flat=True)
    assert stored == reservation + "source_AbC123.mp3"
