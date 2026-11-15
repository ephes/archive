import io
import json

import pytest

from archive import summaries
from archive.models import Item, ItemKind
from archive.summaries import SummaryGenerationError, SummarySource

REQUEST_CAP = 64 * 1024


@pytest.fixture
def model_client(monkeypatch, settings):
    settings.ARCHIVE_SUMMARY_API_KEY = "synthetic"
    requests = []
    monkeypatch.setattr(summaries, "_best_effort_summary_source", lambda **kw: SummarySource())

    def respond(request, **kwargs):
        requests.append(request.data)
        content = json.dumps(
            {"short_summary": "Résumé", "long_summary": "Résumé complet.", "tags": ["test"]}
        )
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    monkeypatch.setattr(summaries, "urlopen", respond)
    return requests


@pytest.mark.parametrize(
    "text", ["🦊漢字é" * 30_000, '\\"\n\t' * 30_000], ids=["unicode", "json-escapes"]
)
def test_complete_summary_request_is_bounded_with_huge_metadata(monkeypatch, model_client, text):
    item = Item(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        title=text,
        source=text,
        author=text,
        notes=text,
        transcript="Français 日本語 transcript " * 1000,
    )
    monkeypatch.setattr(
        summaries,
        "_best_effort_summary_source",
        lambda **kw: SummarySource(meta_description=text, extracted_text="本文 " * 5000),
    )
    summaries.generate_item_summaries(item)
    assert len(model_client) == 1
    assert len(model_client[0]) <= REQUEST_CAP
    prompt = json.loads(model_client[0])["messages"][1]["content"]
    assert item.original_url in prompt
    assert "Kind: article" in prompt
    assert "Français 日本語 transcript" in prompt
    assert "本文" in prompt
    assert "…" in prompt


@pytest.mark.parametrize("field", ["original_url", "kind", "model"])
def test_required_summary_request_overflow_refuses_before_model_call(model_client, settings, field):
    item = Item(original_url="https://example.test/article", kind=ItemKind.ARTICLE)
    if field == "model":
        settings.ARCHIVE_SUMMARY_MODEL = "x" * REQUEST_CAP
    else:
        setattr(item, field, "漢" * REQUEST_CAP)
    with pytest.raises(SummaryGenerationError, match="request.*limit"):
        summaries.generate_item_summaries(item)
    assert model_client == []


def test_summary_request_preserves_ordinary_multilingual_context(monkeypatch, model_client):
    item = Item(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        title="Über Bücher 📚",
        source="Bibliothèque",
        author="山田",
        notes="Une note.\nNoch eine Zeile.",
        transcript="日本語の本文。 Français et Deutsch.",
    )
    source = SummarySource(meta_description="Résumé de la page", extracted_text="Grüße 世界")
    monkeypatch.setattr(summaries, "_best_effort_summary_source", lambda **kw: source)
    summaries.generate_item_summaries(item)
    prompt = json.loads(model_client[0])["messages"][1]["content"]
    for text in (
        item.title,
        item.source,
        item.author,
        item.notes,
        item.transcript,
        source.meta_description,
        source.extracted_text,
    ):
        assert text in prompt
    assert len(model_client[0]) <= REQUEST_CAP


def test_direct_summary_request_also_checks_final_serialized_body(model_client):
    with pytest.raises(SummaryGenerationError, match="request.*limit"):
        summaries._request_summary("🦊" * REQUEST_CAP, timeout=1)
    assert model_client == []
