from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import connection, connections
from django.utils import timezone

from archive import services
from archive.article_audio import ArticleAudioJobUpdate
from archive.media_archival import ArchivedAudio, save_reserved_media_file
from archive.models import EnrichmentStatus as Status
from archive.models import Item, ItemKind, PendingArchiveMediaUpload

pytestmark = pytest.mark.django_db(transaction=True)
STAGES = ("enrichment", "summary", "transcript", "media_archive", "article_audio")


def idle_item(**kwargs):
    values = {f"{stage}_status": Status.COMPLETE for stage in STAGES}
    values["original_url"] = "https://example.test/item"
    values.update(kwargs)
    return Item.objects.create(**values)


def other_worker_claim():
    def claim():
        try:
            row = services.claim_pending_item()
            return row.pk if row else None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(claim).result(timeout=5)


@pytest.mark.parametrize("stage", STAGES)
def test_claim_skips_item_with_any_inflight_stage(stage):
    inflight = idle_item(**{f"{stage}_status": Status.PROCESSING})
    # Another claimable stage must not make the active item available.
    Item.objects.filter(pk=inflight.pk).update(
        **{("summary_status" if stage == "enrichment" else "enrichment_status"): Status.PENDING}
    )
    independent = idle_item(enrichment_status=Status.PENDING)
    assert other_worker_claim() == independent.pk
    assert other_worker_claim() is None
    assert (
        services.recover_stale_processing_items(stale_before=timezone.now() - timedelta(days=1))
        == 0
    )
    assert (
        services.recover_stale_processing_items(stale_before=timezone.now() + timedelta(seconds=1))
        == 2
    )
    assert services.claim_pending_item() is not None


@pytest.mark.parametrize("handoff", [False, True])
def test_new_stage_is_acquired_or_left_with_its_new_owner(monkeypatch, handoff):
    item = idle_item(enrichment_status=Status.PENDING, transcript_status=Status.PENDING)
    claimed = services.claim_pending_item()
    assert claimed.pk == item.pk
    calls = []

    def metadata(item, timeout):
        assert not connection.in_atomic_block
        item.enrichment_status = Status.COMPLETE
        item.kind = ItemKind.PODCAST_EPISODE
        item.audio_url = "https://example.test/audio.mp3"
        item.save(update_fields=["enrichment_status", "kind", "audio_url"])
        if handoff:
            assert other_worker_claim() == item.pk
        return True

    def transcribe(item, timeout):
        assert not connection.in_atomic_block
        assert Item.objects.get(pk=item.pk).transcript_status == Status.PROCESSING
        assert other_worker_claim() is None
        calls.append(item.pk)
        item.transcript_status = Status.COMPLETE
        item.save(update_fields=["transcript_status"])
        return True

    monkeypatch.setattr(services, "_enrich_item_metadata_fields", metadata)
    monkeypatch.setattr(services, "enrich_item_transcript", transcribe)
    assert services.enrich_item_metadata(claimed)
    assert calls == ([] if handoff else [item.pk])


@pytest.mark.parametrize("change", ["source", "kind", "references"])
def test_media_publication_discards_superseded_upload(monkeypatch, change):
    item = idle_item(
        original_url="https://example.test/old.mp4",
        media_url="https://example.test/old.mp4",
        kind=ItemKind.VIDEO,
        archived_video_path="old/video.mp4",
        media_archive_status=Status.PENDING,
    )
    storage = storages["archive_media"]
    storage.save(item.archived_video_path, ContentFile(b"old referenced bytes"))
    paths = []

    def download(item, timeout):
        assert not connection.in_atomic_block
        name = save_reserved_media_file(
            target_name="staged/audio.mp3",
            upload_name="audio.mp3",
            file_handle=BytesIO(b"losing bytes"),
            using="default",
        )
        paths.append(name)
        updates = {}
        if change == "source":
            updates["media_url"] = "https://example.test/new.mp4"
        if change == "kind":
            updates["kind"] = ItemKind.QUOTE
        if change == "references":
            storage.save("new/audio.mp3", ContentFile(b"new referenced bytes"))
            updates["archived_audio_path"] = "new/audio.mp3"
        Item.objects.filter(pk=item.pk).update(**updates)
        return ArchivedAudio(name, "audio/mpeg", len(b"losing bytes"))

    monkeypatch.setattr(services, "archive_item_audio", download)
    assert services.enrich_item_media_archive(item)
    item.refresh_from_db()
    assert item.archived_audio_path == ("new/audio.mp3" if change == "references" else "")
    assert storage.open(item.archived_video_path).read() == b"old referenced bytes"
    if change == "references":
        assert storage.open(item.archived_audio_path).read() == b"new referenced bytes"
    assert not storage.exists(paths[0])
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.parametrize("state", ["running", "failed", "succeeded", "exception"])
@pytest.mark.parametrize("change", ["job", "kind", "source", "text"])
def test_synthesis_results_cannot_restore_superseded_state(monkeypatch, state, change):
    item = idle_item(
        kind=ItemKind.ARTICLE,
        short_summary="Article text",
        article_audio_job_id="observed",
        article_audio_status=Status.PROCESSING,
    )
    poll_at = timezone.now() + timedelta(minutes=10)

    def generate(item, timeout):
        assert not connection.in_atomic_block
        updates = {}
        if change == "job":
            updates.update(
                article_audio_job_id="replacement",
                article_audio_status=Status.PENDING,
                article_audio_poll_at=poll_at,
            )
        if change == "kind":
            updates["kind"] = ItemKind.QUOTE
        if change == "source":
            updates["original_url"] = "https://example.test/replacement"
        if change == "text":
            updates["short_summary"] = "Changed text"
        Item.objects.filter(pk=item.pk).update(**updates)
        if state == "exception":
            raise OSError("old failure")
        return ArticleAudioJobUpdate("observed", state, "/old.mp3", "old failure")

    monkeypatch.setattr(services, "generate_item_article_audio", generate)
    monkeypatch.setattr(
        services, "store_generated_article_audio", lambda **k: pytest.fail("stale copy")
    )
    assert services.enrich_item_article_audio(item)
    item.refresh_from_db()
    assert item.article_audio_job_id == ("replacement" if change == "job" else "observed")
    assert item.article_audio_status == (Status.PENDING if change == "job" else Status.PROCESSING)
    assert item.article_audio_poll_at == (poll_at if change == "job" else None)
    assert item.article_audio_artifact_path == ""
    assert not item.article_audio_generated


@pytest.mark.parametrize("change", ["source", "references"])
def test_media_exception_preserves_newer_state(monkeypatch, change):
    item = idle_item(
        audio_url="https://example.test/old.mp3", media_archive_status=Status.PROCESSING
    )

    def download(item, timeout):
        updates = (
            {"audio_url": "https://example.test/new.mp3"}
            if change == "source"
            else {"archived_audio_path": "new/audio.mp3"}
        )
        Item.objects.filter(pk=item.pk).update(**updates)
        raise OSError("superseded failure")

    monkeypatch.setattr(services, "archive_item_audio", download)
    assert services.enrich_item_media_archive(item)
    item.refresh_from_db()
    assert item.media_archive_status == Status.PROCESSING
    assert item.media_archive_retry_count == 0
    assert item.media_archive_error == ""


def test_superseded_media_cleanup_failure_is_contained(monkeypatch, caplog):
    item = idle_item(audio_url="https://example.test/old.mp3", media_archive_status=Status.PENDING)
    paths = []

    def download(item, timeout):
        name = save_reserved_media_file(
            target_name="staged/audio.mp3",
            upload_name="audio.mp3",
            file_handle=BytesIO(b"losing"),
            using="default",
        )
        paths.append(name)
        Item.objects.filter(pk=item.pk).update(audio_url="https://example.test/new.mp3")
        return ArchivedAudio(name, "audio/mpeg", 6)

    def fail_cleanup(*args, **kwargs):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(services, "archive_item_audio", download)
    monkeypatch.setattr(services, "delete_unreferenced_archive_media_paths", fail_cleanup)
    assert services.enrich_item_media_archive(item)
    assert "Failed to discard superseded media" in caplog.text
    assert PendingArchiveMediaUpload.objects.exists()
    assert storages["archive_media"].exists(paths[0])
