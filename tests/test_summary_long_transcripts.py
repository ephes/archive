"""Summary refresh for long transcripts with oversized pages (synthetic data only)."""

import io
import json
from email.message import Message
from types import SimpleNamespace

import pytest

from archive import services, summaries
from archive.models import EnrichmentStatus, Item, ItemKind

REQUEST_CAP = 64 * 1024
# The length of the long transcript that exposed the failure; the text is synthetic.
LONG_TRANSCRIPT = ("Wort " * 25_000)[:102_314]


def _page_response(payload: bytes, content_type: str):
    headers = Message()
    headers["Content-Type"] = content_type
    reads = []

    def read(size=-1):
        reads.append(size)
        return payload if size is None or size < 0 else payload[:size]

    response = SimpleNamespace(headers=headers, read=read, reads=reads)

    class Context:
        def __enter__(self):
            return response

        def __exit__(self, *exc):
            return False

    return Context(), response


def _oversized_video_page() -> bytes:
    head = (
        "<html><head><meta name='description' content='Synthetische Beschreibung'></head>"
        "<body><p>Ein synthetischer Absatz mit genug Text für die Quelle.</p>"
    ).encode()
    # Script payloads push a watch page past 1 MiB, as large video pages do.
    filler = b"<script>" + b"x" * (summaries.MAX_SUMMARY_SOURCE_BYTES + 10) + b"</script>"
    return head + filler + b"</body></html>"


def _completion(payload: dict) -> io.BytesIO:
    content = json.dumps(payload)
    return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


VALID = {
    "short_summary": "Kurze synthetische Zusammenfassung.",
    "long_summary": "Eine längere synthetische Zusammenfassung. Sie hat zwei Sätze.",
    "tags": ["synthetisch", "test", "video"],
}


@pytest.fixture
def model(monkeypatch, settings):
    settings.ARCHIVE_SUMMARY_API_KEY = "synthetic"
    calls = SimpleNamespace(requests=[], responses=[])

    def respond(request, **kwargs):
        calls.requests.append(request.data)
        return _completion(calls.responses.pop(0))

    monkeypatch.setattr(summaries, "urlopen", respond)

    def unreachable(*args, **kwargs):
        raise OSError("synthetic: no page")

    monkeypatch.setattr(summaries, "open_public_url", unreachable)
    return calls


def _messages(body: bytes) -> list[dict]:
    return json.loads(body)["messages"]


def test_oversized_html_summary_source_uses_its_first_mebibyte(monkeypatch):
    context, response = _page_response(_oversized_video_page(), "text/html; charset=utf-8")
    monkeypatch.setattr(summaries, "open_public_url", lambda *a, **kw: context)

    source = summaries.extract_summary_source_from_url(
        "https://video.example.test/watch", truncate_oversized=True
    )

    assert source.meta_description == "Synthetische Beschreibung"
    assert "synthetischer Absatz" in source.extracted_text
    assert response.reads == [summaries.MAX_SUMMARY_SOURCE_BYTES + 1]


def test_oversized_source_stays_refused_without_truncation(monkeypatch):
    # Article audio reads whole articles aloud: a prefix would silently cut them.
    context, _ = _page_response(_oversized_video_page(), "text/html")
    monkeypatch.setattr(summaries, "open_public_url", lambda *a, **kw: context)

    with pytest.raises(summaries.SummaryGenerationError, match="1 MiB"):
        summaries.extract_summary_source_from_url("https://video.example.test/watch")


@pytest.mark.parametrize("content_type", ["audio/mpeg", "video/mp4", "application/octet-stream"])
def test_non_text_summary_source_is_not_downloaded(monkeypatch, content_type):
    context, response = _page_response(b"\0" * 2048, content_type)
    monkeypatch.setattr(summaries, "open_public_url", lambda *a, **kw: context)

    source = summaries.extract_summary_source_from_url("https://media.example.test/episode")

    assert source == summaries.SummarySource()
    assert response.reads == []


@pytest.mark.django_db
def test_long_transcript_refresh_survives_oversized_page_and_long_reply(monkeypatch, model):
    """Item 111 shape: video, oversized watch page, 102 314-char generated transcript."""
    context, _ = _page_response(_oversized_video_page(), "text/html")
    monkeypatch.setattr(summaries, "open_public_url", lambda *a, **kw: context)
    item = Item.objects.create(
        original_url="https://video.example.test/watch",
        kind=ItemKind.VIDEO,
        transcript=LONG_TRANSCRIPT,
        transcript_generated=True,
        short_summary="Alte Kurzfassung",
        short_summary_generated=True,
        long_summary="Alte lange Fassung",
        long_summary_generated=True,
        tags="alt",
        tags_generated=True,
        summary_status=EnrichmentStatus.PENDING,
    )
    too_long = dict(VALID, short_summary="Ein sehr langer erster Satz " * 12)
    model.responses = [too_long, VALID]

    assert services.enrich_item_summaries(item)

    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.summary_error == ""
    assert item.short_summary == VALID["short_summary"]
    assert item.tags.splitlines() == VALID["tags"]
    assert len(model.requests) == 2
    assert all(len(body) <= REQUEST_CAP for body in model.requests)
    first_prompt = _messages(model.requests[0])[1]["content"]
    assert "Synthetische Beschreibung" in first_prompt
    assert "Wort Wort" in first_prompt


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value,instruction",
    [
        (
            "short_summary",
            "é" * 240,
            "short_summary has 240 characters. Rewrite it to at most 160 characters",
        ),
        ("long_summary", "文" * 8001, "long_summary has 8001 characters. Rewrite it to at most"),
        ("tags", [f"tag{i}" for i in range(9)], "tags has 9 entries. Keep only the 8 most"),
        ("tags", ["é" * 65], "A tag has 65 characters. Shorten every tag to at most 64"),
    ],
    ids=["short", "long", "tag-count", "tag-length"],
)
def test_over_limit_reply_gets_one_bounded_shortening_request(model, field, value, instruction):
    item = Item(original_url="https://example.test/item", transcript=LONG_TRANSCRIPT)
    model.responses = [dict(VALID, **{field: value}), VALID]

    generated = summaries.generate_item_summaries(item)

    assert generated.short_summary == VALID["short_summary"]
    assert len(model.requests) == 2
    repair = model.requests[1]
    assert len(repair) <= REQUEST_CAP
    instructions = _messages(repair)[1]["content"]
    assert instruction in instructions
    assert f"change only {field}." in instructions
    # The repair asks to shorten the reply only; the transcript is not sent again.
    assert "Wort Wort" not in instructions
    for limit in ("239", "8000", "3 to 8", "64"):
        assert limit in instructions


@pytest.mark.django_db
def test_still_over_limit_after_repair_fails_naming_the_fields(model):
    item = Item.objects.create(
        original_url="https://example.test/item", long_summary="Operator text"
    )
    over = dict(VALID, short_summary="é" * 240, tags=[f"tag{i}" for i in range(9)])
    model.responses = [over, over]

    assert not services.enrich_item_summaries(item)

    item.refresh_from_db()
    assert len(model.requests) == 2
    assert item.summary_status == EnrichmentStatus.FAILED
    assert item.short_summary == ""
    assert item.long_summary == "Operator text"
    assert item.summary_error == (
        "Summary response exceeded generated field limits "
        "(short_summary: 240 > 239 characters, tags: 9 > 8 tags)"
    )


@pytest.mark.django_db
def test_oversized_reply_is_not_sent_back_for_repair(model):
    item = Item(original_url="https://example.test/item")
    model.responses = [dict(VALID, long_summary="文" * 30_000)]

    expected = r"limits \(long_summary: 30000 > 8000 characters\)$"
    with pytest.raises(summaries.SummaryGenerationError, match=expected):
        summaries.generate_item_summaries(item)

    assert len(model.requests) == 1


@pytest.mark.django_db
def test_prompt_states_the_validated_limits(model):
    model.responses = [VALID]
    summaries.generate_item_summaries(Item(original_url="https://example.test/item"))
    prompt = _messages(model.requests[0])[1]["content"]
    assert "at most 239 characters" in prompt
    assert "at most 8000 characters" in prompt
    assert "3 to 8" in prompt and "at most 64 characters" in prompt


@pytest.mark.django_db
def test_shortening_request_shows_the_counted_tags_including_duplicates(model):
    tags = ["dup", "Dup", *[f"tag{i}" for i in range(7)]]
    model.responses = [dict(VALID, tags=tags), VALID]

    summaries.generate_item_summaries(Item(original_url="https://example.test/item"))

    instructions = _messages(model.requests[1])[1]["content"]
    assert "tags has 9 entries" in instructions
    shown = json.loads(instructions.rsplit("\n", 1)[1])
    assert len(shown["tags"]) == 9
