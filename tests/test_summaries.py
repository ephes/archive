from __future__ import annotations

import json

import pytest

from archive.models import Item
from archive.summaries import (
    SummaryGenerationError,
    _build_summary_prompt,
    extract_summary_source_from_html,
    generate_item_summaries,
)


def test_extract_summary_source_from_html_collects_meta_description_and_body_text() -> None:
    source = extract_summary_source_from_html(
        """
        <html>
          <head>
            <meta name="description" content="Meta description">
          </head>
          <body>
            <nav>Skip me</nav>
            <article>
              <h1>Example title</h1>
              <p>First paragraph with enough words to make it into the extracted text.</p>
              <p>Second paragraph with more context for the summarizer to use later.</p>
            </article>
          </body>
        </html>
        """
    )

    assert source.meta_description == "Meta description"
    assert "First paragraph with enough words" in source.extracted_text
    assert "Second paragraph with more context" in source.extracted_text
    assert "Skip me" not in source.extracted_text


@pytest.mark.django_db
def test_generate_item_summaries_parses_json_payload(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        title="Example article",
        source="Example",
    )
    settings.ARCHIVE_SUMMARY_API_KEY = "key"
    settings.ARCHIVE_SUMMARY_MODEL = "gpt-4o-mini"
    settings.ARCHIVE_SUMMARY_API_BASE = "https://api.openai.com/v1"

    monkeypatch.setattr(
        "archive.summaries.extract_summary_source_from_url",
        lambda url, timeout: type(
            "Source",
            (),
            {"meta_description": "Meta", "extracted_text": "Body text with useful context."},
        )(),
    )
    monkeypatch.setattr(
        "archive.summaries._request_summary",
        lambda prompt, timeout: json.dumps(
            {
                "short_summary": "Short summary",
                "long_summary": "Long summary",
                "tags": ["alpha", "beta", "alpha"],
            }
        ),
    )

    generated = generate_item_summaries(item=item)

    assert generated.short_summary == "Short summary"
    assert generated.long_summary == "Long summary"
    assert generated.tags == ("alpha", "beta")


def test_generate_item_summaries_requires_configured_api_key(settings) -> None:
    settings.ARCHIVE_SUMMARY_API_KEY = ""
    item = Item(
        original_url="https://example.com/article",
        title="Example article",
    )

    with pytest.raises(SummaryGenerationError, match="not configured"):
        generate_item_summaries(item=item)


@pytest.mark.django_db
def test_build_summary_prompt_includes_transcript_context() -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        title="Example episode",
        source="Example Radio",
        transcript="First transcript paragraph.\n\nSecond transcript paragraph.",
    )

    prompt = _build_summary_prompt(
        item=item,
        source=type(
            "Source",
            (),
            {
                "meta_description": "Meta description",
                "extracted_text": "Body text with useful context.",
            },
        )(),
    )

    assert "Transcript:" in prompt
    assert "First transcript paragraph." in prompt
    assert "Extracted source text:" in prompt
    assert "same language as the transcript when present" in prompt
    assert "Do not translate non-English content into English." in prompt
