from contextlib import nullcontext
from io import BytesIO, StringIO

import pytest
from django.core.files.storage import storages
from django.core.management import call_command
from django.db import OperationalError
from test_attempt_ownership import idle_item

from archive import services
from archive.article_audio import ArticleAudioJobUpdate
from archive.management.commands import run_metadata_worker as worker
from archive.media_archival import ArchivedAudio, save_reserved_media_file
from archive.metadata import ExtractedMetadata, extract_metadata_from_html
from archive.models import EnrichmentStatus as Status
from archive.models import Item, ItemKind, PendingArchiveMediaUpload, ProcessingEvent
from archive.summaries import GeneratedSummary

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "stage", ["enrichment", "media_archive", "transcript", "summary", "article_audio"]
)
@pytest.mark.parametrize("failure", [False, True])
def test_deleted_claim_retires_and_worker_processes_sibling(monkeypatch, stage, failure):
    item = idle_item(
        kind=ItemKind.ARTICLE if stage == "article_audio" else ItemKind.PODCAST_EPISODE,
        audio_url="https://example.test/a.mp3" if stage != "article_audio" else "",
        short_summary="Article text" if stage == "article_audio" else "",
        **{f"{stage}_status": Status.PENDING},
    )
    pk = item.pk
    sibling = idle_item(summary_status=Status.PENDING)
    paths = []
    calls = []

    def generate(*args, **kwargs):
        current = kwargs.get("item")
        if current and current.pk == sibling.pk:
            calls.append(current.pk)
            return GeneratedSummary("Sibling", "Sibling long", ("sibling",))
        if stage == "media_archive" and not failure:
            paths.append(
                save_reserved_media_file(
                    target_name="retired/audio.mp3",
                    upload_name="audio.mp3",
                    file_handle=BytesIO(b"synthetic audio"),
                    using="default",
                )
            )
        Item.objects.filter(pk=pk).delete()
        if failure:
            raise OSError("synthetic upstream failure after deletion")
        return {
            "enrichment": ExtractedMetadata(title="Retired"),
            "media_archive": ArchivedAudio(paths[0] if paths else "", "audio/mpeg", 15),
            "transcript": "Retired words",
            "summary": GeneratedSummary("Retired", "Retired long", ("retired",)),
            "article_audio": ArticleAudioJobUpdate("retired-job", "succeeded", "/retired.mp3"),
        }[stage]

    generator = {
        "enrichment": "extract_metadata_from_url",
        "media_archive": "archive_item_audio",
        "transcript": "generate_item_transcript",
        "summary": "generate_item_summaries",
        "article_audio": "generate_item_article_audio",
    }[stage]
    monkeypatch.setattr(services, "generate_item_summaries", generate)
    monkeypatch.setattr(services, generator, generate)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker.Command, "_item_processing_timeout", lambda *a: nullcontext())
    output = StringIO()
    call_command("run_metadata_worker", once=True, limit=2, stdout=output)
    sibling.refresh_from_db()
    assert sibling.short_summary == "Sibling"
    assert calls == [sibling.pk]
    assert not Item.objects.filter(pk=pk).exists()
    assert not ProcessingEvent.objects.filter(item_id=pk).exists()
    assert not PendingArchiveMediaUpload.objects.exists()
    assert all(not storages["archive_media"].exists(path) for path in paths)
    assert f"Completed item {pk}" not in output.getvalue()


def test_unrelated_database_failure_is_not_retired(monkeypatch):
    idle_item(summary_status=Status.PENDING)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker.Command, "_item_processing_timeout", lambda *a: nullcontext())

    def fail(**kwargs):
        raise OperationalError("synthetic database failure")

    monkeypatch.setattr(worker, "enrich_item_metadata", fail)
    with pytest.raises(OperationalError):
        call_command("run_metadata_worker", once=True)


@pytest.mark.parametrize("provider", ["html", "jsonld"])
def test_extracted_display_metadata_obeys_model_widths(monkeypatch, provider, client):
    import json

    value = "Grüße 😀 " * 300
    html = (
        (
            f'<meta property="og:title" content="{value}">'
            f'<meta property="og:site_name" content="{value}">'
            f'<meta name="author" content="{value}">'
        )
        if provider == "html"
        else '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Article",
                "headline": value,
                "author": {"name": value},
                "publisher": {"name": value},
            }
        )
        + "</script>"
    )
    metadata = extract_metadata_from_html(html, "https://example.test/oversized")
    for field in ("title", "source", "author"):
        limit = Item._meta.get_field(field).max_length
        assert getattr(metadata, field) == value.strip()[:limit]
    item = idle_item(enrichment_status=Status.PROCESSING, author="Manual author")
    monkeypatch.setattr(services, "extract_metadata_from_url", lambda *a, **k: metadata)
    assert services._enrich_item_metadata_fields(item)
    item.refresh_from_db()
    assert item.author == "Manual author"
    assert len(item.title) == 500
    assert len(item.source) == 255
    response = client.get("/")
    assert response.status_code == 200
    assert value.encode() not in response.content
