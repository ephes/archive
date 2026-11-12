import io
import json

import pytest

from archive import article_audio, services, summaries
from archive.models import EnrichmentStatus, Item, ItemKind


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("short_summary", "é" * 240),
        ("long_summary", "文" * 8001),
        ("tags", ["é" * 65]),
        ("tags", [f"tag{i}" for i in range(9)]),
    ],
    ids=["short", "long", "tag", "tag-count"],
)
def test_excess_generated_fields_never_publish(field, value, monkeypatch, settings):
    payload = {"short_summary": "Short", "long_summary": "Long", "tags": ["normal"]}
    payload[field] = value
    settings.ARCHIVE_SUMMARY_API_KEY = "synthetic"
    item = Item.objects.create(
        original_url="https://example.test/item", long_summary="Operator text"
    )
    monkeypatch.setattr(
        summaries, "extract_summary_source_from_url", lambda *a, **kw: summaries.SummarySource()
    )
    body = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
    monkeypatch.setattr(summaries, "urlopen", lambda *a, **kw: io.BytesIO(body))
    assert not services.enrich_item_summaries(item)
    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.FAILED
    assert item.summary_retry_count == 1 and item.summary_retry_at is not None
    assert item.short_summary == item.tags == ""
    assert item.long_summary == "Operator text"
    assert "limit" in item.summary_error


@pytest.mark.django_db
@pytest.mark.parametrize("boundary", [False, True])
def test_generated_fields_at_limits_and_ordinary_text_publish(boundary, monkeypatch, settings):
    payload = {
        "short_summary": "é" * 239 if boundary else "Résumé 日本語",
        "long_summary": "文" * 8000 if boundary else "Längerer Text",
        "tags": [str(i) + "é" * 63 for i in range(8)] if boundary else ["über", "日本語"],
    }
    settings.ARCHIVE_SUMMARY_API_KEY = "synthetic"
    item = Item.objects.create(original_url="https://example.test/item")
    monkeypatch.setattr(
        summaries, "extract_summary_source_from_url", lambda *a, **kw: summaries.SummarySource()
    )
    body = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
    monkeypatch.setattr(summaries, "urlopen", lambda *a, **kw: io.BytesIO(body))
    assert services.enrich_item_summaries(item)
    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == payload["short_summary"]
    assert item.long_summary == payload["long_summary"]
    assert item.tags.splitlines() == payload["tags"]


@pytest.mark.parametrize(
    "html,expected",
    [
        (
            "<blockquote>Leading <p>Inner <em>emphasis</em> sentence.</p> Trailing.</blockquote>",
            "Leading Inner emphasis sentence. Trailing.",
        ),
        (
            "<blockquote>Outer <blockquote>Inner <p>Nested.</p> Tail.</blockquote> "
            "End.</blockquote>",
            "Outer Inner Nested. Tail. End.",
        ),
        (
            "<h1>Heading</h1><p>Text <strong>with <span>inline</span></strong> "
            "words.</p><h6>End</h6>",
            "Heading Text with inline words. End",
        ),
        (
            "<blockquote>Before <nav>Hidden<p>Hidden too</p></nav><p>Visible.</p> "
            "After.</blockquote>",
            "Before Visible. After.",
        ),
        ("<p>First<br>second<script>Hidden</script> third</p>", "First second third"),
    ],
    ids=["paragraph", "blockquote", "heading", "nav", "break"],
)
def test_nested_html_preserves_visible_text_once_in_order(html, expected):
    source = summaries.extract_summary_source_from_html(html)
    assert " ".join(source.extracted_text.split()) == expected


def test_nested_html_reaches_article_audio_source(monkeypatch):
    html = "<blockquote>Leading <p>Inner sentence.</p> Trailing.</blockquote>"
    source = summaries.extract_summary_source_from_html(html)
    monkeypatch.setattr(article_audio, "extract_summary_source_from_url", lambda *a, **kw: source)
    item = Item(original_url="https://example.test/item", kind=ItemKind.ARTICLE)
    text = article_audio._best_effort_article_audio_source_text(item, timeout=1)
    script = article_audio.build_article_audio_script(item, source_text=text)
    assert " ".join(script.split()) == "Leading Inner sentence. Trailing."


@pytest.mark.parametrize(
    "html,expected",
    [
        (
            "<ul><li>First omitted-end list item text<li>Second omitted-end list item</ul>"
            "<div>Cookie banner</div><span>Share</span>",
            ["First omitted-end list item text", "Second omitted-end list item"],
        ),
        (
            "<p>Unclosed paragraph one<p>Unclosed paragraph two</p><button>Accept</button>",
            ["Unclosed paragraph one", "Unclosed paragraph two"],
        ),
        (
            "<div><p>Inside a div without its end tag</div><aside>Promo</aside>",
            ["Inside a div without its end tag"],
        ),
        (
            "<blockquote><ul><li>Outer item<ul><li>Inner item</ul> outer tail</ul></blockquote>",
            ["Outer item", "Inner item", "outer tail"],
        ),
    ],
    ids=["li", "p", "div", "nested-list"],
)
def test_omitted_end_tags_close_containers(html, expected):
    source = summaries.extract_summary_source_from_html(html)
    assert source.extracted_text.split("\n\n") == expected


def test_block_boundaries_separate_and_inline_markup_joins():
    html = (
        "<blockquote><div>Alpha</div><div>Beta</div><a>Gam</a><b>ma</b>"
        "<td>Delta</td></blockquote>"
    )
    source = summaries.extract_summary_source_from_html(html)
    assert source.extracted_text == "Alpha Beta Gamma Delta"


def test_short_top_level_list_items_are_dropped_as_noise():
    html = (
        "<ul><li>Share</li><li>Advertisement</li>"
        "<li>A full list item sentence that carries article content.</li></ul>"
        "<h2>Short</h2><p>Short paragraph.</p>"
    )
    source = summaries.extract_summary_source_from_html(html)
    assert source.extracted_text.split("\n\n") == [
        "A full list item sentence that carries article content.",
        "Short",
        "Short paragraph.",
    ]
