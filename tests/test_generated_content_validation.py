import json
from functools import partial
from tempfile import NamedTemporaryFile, TemporaryDirectory

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from test_direct_media_deadline import media_server  # noqa: F401

from archive import article_audio, summaries, upstream_http
from archive.models import EnrichmentStatus, Item, ItemKind, PendingArchiveMediaUpload
from archive.services import enrich_item_summaries


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        1,
        "text",
        {"short_summary": None, "long_summary": {}, "tags": [None]},
        *[
            {"short_summary": short, "long_summary": long, "tags": tags}
            for short, long, tags in [
                (False, "Long", ["tag"]),
                (" ", "Long", ["tag"]),
                ("Short", 4, ["tag"]),
                ("Short", "Long", ["tag", 4]),
                ("Short", "Long", []),
                ("Short", "Long", ["#"]),
                ("Short", "Long", "tag"),
                ("Short", "Long", ["tag", None]),
                ("Short", "Long", ["tag", "　"]),
                ("Short", "Long", ["tag", "#\u00a0-"]),
            ]
        ],
    ],
)
def test_generated_summary_requires_text_schema(payload):
    with pytest.raises(summaries.SummaryGenerationError):
        summaries._parse_generated_summary(json.dumps(payload))


def test_valid_multilingual_summary_normalization():
    result = summaries._parse_generated_summary(
        json.dumps(
            {
                "short_summary": " Grüße\n zusammen ",
                "long_summary": " Längerer  Text ",
                "tags": [" #Über ", "ÜBER", "日本語"],
            }
        )
    )
    assert result == summaries.GeneratedSummary(
        "Grüße zusammen", "Längerer Text", ("über", "日本語")
    )


@pytest.mark.django_db
def test_bad_summary_never_publishes_and_retries(monkeypatch):
    item = Item.objects.create(
        original_url="https://example.test/article", short_summary="Retained"
    )

    def generate(**kwargs):
        return summaries._parse_generated_summary(
            '{"short_summary":null,"long_summary":{},"tags":[null]}'
        )

    monkeypatch.setattr("archive.services.generate_item_summaries", generate)
    assert not enrich_item_summaries(item)
    item.refresh_from_db()
    assert item.short_summary == "Retained"
    assert item.long_summary == item.tags == ""
    assert item.summary_status == EnrichmentStatus.FAILED
    assert item.summary_retry_count == 1
    assert item.summary_retry_at is not None


@pytest.mark.django_db(transaction=True)
def test_article_copy_timeout_discards_temporary_and_preserves_media(
    media_server,  # noqa: F811
    settings,
    tmp_path,
    monkeypatch,  # noqa: F811
):
    settings.ARCHIVE_ARTICLE_AUDIO_API_BASE = media_server
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        article_audio_artifact_path="/trickle.mp3",
        article_audio_generated=True,
        article_audio_status=EnrichmentStatus.COMPLETE,
        archived_audio_path="retained.mp3",
    )
    storage = storages["archive_media"]
    storage.save("retained.mp3", ContentFile(b"retained"))
    monkeypatch.setattr(
        article_audio, "NamedTemporaryFile", partial(NamedTemporaryFile, dir=tmp_path)
    )
    monkeypatch.setattr(
        upstream_http, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path)
    )
    with pytest.raises(article_audio.ArticleAudioGenerationError, match="total timeout"):
        article_audio.store_generated_article_audio(item, timeout=0.5)
    item.refresh_from_db()
    assert not item.article_audio_file
    assert item.article_audio_artifact_path == "/trickle.mp3"
    assert item.article_audio_generated
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert storage.open(item.archived_audio_path).read() == b"retained"
    assert not PendingArchiveMediaUpload.objects.exists()
    assert not list(tmp_path.glob("archive-article-audio-*"))
    assert not list(tmp_path.glob("archive-upstream-*"))
