import io
import json
from email.message import Message

import pytest
from django.utils import timezone

from archive import article_audio, services, summaries
from archive.models import EnrichmentStatus, Item, ItemKind


@pytest.fixture
def source_response(monkeypatch):
    responses = []

    def install(content_type, payload):
        response = io.BytesIO(payload)
        response.headers = Message()
        response.headers["Content-Type"] = content_type
        responses.append(response)
        monkeypatch.setattr(summaries, "open_public_url", lambda *a, **kw: response)
        return response

    yield install
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("content_type", ["text/html", "text/plain"])
@pytest.mark.parametrize("charset", ["unknown-private-sentinel", "base64_codec"])
def test_unsupported_source_charset_is_private_domain_error(source_response, content_type, charset):
    source_response(f"{content_type}; charset={charset}", b"private source sentinel")
    with pytest.raises(summaries.SummaryGenerationError, match="charset") as error:
        summaries.extract_summary_source_from_url("https://example.test/source")
    assert charset not in str(error.value)
    assert "private source sentinel" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("charset", ["utf-8", "iso-8859-1"])
def test_supported_text_charsets_keep_unicode(source_response, charset):
    source_response(f"text/html; charset={charset}", "<p>Résumé</p>".encode(charset))
    assert (
        summaries.extract_summary_source_from_url("https://example.test/source").extracted_text
        == "Résumé"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ["summary", "article_audio"])
@pytest.mark.parametrize("charset", ["unknown-private-sentinel", "base64_codec"])
def test_unsupported_charset_keeps_stage_fallback_and_attempt(
    stage, charset, source_response, monkeypatch, settings, caplog
):
    source_response(f"text/html; charset={charset}", b"private source sentinel")
    settings.ARCHIVE_SUMMARY_API_KEY = "synthetic"
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    started_at = timezone.now()
    item = Item.objects.create(
        original_url="https://example.test/source",
        kind=ItemKind.ARTICLE,
        title="Stored title",
        notes="Stored notes",
        long_summary="Stored article text",
        summary_status=(
            EnrichmentStatus.PROCESSING if stage == "summary" else EnrichmentStatus.COMPLETE
        ),
        article_audio_status=EnrichmentStatus.PROCESSING,
        processing_started_at=started_at,
    )
    requests = []

    def summary_api(request, **kwargs):
        assert Item.objects.get(pk=item.pk).processing_started_at == started_at
        requests.append(json.loads(request.data))
        content = json.dumps({"short_summary": "Short", "long_summary": "Long", "tags": ["tag"]})
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    def audio_api(request, **kwargs):
        assert Item.objects.get(pk=item.pk).processing_started_at == started_at
        requests.append(json.loads(request.data))
        return io.BytesIO(b'{"id":"job-123","state":"queued"}')

    monkeypatch.setattr(summaries, "urlopen", summary_api)
    monkeypatch.setattr(article_audio, "urlopen", audio_api)
    if stage == "summary":
        assert services.enrich_item_summaries(item)
        assert "Stored notes" in requests[0]["messages"][1]["content"]
        assert item.summary_status == EnrichmentStatus.COMPLETE
        assert item.short_summary == "Short"
    else:
        assert services.enrich_item_article_audio(item)
        assert requests[0]["input"]["text"] == "Stored title\n\nStored article text"
        assert item.article_audio_job_id == "job-123"
    item.refresh_from_db()
    # Queuing synthesis normally releases the attempt; summary completion keeps
    # ownership while the other stage remains processing.
    assert item.processing_started_at == (started_at if stage == "summary" else None)
    assert item.long_summary == "Stored article text"
    assert not item.summary_error and not item.article_audio_error
    assert charset not in caplog.text
    assert "private source sentinel" not in caplog.text


class _CountedStack(list):
    """Count visited stack entries, including hidden linear list operations."""

    def __init__(self):
        super().__init__()
        self.work = 0

    def __getitem__(self, key):
        self.work += len(self) if isinstance(key, slice) else 1
        return super().__getitem__(key)

    def __contains__(self, value):
        self.work += len(self)
        return super().__contains__(value)

    def __iter__(self):
        self.work += len(self)
        return super().__iter__()

    def pop(self, index=-1):
        self.work += 1
        return super().pop(index)

    def __delitem__(self, key):
        self.work += len(self[key]) if isinstance(key, slice) else 1
        return super().__delitem__(key)

    def clear(self):
        self.work += len(self)
        super().clear()


@pytest.mark.parametrize("depth", [1000, 2000, 4000])
@pytest.mark.parametrize("closing", ["</blockquote>", "</h1></blockquote>"])
def test_nested_container_lookup_has_linear_work(depth, closing):
    parser = summaries._SummaryHTMLParser()
    stack = _CountedStack()
    parser._text_containers = stack
    html = "<blockquote>Before " * depth + "Middle" + (" After" + closing) * depth
    assert len(html.encode()) < summaries.MAX_SUMMARY_SOURCE_BYTES
    parser.feed(html)
    parser.close()
    assert parser.text_chunks == (
        ["Before"] * (depth - 1) + ["Before Middle After"] + ["After"] * (depth - 1)
    )
    assert not stack
    assert stack.work <= 20 * depth


def test_excess_container_depth_refuses_incomplete_source():
    html = '<meta name="description" content="Partial"><blockquote>' * 4097
    with pytest.raises(summaries.SummaryGenerationError, match="depth"):
        summaries.extract_summary_source_from_html(html)


def test_depth_limit_retains_boundary_text_and_omitted_close_recovery():
    html = "<blockquote>" * 4095 + "<p>First<p>Second</div>Tail" + "</blockquote>" * 4095
    assert (
        summaries.extract_summary_source_from_html(html).extracted_text == "First\n\nSecond\n\nTail"
    )


def test_depth_refusal_uses_empty_source_fallback(monkeypatch, caplog):
    item = Item(pk=123, original_url="https://example.test/source")
    monkeypatch.setattr(
        summaries,
        "extract_summary_source_from_url",
        lambda *a, **kw: summaries.extract_summary_source_from_html("<blockquote>Partial" * 4097),
    )
    assert summaries._best_effort_summary_source(item, timeout=1) == summaries.SummarySource()
    assert "Partial" not in caplog.text
