import json
from datetime import timedelta
from xml.etree import ElementTree

import pytest
from django.utils import timezone
from test_attempt_ownership import idle_item

from archive import services, views
from archive.models import EnrichmentStatus as Status
from archive.models import Item, ItemKind, ProcessingEvent
from archive.summaries import GeneratedSummary

pytestmark = pytest.mark.django_db(transaction=True)
HEADERS = {"Authorization": "Bearer test-api-token"}
STAGE_CALLS = {
    "summary": ("generate_item_summaries", services.enrich_item_summaries),
    "transcript": ("generate_item_transcript", services.enrich_item_transcript),
    "media_archive": ("archive_item_audio", services.enrich_item_media_archive),
    "article_audio": ("generate_item_article_audio", services.enrich_item_article_audio),
}


def attempt(stage):
    return idle_item(
        kind=ItemKind.ARTICLE if stage == "article_audio" else ItemKind.PODCAST_EPISODE,
        audio_url="" if stage == "article_audio" else "https://example.test/audio.mp3",
        short_summary="Article text" if stage == "article_audio" else "",
        **{f"{stage}_status": Status.PROCESSING},
    )


@pytest.mark.parametrize("stage", STAGE_CALLS)
@pytest.mark.parametrize("change", ["success", "source", "attempt", "input"])
def test_stale_failure_preserves_current_state(monkeypatch, stage, change):
    item = attempt(stage)
    updates = {"original_url": "https://example.test/changed"}
    if change == "success":
        updates = {f"{stage}_status": Status.COMPLETE}
        updates.update(
            {
                "summary": {
                    "short_summary": "New short",
                    "long_summary": "New long",
                    "tags": "new",
                    "summary_retry_count": 0,
                    "summary_retry_at": None,
                },
                "transcript": {"transcript": "New transcript", "transcript_generated": True},
                "media_archive": {
                    "archived_audio_path": "new/audio.mp3",
                    "media_archive_retry_count": 0,
                    "media_archive_retry_at": None,
                },
                "article_audio": {
                    "article_audio_job_id": "new-job",
                    "article_audio_generated": True,
                    "article_audio_artifact_path": "/new.mp3",
                    "article_audio_poll_at": None,
                },
            }[stage]
        )
    if change == "attempt":
        updates = {"processing_started_at": timezone.now() + timedelta(seconds=1)}
    if change == "input":
        updates = {
            "summary": {"transcript": "New summary input"},
            "transcript": {"archived_audio_path": "new/source.mp3"},
            "media_archive": {"archived_video_path": "new/source.mp4"},
            "article_audio": {"title": "New script title"},
        }[stage]
    snapshot = []

    def fail(**kwargs):
        Item.objects.filter(pk=item.pk).update(**updates)
        snapshot.append(Item.objects.get(pk=item.pk))
        raise OSError("old attempt failed")

    generator, enrich = STAGE_CALLS[stage]
    monkeypatch.setattr(services, generator, fail)
    enrich(item)
    item.refresh_from_db()
    assert snapshot
    assert not ProcessingEvent.objects.filter(item_id=item.pk).exists()
    for field in (*updates, f"{stage}_error", f"{stage}_status"):
        assert getattr(item, field) == getattr(snapshot[0], field)
    if stage in {"summary", "media_archive"}:
        assert getattr(item, f"{stage}_retry_count") == 0


@pytest.mark.parametrize("stage", ["summary", "media_archive"])
def test_failures_from_stale_instances_increment_current_retry_count(monkeypatch, stage):
    # Direct calls without a worker lease still serialize counter updates from
    # stale instances. Claimed attempts instead require an exact timestamp match.
    item = idle_item(
        kind=ItemKind.PODCAST_EPISODE,
        audio_url="https://example.test/audio.mp3",
        **{f"{stage}_status": Status.PENDING},
    )
    stale = Item.objects.get(pk=item.pk)
    generator, enrich = STAGE_CALLS[stage]

    def fail(**kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr(services, generator, fail)
    before = timezone.now()
    assert not enrich(item)
    assert not enrich(stale)
    item.refresh_from_db()
    assert getattr(item, f"{stage}_retry_count") == 2
    assert getattr(item, f"{stage}_status") == Status.FAILED
    assert before + timedelta(minutes=30) <= getattr(item, f"{stage}_retry_at")
    assert getattr(item, f"{stage}_retry_at") < timezone.now() + timedelta(minutes=31)


def test_incomplete_generated_summary_leaves_a_failed_retryable_stage(monkeypatch):
    item = attempt("summary")
    monkeypatch.setattr(
        services, "generate_item_summaries", lambda **k: GeneratedSummary("New short", "", ("tag",))
    )
    assert not services.enrich_item_summaries(item)
    item.refresh_from_db()
    assert item.summary_status == Status.FAILED
    assert item.summary_retry_count == 1


@pytest.mark.parametrize("method", ["post", "patch"])
@pytest.mark.parametrize("shape", ["array", "object"])
def test_deep_api_json_is_a_controlled_400(client, method, shape):
    item = idle_item()
    url = "/api/items/" if method == "post" else f"/api/items/{item.pk}/"
    payload = (
        "[" * 10000 + "0" + "]" * 10000
        if shape == "array"
        else '{"nested":' * 10000 + "0" + "}" * 10000
    )
    before = Item.objects.count()
    response = getattr(client, method)(
        url, data=payload, content_type="application/json", headers=HEADERS
    )
    assert response.status_code == 400
    assert Item.objects.count() == before
    item.refresh_from_db()
    assert item.kind == ItemKind.LINK


@pytest.mark.parametrize("method", ["post", "patch"])
def test_decoder_recursion_uses_invalid_json_contract_and_auth_first(client, monkeypatch, method):
    item = idle_item()
    url = "/api/items/" if method == "post" else f"/api/items/{item.pk}/"
    real_loads = json.loads
    calls = []

    def decode(value, *args, **kwargs):
        if value == b"synthetic recursion":
            calls.append(value)
            raise RecursionError("private body diagnostic")
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(views.json, "loads", decode)
    request = getattr(client, method)
    assert (
        request(url, data=b"synthetic recursion", content_type="application/json").status_code
        == 401
    )
    assert not calls
    response = request(
        url, data=b"synthetic recursion", content_type="application/json", headers=HEADERS
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid JSON"}
    assert calls == [b"synthetic recursion"]


@pytest.mark.parametrize("feed", ["rss", "podcast"])
def test_rss_replaces_forbidden_xml_characters_without_changing_stored_text(client, feed):
    title = "Title\x01 & Grüße 😀"
    description = "Résumé\x0b <ok> \ufffe"
    item = idle_item(
        kind=ItemKind.PODCAST_EPISODE,
        title=title,
        short_summary=description,
        long_summary=description,
        archived_audio_path="synthetic/audio.mp3",
        archived_audio_content_type="audio/mpeg\x01",
        archived_audio_size_bytes=7,
    )
    response = client.get(f"/feeds/{feed}.xml")
    assert response.status_code == 200
    root = ElementTree.fromstring(response.content)
    entry = root.find("channel/item")
    assert entry is not None
    assert entry.findtext("title") == "Title\ufffd & Grüße 😀"
    assert entry.findtext("description") == "Résumé\ufffd <ok> \ufffd"
    if feed == "podcast":
        assert entry.find("enclosure").get("type") == "audio/mpeg\ufffd"
    item.refresh_from_db()
    assert item.title == title
    assert item.short_summary == item.long_summary == description


@pytest.mark.parametrize("stage", ["summary", "media_archive"])
def test_recovered_attempt_cannot_change_a_newer_settled_failure(monkeypatch, stage):
    old = attempt(stage)
    old_started_at = old.processing_started_at
    assert services.recover_processing_item(old.pk, expected_started_at=old_started_at) == 1
    newer = services.claim_pending_item()
    assert newer.pk == old.pk
    assert newer.processing_started_at != old_started_at
    generator, enrich = STAGE_CALLS[stage]

    def fail_newer(**kwargs):
        raise OSError("newer failure")

    monkeypatch.setattr(services, generator, fail_newer)
    assert not enrich(newer)
    settled = Item.objects.get(pk=old.pk)
    assert settled.processing_started_at is None
    events = ProcessingEvent.objects.filter(item_id=old.pk).count()

    def fail_older(**kwargs):
        raise OSError("older failure")

    monkeypatch.setattr(services, generator, fail_older)
    assert enrich(old)
    current = Item.objects.get(pk=old.pk)
    assert getattr(current, f"{stage}_retry_count") == 1
    assert getattr(current, f"{stage}_error") == "newer failure"
    assert getattr(current, f"{stage}_retry_at") == getattr(settled, f"{stage}_retry_at")
    assert ProcessingEvent.objects.filter(item_id=old.pk).count() == events
