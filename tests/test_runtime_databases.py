import pytest
from django.db.models.signals import post_save

from archive import services
from archive.capture import capture_item
from archive.metadata import ExtractedMetadata
from archive.models import EnrichmentStatus as Status
from archive.models import Item, ItemKind, ProcessingEvent
from archive.summaries import GeneratedSummary

pytestmark = pytest.mark.django_db(databases=["default", "secondary"], transaction=True)


class ArchiveRouter:
    def db_for_read(self, model, **hints):
        return "secondary" if model._meta.app_label == "archive" else None

    db_for_write = db_for_read


@pytest.fixture
def routed(settings):
    settings.DATABASE_ROUTERS = [ArchiveRouter()]
    Item.objects.using("default").create(original_url="https://example.test/default-sentinel")


def test_capture_uses_routed_identity_and_rollback(routed):
    first, created = capture_item(Item(original_url="https://example.test/repeated"))
    assert created and first._state.db == "secondary"
    repeated, created = capture_item(Item(original_url=first.original_url))
    assert not created and repeated.pk == first.pk

    def fail(sender, instance, **kwargs):
        raise RuntimeError("synthetic post-save failure")

    post_save.connect(fail, sender=Item)
    try:
        with pytest.raises(RuntimeError):
            capture_item(Item(original_url="https://example.test/rolled-back"))
    finally:
        post_save.disconnect(fail, sender=Item)
    assert Item.objects.using("secondary").count() == 1
    assert Item.objects.using("default").count() == 1


@pytest.mark.parametrize("stage", ["summary", "transcript", "enrichment"])
@pytest.mark.parametrize("failure", [False, True])
def test_stage_and_health_share_routed_transaction(routed, monkeypatch, stage, failure):
    item = Item.objects.create(
        original_url="https://example.test/routed",
        kind=ItemKind.PODCAST_EPISODE,
        audio_url="https://example.test/a.mp3",
        **{
            f"{s}_status": Status.PROCESSING if s == stage else Status.COMPLETE
            for s in ("summary", "transcript", "enrichment", "media_archive", "article_audio")
        },
    )

    def generate(*args, **kwargs):
        if failure:
            raise OSError("synthetic failure")
        return {
            "summary": GeneratedSummary("Short", "Long", ("tag",)),
            "transcript": "Words",
            "enrichment": ExtractedMetadata(title="Title"),
        }[stage]

    generator, enrich = {
        "summary": ("generate_item_summaries", services.enrich_item_summaries),
        "transcript": ("generate_item_transcript", services.enrich_item_transcript),
        "enrichment": ("extract_metadata_from_url", services._enrich_item_metadata_fields),
    }[stage]
    monkeypatch.setattr(services, generator, generate)
    original_record = services.record_step_outcome

    def interrupt(*args, **kwargs):
        original_record(*args, **kwargs)
        raise RuntimeError("synthetic publication failure")

    with monkeypatch.context() as m:
        m.setattr(services, "record_step_outcome", interrupt)
        with pytest.raises(RuntimeError, match="publication failure"):
            enrich(item)
    current = Item.objects.using("secondary").get(pk=item.pk)
    assert getattr(current, f"{stage}_status") == Status.PROCESSING
    assert not current.short_summary and not current.transcript and not current.title
    assert not ProcessingEvent.objects.using("secondary").exists()
    assert not ProcessingEvent.objects.using("default").exists()
    assert enrich(current) is not failure
    assert ProcessingEvent.objects.using("secondary").count() == 1
    assert not ProcessingEvent.objects.using("default").exists()
    assert (
        Item.objects.using("default").get().original_url == "https://example.test/default-sentinel"
    )
