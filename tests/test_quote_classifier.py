from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from archive.classification import CURRENT_CLASSIFICATION_ENGINE_VERSION
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.quote_classifier import (
    CURRENT_QUOTE_CLASSIFIER_VERSION,
    QuoteClassificationError,
    classify_quote_item,
    quote_classifier_processed,
)
from archive.summaries import SummarySource


@pytest.mark.django_db
def test_classify_quote_item_marks_high_confidence_yes_and_normalizes_statuses(settings) -> None:
    settings.ARCHIVE_SUMMARY_MODEL = "gpt-test"
    item = Item.objects.create(
        original_url="https://example.com/quote",
        title="A useful line",
        source="Example",
        author="Ada",
        kind=ItemKind.LINK,
        notes="save as weeknote opener",
        short_summary="A concise quoted line.",
        tags="quotes\nweeknotes",
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_error="old media failure",
        article_audio_status=EnrichmentStatus.FAILED,
        article_audio_error="old tts failure",
        classification_evidence={"source_adapter": ""},
        classification_rule="default_link",
        classification_engine_version=1,
    )
    prompts: list[str] = []

    def fake_classifier(prompt: str, timeout: int) -> str:
        prompts.append(prompt)
        assert timeout == 12
        return json.dumps(
            {
                "is_quote": True,
                "confidence": 0.93,
                "rationale": "The item is a standalone quotation suitable as an opener.",
                "reusable_text": "A useful line.",
            }
        )

    result = classify_quote_item(
        item=item,
        timeout=12,
        source_fetcher=lambda url, timeout: SummarySource(
            meta_description="Quote page",
            extracted_text="A useful line with surrounding citation text.",
        ),
        classifier=fake_classifier,
    )

    item.refresh_from_db()
    assert result.applied_kind_update is True
    assert item.kind == ItemKind.QUOTE
    assert item.classification_rule == "quote_classifier"
    assert item.classification_engine_version == CURRENT_CLASSIFICATION_ENGINE_VERSION
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.media_archive_error == ""
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_error == ""
    assert item.classification_evidence["source_adapter"] == ""
    evidence = item.classification_evidence["quote_classifier"]
    assert evidence["version"] == CURRENT_QUOTE_CLASSIFIER_VERSION
    assert evidence["decision"] == "yes"
    assert evidence["applied"] is True
    assert evidence["confidence"] == 0.93
    assert evidence["model"] == "gpt-test"
    assert quote_classifier_processed(item) is True
    assert "Title: A useful line" in prompts[0]
    assert "Source: Example" in prompts[0]
    assert "Author: Ada" in prompts[0]
    assert "Kind: link" in prompts[0]
    assert "Notes: save as weeknote opener" in prompts[0]
    assert "Summary: A concise quoted line." in prompts[0]
    assert "Tags: quotes, weeknotes" in prompts[0]
    assert "A useful line with surrounding citation text." in prompts[0]


@pytest.mark.django_db
def test_classify_quote_item_stores_no_evidence_without_changing_kind() -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        title="An article",
        kind=ItemKind.ARTICLE,
        classification_rule="metadata_kind_hint",
        classification_engine_version=1,
    )

    result = classify_quote_item(
        item=item,
        source_fetcher=lambda url, timeout: SummarySource(extracted_text="Long article body."),
        classifier=lambda prompt, timeout: json.dumps(
            {
                "is_quote": False,
                "confidence": 0.88,
                "rationale": "This is an article, not a standalone reusable quotation.",
                "reusable_text": "",
            }
        ),
    )

    item.refresh_from_db()
    assert result.applied_kind_update is False
    assert item.kind == ItemKind.ARTICLE
    assert item.classification_rule == "metadata_kind_hint"
    assert item.classification_engine_version == 1
    assert item.classification_evidence["quote_classifier"]["version"] == (
        CURRENT_QUOTE_CLASSIFIER_VERSION
    )
    assert item.classification_evidence["quote_classifier"]["decision"] == "no"
    assert item.classification_evidence["quote_classifier"]["applied"] is False
    assert quote_classifier_processed(item) is True


@pytest.mark.django_db
def test_classify_quote_item_dry_run_does_not_update_item() -> None:
    item = Item.objects.create(
        original_url="https://example.com/quote",
        title="Dry run quote",
        kind=ItemKind.LINK,
    )

    result = classify_quote_item(
        item=item,
        dry_run=True,
        source_fetcher=lambda url, timeout: SummarySource(extracted_text="Quote text."),
        classifier=lambda prompt, timeout: json.dumps(
            {
                "is_quote": True,
                "confidence": 0.99,
                "rationale": "Clearly a standalone quote.",
                "reusable_text": "Quote text.",
            }
        ),
    )

    item.refresh_from_db()
    assert result.decision.high_confidence_yes is True
    assert result.update_fields == ()
    assert item.kind == ItemKind.LINK
    assert item.classification_evidence == {}


@pytest.mark.django_db
def test_classify_quote_items_command_skips_processed_quotes_and_operator_overrides(
    monkeypatch, settings
) -> None:
    settings.ARCHIVE_SUMMARY_API_KEY = "key"
    settings.ARCHIVE_SUMMARY_MODEL = "gpt-test"
    candidate = Item.objects.create(
        original_url="https://example.com/candidate",
        title="Candidate quote",
        kind=ItemKind.LINK,
    )
    generic_link = Item.objects.create(
        original_url="https://example.com/product",
        title="Useful product homepage",
        kind=ItemKind.LINK,
    )
    processed = Item.objects.create(
        original_url="https://example.com/processed",
        kind=ItemKind.LINK,
        classification_evidence={
            "quote_classifier": {"version": CURRENT_QUOTE_CLASSIFIER_VERSION, "decision": "no"}
        },
    )
    quote = Item.objects.create(
        original_url="https://example.com/quote",
        kind=ItemKind.QUOTE,
    )
    override = Item.objects.create(
        original_url="https://example.com/override",
        kind=ItemKind.ARTICLE,
        classification_rule="operator_override",
    )
    calls: list[str] = []

    monkeypatch.setattr(
        "archive.quote_classifier.extract_summary_source_from_url",
        lambda url, timeout: SummarySource(extracted_text="Standalone quote text."),
    )

    def fake_request(prompt: str, timeout: int) -> str:
        calls.append(prompt)
        return json.dumps(
            {
                "is_quote": True,
                "confidence": 0.91,
                "rationale": "The saved item is a concise standalone quote.",
                "reusable_text": "Standalone quote text.",
            }
        )

    monkeypatch.setattr("archive.quote_classifier._request_quote_classification", fake_request)

    stdout = StringIO()
    call_command("classify_quote_items", "--limit", "10", stdout=stdout)

    output = stdout.getvalue()
    candidate.refresh_from_db()
    processed.refresh_from_db()
    generic_link.refresh_from_db()
    quote.refresh_from_db()
    override.refresh_from_db()

    assert len(calls) == 1
    assert f"MARKED QUOTE item {candidate.pk}" in output
    assert f"item {generic_link.pk}" not in output
    assert f"item {processed.pk}" not in output
    assert f"item {quote.pk}" not in output
    assert f"item {override.pk}" not in output
    assert "Summary: inspected=1 yes=1 no=0 changed=1 failed=0 mode=apply" in output
    assert candidate.kind == ItemKind.QUOTE
    assert candidate.classification_rule == "quote_classifier"
    assert generic_link.kind == ItemKind.LINK
    assert generic_link.classification_evidence == {}
    assert processed.kind == ItemKind.LINK
    assert quote.kind == ItemKind.QUOTE
    assert override.classification_rule == "operator_override"


@pytest.mark.django_db
def test_classify_quote_items_command_dry_run_since_days(monkeypatch, settings) -> None:
    settings.ARCHIVE_SUMMARY_API_KEY = "key"
    recent = Item.objects.create(
        original_url="https://example.com/recent",
        title="Recent quote",
        kind=ItemKind.LINK,
        shared_at=timezone.now(),
    )
    old = Item.objects.create(
        original_url="https://example.com/old",
        title="Old quote",
        kind=ItemKind.LINK,
        shared_at=timezone.now() - timedelta(days=30),
    )
    calls = 0

    monkeypatch.setattr(
        "archive.quote_classifier.extract_summary_source_from_url",
        lambda url, timeout: SummarySource(extracted_text="Quote text."),
    )

    def fake_request(prompt: str, timeout: int) -> str:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "is_quote": True,
                "confidence": 0.95,
                "rationale": "Clearly reusable.",
                "reusable_text": "Quote text.",
            }
        )

    monkeypatch.setattr("archive.quote_classifier._request_quote_classification", fake_request)

    stdout = StringIO()
    call_command(
        "classify_quote_items",
        "--since-days",
        "14",
        "--dry-run",
        stdout=stdout,
    )

    output = stdout.getvalue()
    recent.refresh_from_db()
    old.refresh_from_db()

    assert calls == 1
    assert f"WOULD MARK QUOTE item {recent.pk}" in output
    assert f"item {old.pk}" not in output
    assert "mode=dry-run" in output
    assert recent.kind == ItemKind.LINK
    assert recent.classification_evidence == {}
    assert old.kind == ItemKind.LINK


@pytest.mark.parametrize(
    "raw_response, message",
    [
        ("[]", "JSON object"),
        (json.dumps({"is_quote": True, "confidence": 5, "rationale": "bad"}), "between 0 and 1"),
        (
            json.dumps({"is_quote": True, "confidence": True, "rationale": "bad"}),
            "confidence must be a number",
        ),
        (
            json.dumps({"is_quote": 1, "confidence": 0.9, "rationale": "bad"}),
            "is_quote must be a boolean",
        ),
        (
            json.dumps({"is_quote": True, "confidence": 0.9, "rationale": []}),
            "rationale must be a string",
        ),
    ],
)
def test_classify_quote_item_rejects_malformed_classifier_schema(raw_response, message) -> None:
    item = Item(original_url="https://example.com/quote", title="Quote")

    with pytest.raises(QuoteClassificationError, match=message):
        classify_quote_item(
            item=item,
            source_fetcher=lambda url, timeout: SummarySource(),
            classifier=lambda prompt, timeout: raw_response,
        )


def test_classify_quote_item_requires_api_key_when_using_real_classifier(settings) -> None:
    settings.ARCHIVE_SUMMARY_API_KEY = ""
    item = Item(original_url="https://example.com/quote", title="Quote")

    with pytest.raises(QuoteClassificationError, match="not configured"):
        classify_quote_item(item=item)
