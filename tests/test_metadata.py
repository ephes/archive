from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from archive.article_audio import ArticleAudioJobUpdate
from archive.classification import CURRENT_CLASSIFICATION_ENGINE_VERSION, classify_item
from archive.media_archival import ArchivedAudio
from archive.metadata import (
    MetadataExtractionError,
    extract_metadata_from_html,
    extract_metadata_from_url,
)
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import (
    MEDIA_ARCHIVE_RETRY_DELAYS,
    SUMMARY_RETRY_DELAYS,
    claim_pending_item,
    enrich_item_article_audio,
    enrich_item_metadata,
    enrich_item_transcript,
    enrich_pending_items,
    prepare_item_for_enrichment,
    recover_processing_items,
    request_item_reprocess,
)
from archive.summaries import GeneratedSummary


class _MigrationApps:
    def get_model(self, app_label: str, model_name: str):
        assert app_label == "archive"
        assert model_name == "Item"
        return Item


def stub_archive_audio(monkeypatch, size_bytes: int = 12345) -> None:
    monkeypatch.setattr(
        "archive.services.archive_item_audio",
        lambda item, timeout: ArchivedAudio(
            object_name=f"items/{item.pk}/audio/source.mp3",
            content_type="audio/mpeg",
            size_bytes=size_bytes,
        ),
    )


def _save_archived_media(path: str, payload: bytes) -> None:
    if storages["archive_media"].exists(path):
        storages["archive_media"].delete(path)
    storages["archive_media"].save(path, ContentFile(payload))


@pytest.mark.django_db
def test_extract_metadata_from_html_prefers_structured_fields() -> None:
    html = """
    <html>
      <head>
        <title>Fallback title</title>
        <meta property="og:title" content="OG Title">
        <meta property="og:site_name" content="OG Site">
        <meta name="author" content="Meta Author">
        <meta property="article:published_time" content="2026-03-01T12:34:56+00:00">
        <meta property="og:audio" content="/media/audio.mp3">
        <script type="application/ld+json">
          {"@type":"Article","headline":"LD Title","publisher":{"name":"LD Publisher"}}
        </script>
      </head>
      <body></body>
    </html>
    """

    metadata = extract_metadata_from_html(html=html, base_url="https://example.com/articles/demo")

    assert metadata.title == "OG Title"
    assert metadata.source == "OG Site"
    assert metadata.author == "Meta Author"
    assert metadata.media_url == "https://example.com/media/audio.mp3"
    assert metadata.audio_url == "https://example.com/media/audio.mp3"
    assert metadata.kind_hint == ItemKind.ARTICLE
    assert metadata.original_published_at == datetime(
        2026,
        3,
        1,
        12,
        34,
        56,
        tzinfo=UTC,
    )


@pytest.mark.django_db
def test_extract_metadata_from_html_detects_audio_source_elements() -> None:
    metadata = extract_metadata_from_html(
        html="""
        <html>
          <body>
            <audio controls>
              <source src="/media/audio.mp3" type="audio/mpeg">
            </audio>
          </body>
        </html>
        """,
        base_url="https://example.com/articles/demo",
    )

    assert metadata.audio_url == "https://example.com/media/audio.mp3"
    assert metadata.media_candidates == (
        metadata.media_candidates[0],
    )
    assert metadata.media_candidates[0].url == "https://example.com/media/audio.mp3"
    assert metadata.media_candidates[0].candidate_type == "audio"
    assert metadata.media_candidates[0].detection_source == "html_audio"


@pytest.mark.django_db
def test_extract_metadata_from_html_detects_video_source_elements() -> None:
    metadata = extract_metadata_from_html(
        html="""
        <html>
          <body>
            <video controls>
              <source src="https://cdn.example.com/talk.mp4" type="video/mp4">
            </video>
          </body>
        </html>
        """,
        base_url="https://example.com/talks/demo",
    )

    assert metadata.media_url == "https://cdn.example.com/talk.mp4"
    assert metadata.media_candidates[0].url == "https://cdn.example.com/talk.mp4"
    assert metadata.media_candidates[0].candidate_type == "video"
    assert metadata.media_candidates[0].detection_source == "html_video"


# ---------------------------------------------------------------------------
# extract_metadata_from_url – unit tests for the HTTP fetch layer
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    """Minimal fake of the object returned by ``urllib.request.urlopen``."""

    def __init__(
        self, body: bytes, content_type: str = "text/html", charset: str = "utf-8"
    ) -> None:
        self._body = body
        self._pos = 0
        self._content_type = content_type
        self._charset = charset
        self.headers = self

    # Mimic http.client.HTTPMessage interface used by the production code.
    def get_content_type(self) -> str:
        return self._content_type

    def get_content_charset(self) -> str:
        return self._charset

    def read(self, n: int = -1) -> bytes:
        if n == -1:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
        else:
            chunk = self._body[self._pos: self._pos + n]
            self._pos += len(chunk)
        return chunk

    # Context-manager support (``with urlopen(...) as response``).
    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        pass


def test_extract_metadata_from_url_extracts_title_from_normal_page(monkeypatch) -> None:
    html = b"""<html><head>
        <meta property="og:title" content="Normal Page Title">
        <meta property="og:site_name" content="Example">
    </head><body><p>content</p></body></html>"""

    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _FakeHTTPResponse(html),
    )

    metadata = extract_metadata_from_url("https://example.com/page")

    assert metadata.title == "Normal Page Title"
    assert metadata.source == "Example"


def test_extract_metadata_from_url_succeeds_on_page_larger_than_1mib(monkeypatch) -> None:
    """A page whose total body exceeds MAX_METADATA_BYTES must not raise.

    All relevant metadata is in ``<head>``, which is emitted well before the
    1 MiB mark.  The fix stops reading after ``</head>`` so large pages such
    as YouTube are handled correctly.
    """
    head = (
        b"<html><head>"
        b'<meta property="og:title" content="Large Page Title">'
        b'<meta property="og:site_name" content="Big Site">'
        b"</head>"
    )
    # Body that pushes the total well beyond 1 MiB.
    body = b"<body>" + b"x" * (2 * 1024 * 1024) + b"</body></html>"
    html = head + body

    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _FakeHTTPResponse(html),
    )

    metadata = extract_metadata_from_url("https://example.com/large")

    assert metadata.title == "Large Page Title"
    assert metadata.source == "Big Site"


def test_extract_metadata_from_url_handles_head_tag_split_across_chunk_boundary(
    monkeypatch,
) -> None:
    """``</head>`` straddling two read() calls must still be detected by the parser.

    We place ``</head>`` so that its first 4 bytes (``</he``) fall at the very end
    of chunk 1 and the remaining 3 bytes (``ad>``) open chunk 2.  Python's
    ``HTMLParser`` buffers incomplete tags across ``feed()`` calls, so the
    structural close must still be recognised and the title must be extracted.
    """
    from archive.metadata import _METADATA_CHUNK_SIZE

    title_tag = b'<meta property="og:title" content="Straddled Title">'
    head_prefix = b"<html><head>" + title_tag
    sentinel = b"</head>"
    split_in_sentinel = 4  # "</he" ends chunk 1, "ad>" starts chunk 2

    # Pad head_prefix so the sentinel starts at exactly (CHUNK_SIZE - split_in_sentinel).
    target_start = _METADATA_CHUNK_SIZE - split_in_sentinel
    pad_len = target_start - len(head_prefix)
    assert pad_len >= 0, "head_prefix exceeds one chunk; adjust split_in_sentinel"
    html = head_prefix + b" " * pad_len + sentinel + b"<body></body></html>"

    # Verify the split is where we expect it.
    assert html[target_start : target_start + len(sentinel)] == sentinel
    chunk1_tail = html[_METADATA_CHUNK_SIZE - split_in_sentinel : _METADATA_CHUNK_SIZE]
    assert chunk1_tail == sentinel[:split_in_sentinel]
    tail_len = len(sentinel) - split_in_sentinel
    chunk2_head = html[_METADATA_CHUNK_SIZE : _METADATA_CHUNK_SIZE + tail_len]
    assert chunk2_head == sentinel[split_in_sentinel:]

    pos = 0

    class _StreamingResponse(_FakeHTTPResponse):
        def read(self, n: int = -1) -> bytes:
            nonlocal pos
            end = len(html) if n < 0 else pos + n
            chunk = html[pos:end]
            pos += len(chunk)
            return chunk

    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _StreamingResponse(b""),
    )

    metadata = extract_metadata_from_url("https://example.com/straddled")
    assert metadata.title == "Straddled Title"


def test_extract_metadata_from_url_ignores_head_close_tag_inside_script(monkeypatch) -> None:
    """``</head>`` inside ``<script>`` content must not stop reading early.

    Python's ``HTMLParser`` enters CDATA mode for ``<script>`` and does not fire
    tag events for its contents, so a literal ``</head>`` string inside a script
    must not set ``parser.head_closed`` before the structural ``</head>`` is seen.
    """
    html = (
        b"<html><head>"
        b'<script>var x = "</head>";</script>'
        b'<meta property="og:title" content="Real Title">'
        b"</head>"
        b"<body>body</body></html>"
    )

    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _FakeHTTPResponse(html),
    )

    metadata = extract_metadata_from_url("https://example.com/script-trick")
    assert metadata.title == "Real Title"


def test_extract_metadata_from_url_raises_when_cap_reached_without_closing_head(
    monkeypatch,
) -> None:
    """Exhausting MAX_METADATA_BYTES without a structural ``</head>`` must raise.

    This preserves the old hard-failure behaviour for pages that genuinely have
    no parseable head section within the size limit, preventing a silent partial
    extraction from being treated as a successful metadata pass.
    """
    from archive.metadata import MAX_METADATA_BYTES

    # A page with no </head> whose body exceeds the cap.
    html = b"<html><head>" + b"x" * (MAX_METADATA_BYTES + 1)

    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _FakeHTTPResponse(html),
    )

    with pytest.raises(MetadataExtractionError, match="exceeded"):
        extract_metadata_from_url("https://example.com/no-head")


def test_extract_metadata_from_url_raises_on_http_error(monkeypatch) -> None:
    from urllib.error import HTTPError

    def _raise(request, timeout):
        raise HTTPError(  # type: ignore[arg-type]
            url="https://example.com/404",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("archive.metadata.urlopen", _raise)

    with pytest.raises(MetadataExtractionError, match="HTTP 404"):
        extract_metadata_from_url("https://example.com/404")


def test_extract_metadata_from_url_raises_on_unsupported_content_type(monkeypatch) -> None:
    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _FakeHTTPResponse(b"binary", content_type="application/pdf"),
    )

    with pytest.raises(MetadataExtractionError, match="Unsupported content type"):
        extract_metadata_from_url("https://example.com/doc.pdf")


def test_extract_metadata_from_url_captures_body_media_past_head_close(monkeypatch) -> None:
    """Body ``<audio>`` / ``<video>`` tags beyond the ``</head>`` chunk must still
    be captured, as long as they fall within ``MAX_METADATA_BYTES``.

    Stopping the read at ``</head>`` would miss inline players on podcast and radio
    pages whose embed markup lives in the document body.
    """
    from archive.metadata import _METADATA_CHUNK_SIZE

    title_meta = b'<meta property="og:title" content="Body Audio Page">'
    head = b"<html><head>" + title_meta + b"</head>"
    # Push the audio tag past the first chunk boundary.
    pad = b" " * (_METADATA_CHUNK_SIZE - len(head) + 10)
    audio = b'<body><audio><source src="https://cdn.example.com/ep.mp3"></audio></body></html>'
    html = head + pad + audio

    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _FakeHTTPResponse(html),
    )

    metadata = extract_metadata_from_url("https://example.com/body-audio")

    assert metadata.title == "Body Audio Page"
    assert metadata.audio_url == "https://cdn.example.com/ep.mp3"


def test_extract_metadata_from_url_handles_multibyte_char_split_across_chunk_boundary(
    monkeypatch,
) -> None:
    """A UTF-8 character whose bytes straddle a chunk boundary must decode cleanly.

    Without an incremental decoder, the first chunk's trailing incomplete byte is
    replaced with U+FFFD and the second chunk's continuation bytes are replaced
    separately, corrupting the extracted value.
    """
    from archive.metadata import _METADATA_CHUNK_SIZE

    # € is encoded as three bytes in UTF-8: 0xe2 0x82 0xac.
    # Position it so the first byte (0xe2) is the very last byte of chunk 1.
    euro = "€".encode()
    assert len(euro) == 3

    attr_prefix = b'<meta property="og:title" content="Price:\xc2\xa0'
    # \xc2\xa0 is the UTF-8 encoding of non-breaking space, used here to keep
    # the attribute value realistic without additional padding complexity.
    attr_suffix = b'5.99"></head><body></body></html>'
    doc_prefix = b"<html><head>"

    # Place the start of euro at exactly (CHUNK_SIZE - 1).
    target = _METADATA_CHUNK_SIZE - 1
    pad_len = target - len(doc_prefix) - len(attr_prefix)
    assert pad_len >= 0, "attr_prefix too long; adjust target offset"
    html = doc_prefix + b" " * pad_len + attr_prefix + euro + attr_suffix

    assert html[target : target + 1] == euro[:1]
    assert html[target + 1 : target + 3] == euro[1:]

    monkeypatch.setattr(
        "archive.metadata.urlopen",
        lambda request, timeout: _FakeHTTPResponse(html),
    )

    metadata = extract_metadata_from_url("https://example.com/euro-title")

    assert "€" in metadata.title
    assert "5.99" in metadata.title
    assert "\ufffd" not in metadata.title


@pytest.mark.django_db
def test_enrich_item_metadata_classifies_castro_episode_and_archives_html_audio(
    monkeypatch,
) -> None:
    stub_archive_audio(monkeypatch)
    item = Item.objects.create(
        original_url="https://castro.fm/episode/ubOf93",
        kind=ItemKind.LINK,
    )

    def fake_extract(url: str, timeout: int):
        assert url == item.original_url
        return extract_metadata_from_html(
            html="""
            <html>
              <head>
                <meta property="og:title" content="Castro Episode">
                <meta property="og:description" content="Episode description">
              </head>
              <body>
                <audio preload="true">
                  <source
                    src="https://cdn.example.com/castro-episode.mp3"
                    type="audio/mpeg">
                </audio>
              </body>
            </html>
            """,
            base_url=url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Short generated summary",
            long_summary="Long generated summary with enough detail to be useful later.",
            tags=("podcast", "castro", "episode"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript from Castro audio.",
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.kind == ItemKind.PODCAST_EPISODE
    assert item.audio_url == "https://cdn.example.com/castro-episode.mp3"
    assert item.classification_rule == "adapter_castro_episode"
    assert item.classification_evidence["selected_media"]["audio"] == (
        "https://cdn.example.com/castro-episode.mp3"
    )
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"


@pytest.mark.django_db
def test_enrich_item_metadata_updates_missing_fields_without_overwriting_existing_ones(
    monkeypatch,
) -> None:
    stub_archive_audio(monkeypatch)
    item = Item.objects.create(
        original_url="https://example.com/demo",
        title="Manual title",
        kind=ItemKind.LINK,
    )

    def fake_extract(url: str, timeout: int):
        assert url == item.original_url
        assert timeout == 15
        return extract_metadata_from_html(
            html="""
            <html>
              <head>
                <meta property="og:title" content="Fetched title">
                <meta property="og:site_name" content="Example Site">
                <meta name="author" content="Example Author">
                <meta property="article:published_time" content="2026-03-01T12:34:56+00:00">
                <meta property="og:audio" content="https://cdn.example.com/audio.mp3">
              </head>
            </html>
            """,
            base_url=item.original_url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Short generated summary",
            long_summary="Long generated summary with more detail.",
            tags=("radio", "culture", "interview"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript paragraph one.\n\nTranscript paragraph two.",
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.title == "Manual title"
    assert item.source == "Example Site"
    assert item.author == "Example Author"
    assert item.original_published_at == datetime(
        2026,
        3,
        1,
        12,
        34,
        56,
        tzinfo=UTC,
    )
    assert item.media_url == "https://cdn.example.com/audio.mp3"
    assert item.audio_url == "https://cdn.example.com/audio.mp3"
    assert item.kind == ItemKind.PODCAST_EPISODE
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_error == ""
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.transcript == "Transcript paragraph one.\n\nTranscript paragraph two."
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Short generated summary"
    assert item.long_summary == "Long generated summary with more detail."
    assert item.tags == "radio\nculture\ninterview"
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.summary_error == ""


@pytest.mark.django_db
def test_api_items_are_public_immediately_and_join_feed_after_enrichment(
    client,
    settings,
    api_url: str,
    monkeypatch,
) -> None:
    stub_archive_audio(monkeypatch)
    settings.ARCHIVE_API_TOKEN = "test-token"

    response = client.post(
        api_url,
        data='{"url":"https://example.com/shared"}',
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 201
    item = Item.objects.get(pk=response.json()["id"])
    assert item.title == ""
    assert item.enrichment_status == EnrichmentStatus.PENDING
    assert b"https://example.com/shared" in client.get(reverse("archive:overview")).content
    assert b"<item>" not in client.get(reverse("archive:rss-feed")).content

    def fake_extract(url: str, timeout: int):
        return extract_metadata_from_html(
            html="""
            <html>
              <head>
                <meta property="og:title" content="Extracted title">
                <meta property="og:site_name" content="Example Site">
                <meta property="og:audio" content="https://cdn.example.com/audio.mp3">
              </head>
            </html>
            """,
            base_url=url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="A compact generated summary.",
            long_summary="A longer generated summary for the detail page.",
            tags=("example", "shared", "article"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript from audio.",
    )

    assert enrich_pending_items(limit=1) == 1

    item.refresh_from_db()
    assert item.title == "Extracted title"
    assert item.source == "Example Site"
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.transcript == "Transcript from audio."
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "A compact generated summary."
    assert b"Extracted title" in client.get(reverse("archive:rss-feed")).content
    assert b"A compact generated summary." in client.get(reverse("archive:rss-feed")).content


@pytest.mark.django_db
def test_enrich_item_metadata_marks_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/demo",
        kind=ItemKind.LINK,
        short_summary="Ready",
        long_summary="Already summarized.",
        tags="demo",
        summary_status=EnrichmentStatus.COMPLETE,
    )

    def fake_extract(url: str, timeout: int):
        raise MetadataExtractionError("boom")

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.FAILED
    assert item.enrichment_error == "boom"


@pytest.mark.django_db
def test_request_item_reprocess_preserves_operator_override_and_resets_media_archive() -> None:
    item = Item.objects.create(
        original_url="https://castro.fm/episode/ubOf93",
        kind=ItemKind.ARTICLE,
        classification_rule="operator_override",
        classification_evidence={"operator_override": {"kind": ItemKind.ARTICLE}},
        audio_url="https://cdn.example.com/source.mp3",
        enrichment_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_error="download failed",
        media_archive_retry_count=2,
        summary_status=EnrichmentStatus.COMPLETE,
        short_summary="Summary",
        long_summary="Longer summary.",
    )

    request_item_reprocess(item)

    item.refresh_from_db()
    assert item.kind == ItemKind.ARTICLE
    assert item.classification_rule == "operator_override"
    assert item.enrichment_status == EnrichmentStatus.PENDING
    assert item.enrichment_error == ""
    assert item.media_archive_status == EnrichmentStatus.PENDING
    assert item.media_archive_error == ""
    assert item.media_archive_retry_count == 0


@pytest.mark.django_db
def test_reclassify_items_dry_run_reports_changed_and_unchanged_items() -> None:
    changed_item = Item.objects.create(
        original_url="https://castro.fm/episode/ubOf93",
        kind=ItemKind.LINK,
        classification_rule="default_link",
        classification_engine_version=1,
        classification_evidence={},
    )
    unchanged_decision = classify_item(original_url="https://example.com/article")
    unchanged_item = Item.objects.create(
        original_url="https://example.com/article",
        kind=unchanged_decision.kind,
        classification_rule=unchanged_decision.rule,
        classification_engine_version=CURRENT_CLASSIFICATION_ENGINE_VERSION,
        classification_evidence=unchanged_decision.evidence,
    )

    stdout = StringIO()
    call_command(
        "reclassify_items",
        "--item-id",
        str(changed_item.pk),
        "--item-id",
        str(unchanged_item.pk),
        stdout=stdout,
    )

    output = stdout.getvalue()
    changed_item.refresh_from_db()
    unchanged_item.refresh_from_db()

    assert "Dry-run mode: no items will be updated" in output
    assert f"WOULD UPDATE item {changed_item.pk}" in output
    assert "kind: link -> podcast_episode" in output
    assert "rule: default_link -> adapter_castro_episode" in output
    assert "engine_version: 1 -> 2" in output
    assert f"UNCHANGED item {unchanged_item.pk}" in output
    assert changed_item.kind == ItemKind.LINK
    assert changed_item.classification_engine_version == 1


@pytest.mark.django_db
def test_reclassify_items_host_filter_limits_replay_scope() -> None:
    matched_item = Item.objects.create(
        original_url="https://castro.fm/episode/ubOf93",
        kind=ItemKind.LINK,
        classification_rule="default_link",
        classification_engine_version=1,
        classification_evidence={},
    )
    other_item = Item.objects.create(
        original_url="https://example.com/story",
        audio_url="https://cdn.example.com/story.mp3",
        kind=ItemKind.LINK,
        classification_rule="default_link",
        classification_engine_version=1,
        classification_evidence={},
    )

    stdout = StringIO()
    call_command(
        "reclassify_items",
        "--host",
        "castro.fm",
        "--limit",
        "1",
        stdout=stdout,
    )

    output = stdout.getvalue()

    assert f"WOULD UPDATE item {matched_item.pk}" in output
    assert f"item {other_item.pk}: {other_item.original_url}" not in output
    assert "Summary: inspected=1 changed=1 unchanged=0 mode=dry-run" in output


@pytest.mark.django_db
def test_reclassify_items_apply_updates_classification_without_requeueing_downstream_work() -> None:
    item = Item.objects.create(
        original_url="https://example.com/story",
        audio_url="https://cdn.example.com/story.mp3",
        kind=ItemKind.LINK,
        classification_rule="default_link",
        classification_engine_version=1,
        classification_evidence={},
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_error="download failed",
    )

    stdout = StringIO()
    call_command(
        "reclassify_items",
        "--item-id",
        str(item.pk),
        "--apply",
        stdout=stdout,
    )

    output = stdout.getvalue()
    item.refresh_from_db()

    assert "Apply mode: updating classification fields only." in output
    assert f"UPDATED item {item.pk}" in output
    assert item.kind == ItemKind.PODCAST_EPISODE
    assert item.classification_rule == "audio_url_signal"
    assert item.classification_engine_version == CURRENT_CLASSIFICATION_ENGINE_VERSION
    assert item.classification_evidence["selected_media"]["audio"] == (
        "https://cdn.example.com/story.mp3"
    )
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.media_archive_error == "download failed"


@pytest.mark.django_db
def test_reclassify_items_preserves_operator_override_in_apply_mode() -> None:
    item = Item.objects.create(
        original_url="https://castro.fm/episode/ubOf93",
        kind=ItemKind.ARTICLE,
        classification_rule="operator_override",
        classification_engine_version=1,
        classification_evidence={"operator_override": {"kind": ItemKind.ARTICLE}},
        article_audio_status=EnrichmentStatus.COMPLETE,
    )

    call_command(
        "reclassify_items",
        "--item-id",
        str(item.pk),
        "--apply",
        stdout=StringIO(),
    )

    item.refresh_from_db()
    assert item.kind == ItemKind.ARTICLE
    assert item.classification_rule == "operator_override"
    assert item.classification_engine_version == CURRENT_CLASSIFICATION_ENGINE_VERSION
    assert item.article_audio_status == EnrichmentStatus.COMPLETE


@pytest.mark.django_db
def test_reclassify_items_normalize_downstream_dry_run_reports_stale_status_cleanup() -> None:
    poll_at = timezone.now() + timedelta(minutes=5)
    retry_at = timezone.now() + timedelta(minutes=10)
    item = Item.objects.create(
        original_url="https://example.com/story",
        kind=ItemKind.ARTICLE,
        classification_rule="metadata_kind_hint",
        classification_engine_version=1,
        classification_evidence={},
        transcript_status=EnrichmentStatus.FAILED,
        transcript_error="transcript failed",
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_error="download failed",
        media_archive_retry_count=2,
        media_archive_retry_at=retry_at,
        article_audio_status=EnrichmentStatus.FAILED,
        article_audio_error="tts failed",
        article_audio_poll_at=poll_at,
    )

    stdout = StringIO()
    call_command(
        "reclassify_items",
        "--item-id",
        str(item.pk),
        "--normalize-downstream",
        stdout=stdout,
    )

    output = stdout.getvalue()
    item.refresh_from_db()

    assert "Downstream normalization is previewed only" in output
    assert f"WOULD UPDATE item {item.pk}" in output
    assert "kind: article -> link" in output
    assert "transcript: status: failed -> complete" in output
    assert "media_archive: status: failed -> complete" in output
    assert "article_audio: status: failed -> complete" in output
    assert item.kind == ItemKind.ARTICLE
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.article_audio_status == EnrichmentStatus.FAILED
    assert item.transcript_status == EnrichmentStatus.FAILED


@pytest.mark.django_db
def test_reclassify_items_apply_with_normalize_downstream_cleans_unsupported_statuses() -> None:
    item = Item.objects.create(
        original_url="https://example.com/story",
        kind=ItemKind.ARTICLE,
        classification_rule="metadata_kind_hint",
        classification_engine_version=1,
        classification_evidence={},
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.FAILED,
        transcript_error="transcript failed",
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_error="download failed",
        media_archive_retry_count=2,
        media_archive_retry_at=timezone.now() + timedelta(minutes=10),
        article_audio_status=EnrichmentStatus.FAILED,
        article_audio_error="tts failed",
        article_audio_poll_at=timezone.now() + timedelta(minutes=5),
    )

    stdout = StringIO()
    call_command(
        "reclassify_items",
        "--item-id",
        str(item.pk),
        "--apply",
        "--normalize-downstream",
        stdout=stdout,
    )

    output = stdout.getvalue()
    item.refresh_from_db()

    assert (
        "Apply mode: updating classification fields plus explicit downstream normalization only."
        in output
    )
    assert item.kind == ItemKind.LINK
    assert item.classification_rule == "default_link"
    assert item.classification_engine_version == CURRENT_CLASSIFICATION_ENGINE_VERSION
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.transcript_error == ""
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.media_archive_error == ""
    assert item.media_archive_retry_count == 0
    assert item.media_archive_retry_at is None
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_error == ""
    assert item.article_audio_poll_at is None
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert claim_pending_item() is None


@pytest.mark.django_db
def test_reclassify_items_normalize_downstream_does_not_queue_newly_eligible_work() -> None:
    item = Item.objects.create(
        original_url="https://example.com/story",
        audio_url="https://cdn.example.com/story.mp3",
        kind=ItemKind.LINK,
        classification_rule="default_link",
        classification_engine_version=1,
        classification_evidence={},
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.COMPLETE,
        article_audio_status=EnrichmentStatus.COMPLETE,
    )

    call_command(
        "reclassify_items",
        "--item-id",
        str(item.pk),
        "--apply",
        "--normalize-downstream",
        stdout=StringIO(),
    )

    item.refresh_from_db()
    assert item.kind == ItemKind.PODCAST_EPISODE
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert claim_pending_item() is None


@pytest.mark.django_db
def test_reclassify_items_apply_with_normalize_downstream_cleans_materialized_statuses() -> None:
    item = Item.objects.create(
        original_url="https://example.com/story",
        kind=ItemKind.LINK,
        classification_rule="default_link",
        classification_engine_version=CURRENT_CLASSIFICATION_ENGINE_VERSION,
        classification_evidence={},
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript="Existing transcript text.",
        transcript_status=EnrichmentStatus.FAILED,
        transcript_error="transcript failed",
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=12345,
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_error="download failed",
        media_archive_retry_count=2,
        media_archive_retry_at=timezone.now() + timedelta(minutes=10),
        article_audio_generated=True,
        article_audio_artifact_path="/v1/jobs/job-123/artifacts/speech.mp3",
        article_audio_status=EnrichmentStatus.FAILED,
        article_audio_error="tts failed",
        article_audio_poll_at=timezone.now() + timedelta(minutes=5),
    )

    stdout = StringIO()
    call_command(
        "reclassify_items",
        "--item-id",
        str(item.pk),
        "--apply",
        "--normalize-downstream",
        stdout=stdout,
    )

    output = stdout.getvalue()
    item.refresh_from_db()

    assert "transcript already exists" in output
    assert "archived audio already exists" in output
    assert "generated article audio already exists" in output
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.transcript_error == ""
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.media_archive_error == ""
    assert item.media_archive_retry_count == 0
    assert item.media_archive_retry_at is None
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_error == ""
    assert item.article_audio_poll_at is None
    assert claim_pending_item() is None


@pytest.mark.django_db
def test_reclassify_items_dry_run_does_not_touch_downstream_statuses() -> None:
    item = Item.objects.create(
        original_url="https://example.com/story",
        audio_url="https://cdn.example.com/story.mp3",
        kind=ItemKind.LINK,
        classification_rule="default_link",
        classification_engine_version=1,
        classification_evidence={},
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_error="download failed",
        summary_status=EnrichmentStatus.FAILED,
        summary_error="summary failed",
        transcript_status=EnrichmentStatus.FAILED,
        transcript_error="transcript failed",
        article_audio_status=EnrichmentStatus.FAILED,
        article_audio_error="article audio failed",
    )

    call_command(
        "reclassify_items",
        "--item-id",
        str(item.pk),
        stdout=StringIO(),
    )

    item.refresh_from_db()
    assert item.kind == ItemKind.LINK
    assert item.classification_rule == "default_link"
    assert item.classification_engine_version == 1
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.media_archive_error == "download failed"
    assert item.summary_status == EnrichmentStatus.FAILED
    assert item.summary_error == "summary failed"
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.transcript_error == "transcript failed"
    assert item.article_audio_status == EnrichmentStatus.FAILED
    assert item.article_audio_error == "article audio failed"


@pytest.mark.django_db
def test_rebuild_search_index_restores_search_results_after_fts_drift(client) -> None:
    item = Item.objects.create(
        original_url="https://example.com/searchable",
        title="Search drift item",
        notes="Spruce term for rebuilding.",
    )

    initial_response = client.get(reverse("archive:search"), {"q": "spruce"})
    assert initial_response.status_code == 200
    assert [result.pk for result in initial_response.context["results"]] == [item.pk]

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM archive_item_fts WHERE rowid = %s", [item.pk])

    drifted_response = client.get(reverse("archive:search"), {"q": "spruce"})
    assert drifted_response.status_code == 200
    assert drifted_response.context["results"] == []

    stdout = StringIO()
    call_command("rebuild_search_index", stdout=stdout)

    rebuilt_response = client.get(reverse("archive:search"), {"q": "spruce"})
    assert rebuilt_response.status_code == 200
    assert [result.pk for result in rebuilt_response.context["results"]] == [item.pk]
    assert "Rebuilt Archive search index for 1 item." in stdout.getvalue()


@pytest.mark.django_db
def test_enrich_item_metadata_keeps_feed_ready_items_complete_when_fetch_fails(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/demo",
        title="Already ready",
        kind=ItemKind.LINK,
        enrichment_status=EnrichmentStatus.PROCESSING,
        short_summary="Ready",
        long_summary="Already summarized.",
        tags="ready",
        summary_status=EnrichmentStatus.COMPLETE,
    )

    def fake_extract(url: str, timeout: int):
        raise MetadataExtractionError("boom")

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_error == "boom"


@pytest.mark.django_db
def test_prepare_item_for_enrichment_marks_fully_populated_items_complete() -> None:
    item = Item(
        original_url="https://example.com/demo",
        title="Complete item",
        short_summary="Short",
        long_summary="Long",
        tags="complete",
        transcript="Transcript",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
    )

    prepare_item_for_enrichment(item)

    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.enrichment_error == ""
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.summary_error == ""
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.transcript_error == ""


@pytest.mark.django_db
def test_recover_processing_items_requeues_stale_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck",
        enrichment_status=EnrichmentStatus.PROCESSING,
    )
    Item.objects.create(
        original_url="https://example.com/pending",
        enrichment_status=EnrichmentStatus.PENDING,
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.enrichment_status == EnrichmentStatus.PENDING
    assert claim_pending_item() is not None


@pytest.mark.django_db
def test_recover_processing_items_requeues_summary_only_processing_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck-summary",
        title="Existing metadata",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PROCESSING,
        summary_retry_count=1,
        summary_retry_at=timezone.now() + timedelta(minutes=5),
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.enrichment_status == EnrichmentStatus.COMPLETE
    assert stuck.summary_status == EnrichmentStatus.PENDING
    assert stuck.summary_retry_at is None


@pytest.mark.django_db
def test_recover_processing_items_requeues_transcript_processing_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck-transcript.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.transcript_status == EnrichmentStatus.PENDING


@pytest.mark.django_db
def test_claim_pending_item_includes_archived_media_only_transcripts() -> None:
    item = Item.objects.create(
        original_url="https://example.com/archived-only",
        archived_audio_path="items/1/audio/source.mp3",
        transcript_status=EnrichmentStatus.PENDING,
    )

    claimed = claim_pending_item()

    assert claimed is not None
    assert claimed.pk == item.pk
    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.PROCESSING


@pytest.mark.django_db
def test_recover_processing_items_requeues_media_archive_processing_items() -> None:
    stuck = Item.objects.create(
        original_url="https://example.com/stuck-audio.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        media_archive_status=EnrichmentStatus.PROCESSING,
        media_archive_retry_count=1,
        media_archive_retry_at=timezone.now() + timedelta(minutes=5),
    )

    assert recover_processing_items() == 1

    stuck.refresh_from_db()
    assert stuck.media_archive_status == EnrichmentStatus.PENDING
    assert stuck.media_archive_retry_at is None


@pytest.mark.django_db
def test_enrich_item_media_archive_records_archived_audio(monkeypatch) -> None:
    stub_archive_audio(monkeypatch, size_bytes=4321)
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        title="Archived episode",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.media_archive_error == ""
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.archived_audio_content_type == "audio/mpeg"
    assert item.archived_audio_size_bytes == 4321


@pytest.mark.django_db
def test_enrich_item_media_archive_records_video_source_for_extracted_audio(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        title="Archived video",
        kind=ItemKind.VIDEO,
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
    )
    monkeypatch.setattr(
        "archive.services.archive_item_audio",
        lambda item, timeout: ArchivedAudio(
            object_name=f"items/{item.pk}/audio/extracted.mp3",
            content_type="audio/mpeg",
            size_bytes=7654,
            source_object_name=f"items/{item.pk}/video/source.mp4",
            source_content_type="video/mp4",
            source_size_bytes=98765,
        ),
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == f"items/{item.pk}/audio/extracted.mp3"
    assert item.archived_audio_content_type == "audio/mpeg"
    assert item.archived_audio_size_bytes == 7654
    assert item.archived_video_path == f"items/{item.pk}/video/source.mp4"
    assert item.archived_video_content_type == "video/mp4"
    assert item.archived_video_size_bytes == 98765


@pytest.mark.django_db
def test_prepare_item_for_enrichment_marks_supported_youtube_page_for_media_archive() -> None:
    item = Item(
        original_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
    )

    prepare_item_for_enrichment(item)

    assert item.media_archive_status == EnrichmentStatus.PENDING
    assert item.media_archive_error == ""
    assert item.media_archive_retry_count == 0
    assert item.media_archive_retry_at is None


@pytest.mark.django_db
def test_video_archive_migration_requeues_existing_querystring_urls() -> None:
    migration = importlib.import_module(
        "archive.migrations.0008_item_archived_video_content_type_and_more"
    )
    eligible = Item.objects.create(
        original_url="https://cdn.example.com/video.mp4?token=abc",
        media_archive_status=EnrichmentStatus.COMPLETE,
    )
    Item.objects.create(
        original_url="https://cdn.example.com/audio.mp3?token=abc",
        media_archive_status=EnrichmentStatus.COMPLETE,
    )
    Item.objects.create(
        original_url="https://cdn.example.com/already-pending.mp4?token=abc",
        media_archive_status=EnrichmentStatus.PENDING,
    )

    migration.queue_existing_archivable_video(_MigrationApps(), None)

    eligible.refresh_from_db()
    assert eligible.media_archive_status == EnrichmentStatus.PENDING


@pytest.mark.django_db
def test_enrich_item_media_archive_marks_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        title="Archived episode",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
    )
    monkeypatch.setattr(
        "archive.services.archive_item_audio",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("archive boom")),
    )

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.media_archive_error == "archive boom"
    assert item.media_archive_retry_count == 1
    assert item.media_archive_retry_at is not None


@pytest.mark.django_db
def test_failed_media_archive_is_not_retried_before_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-audio.mp3",
        title="Retry later",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_retry_count=1,
        media_archive_retry_at=timezone.now() + timedelta(minutes=1),
    )

    assert claim_pending_item() is None

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED


@pytest.mark.django_db
def test_failed_media_archive_is_retried_after_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-audio-now.mp3",
        title="Retry now",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.FAILED,
        media_archive_retry_count=1,
        media_archive_retry_at=timezone.now() - timedelta(seconds=1),
        media_archive_error="temporary outage",
    )

    claimed = claim_pending_item()

    assert claimed is not None
    assert claimed.pk == item.pk
    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.PROCESSING
    assert item.media_archive_error == ""
    assert item.media_archive_retry_at is None


@pytest.mark.django_db
def test_media_archive_failures_stop_retrying_after_retry_limit(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-limit-audio.mp3",
        title="Retry limit",
        kind=ItemKind.PODCAST_EPISODE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
        media_archive_retry_count=len(MEDIA_ARCHIVE_RETRY_DELAYS),
    )
    monkeypatch.setattr(
        "archive.services.archive_item_audio",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("still broken")),
    )

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.FAILED
    assert item.media_archive_retry_count == len(MEDIA_ARCHIVE_RETRY_DELAYS) + 1
    assert item.media_archive_retry_at is None
    assert claim_pending_item() is None


@pytest.mark.django_db
def test_enrich_pending_items_claims_oldest_pending_item(monkeypatch) -> None:
    older = Item.objects.create(
        original_url="https://example.com/older",
        title="",
        shared_at=timezone.now() - timedelta(minutes=1),
    )
    newer = Item.objects.create(
        original_url="https://example.com/newer",
        title="",
        shared_at=timezone.now(),
    )
    prepare_item_for_enrichment(older)
    older.save(update_fields=["enrichment_status"])
    prepare_item_for_enrichment(newer)
    newer.save(update_fields=["enrichment_status"])

    def fake_extract(url: str, timeout: int):
        return extract_metadata_from_html(
            html=f"""
            <html>
              <head>
                <title>{url}</title>
              </head>
            </html>
            """,
            base_url=url,
        )

    monkeypatch.setattr("archive.services.extract_metadata_from_url", fake_extract)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary=f"Summary for {item.original_url}",
            long_summary=f"Long summary for {item.original_url}",
            tags=("queued", "summary", "test"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript",
    )

    assert enrich_pending_items(limit=1) == 1

    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.enrichment_status == EnrichmentStatus.COMPLETE
    assert older.transcript_status == EnrichmentStatus.COMPLETE
    assert older.summary_status == EnrichmentStatus.COMPLETE
    assert newer.enrichment_status == EnrichmentStatus.PENDING
    assert newer.summary_status == EnrichmentStatus.PENDING


@pytest.mark.django_db
def test_enrich_pending_items_runs_summary_backfill_without_refetching_metadata(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/backfill",
        title="Already enriched",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PENDING,
    )

    def unexpected_extract(url: str, timeout: int):
        raise AssertionError("metadata extraction should be skipped for summary backfill")

    monkeypatch.setattr("archive.services.extract_metadata_from_url", unexpected_extract)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Backfilled short summary",
            long_summary="Backfilled long summary",
            tags=("backfill", "summary", "test"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript for the item.",
    )

    assert enrich_pending_items(limit=1) == 1

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Backfilled short summary"


@pytest.mark.django_db
def test_enrich_item_metadata_keeps_manual_generated_values(monkeypatch) -> None:
    stub_archive_audio(monkeypatch)
    item = Item.objects.create(
        original_url="https://example.com/manual-summary",
        title="Manual summary item",
        short_summary="Manual short summary",
        tags="manual",
    )
    prepare_item_for_enrichment(item)
    item.save(update_fields=["enrichment_status", "summary_status", "summary_error"])

    monkeypatch.setattr(
        "archive.services.extract_metadata_from_url",
        lambda url, timeout: extract_metadata_from_html(
            html="""
            <html>
              <head>
                <meta property="og:site_name" content="Example Site">
                <meta name="author" content="Example Author">
                <meta property="article:published_time" content="2026-03-01T12:34:56+00:00">
                <meta property="og:audio" content="https://cdn.example.com/audio.mp3">
              </head>
            </html>
            """,
            base_url=url,
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Generated short summary should not overwrite",
            long_summary="Generated long summary should fill only the missing field.",
            tags=("generated", "manual", "test"),
        ),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Generated transcript",
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.short_summary == "Manual short summary"
    assert item.long_summary == "Generated long summary should fill only the missing field."
    assert item.tags == "manual"
    assert item.archived_audio_path == f"items/{item.pk}/audio/source.mp3"
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.transcript == "Generated transcript"


@pytest.mark.django_db
def test_transcript_requeues_and_refreshes_generated_summaries(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        title="Transcript refresh",
        kind=ItemKind.PODCAST_EPISODE,
        short_summary="Old generated short summary",
        long_summary="Old generated long summary.",
        tags="old\ngenerated",
        short_summary_generated=True,
        long_summary_generated=True,
        tags_generated=True,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Fresh transcript text.",
    )
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: GeneratedSummary(
            short_summary="Improved short summary",
            long_summary="Improved long summary",
            tags=("improved", "transcript", "refresh"),
        ),
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.transcript == "Fresh transcript text."
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Improved short summary"
    assert item.long_summary == "Improved long summary"
    assert item.tags == "improved\ntranscript\nrefresh"


@pytest.mark.django_db
def test_transcript_does_not_overwrite_manual_summary_fields(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/manual.mp3",
        title="Manual fields",
        kind=ItemKind.PODCAST_EPISODE,
        short_summary="Manual short summary",
        long_summary="Manual long summary",
        tags="manual",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Fresh transcript text.",
    )

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.transcript == "Fresh transcript text."
    assert item.summary_status == EnrichmentStatus.COMPLETE
    assert item.short_summary == "Manual short summary"
    assert item.long_summary == "Manual long summary"
    assert item.tags == "manual"


@pytest.mark.django_db
def test_youtube_item_transcribes_from_archived_media_after_archival(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://www.youtube.com/watch?v=demo123",
        title="Archived YouTube item",
        kind=ItemKind.VIDEO,
        short_summary="Manual short summary",
        long_summary="Manual long summary",
        tags="manual",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.PROCESSING,
        transcript_status=EnrichmentStatus.PROCESSING,
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.openai.com/v1"
    settings.ARCHIVE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

    def fake_archive_item_audio(item, timeout):
        audio_path = f"items/{item.pk}/audio/extracted.mp3"
        video_path = f"items/{item.pk}/video/source.mp4"
        _save_archived_media(audio_path, b"archived-youtube-audio")
        _save_archived_media(video_path, b"archived-youtube-video")
        return ArchivedAudio(
            object_name=audio_path,
            content_type="audio/mpeg",
            size_bytes=len(b"archived-youtube-audio"),
            source_object_name=video_path,
            source_content_type="video/mp4",
            source_size_bytes=len(b"archived-youtube-video"),
        )

    class _FakeTranscriptionResponse:
        headers = SimpleNamespace(get_content_type=lambda: "application/json")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return b'{"text":"Transcript from archived YouTube audio."}'

    def fake_urlopen(request, timeout):
        assert request.full_url != "https://www.youtube.com/watch?v=demo123"
        assert request.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert b"archived-youtube-audio" in request.data
        return _FakeTranscriptionResponse()

    monkeypatch.setattr("archive.services.archive_item_audio", fake_archive_item_audio)
    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    assert enrich_item_metadata(item) is True

    item.refresh_from_db()
    assert item.media_archive_status == EnrichmentStatus.COMPLETE
    assert item.archived_audio_path == f"items/{item.pk}/audio/extracted.mp3"
    assert item.archived_video_path == f"items/{item.pk}/video/source.mp4"
    assert item.transcript == "Transcript from archived YouTube audio."
    assert item.transcript_status == EnrichmentStatus.COMPLETE


@pytest.mark.django_db
def test_enrich_item_metadata_marks_summary_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/summary-failure",
        title="Summary failure item",
        source="Example Site",
        author="Example Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("bad prompt")),
    )
    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: "Transcript text",
    )

    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.enrichment_status == EnrichmentStatus.COMPLETE
    assert item.summary_status == EnrichmentStatus.FAILED
    assert "bad prompt" in item.summary_error
    assert item.summary_retry_count == 1
    assert item.summary_retry_at is not None


@pytest.mark.django_db
def test_failed_summary_is_not_retried_before_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-window",
        title="Retry later",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.FAILED,
        summary_retry_count=1,
        summary_retry_at=timezone.now() + timedelta(minutes=1),
        transcript_status=EnrichmentStatus.COMPLETE,
    )

    assert claim_pending_item() is None

    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.FAILED


@pytest.mark.django_db
def test_failed_summary_is_retried_after_backoff_window() -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-now",
        title="Retry now",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.FAILED,
        summary_retry_count=1,
        summary_retry_at=timezone.now() - timedelta(seconds=1),
        summary_error="temporary outage",
        transcript_status=EnrichmentStatus.COMPLETE,
    )

    claimed = claim_pending_item()

    assert claimed is not None
    assert claimed.pk == item.pk
    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.PROCESSING
    assert item.summary_error == ""
    assert item.summary_retry_at is None


@pytest.mark.django_db
def test_summary_failures_stop_retrying_after_retry_limit(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/retry-limit",
        title="Retry limit",
        source="Example",
        author="Author",
        original_published_at=timezone.now(),
        media_url="https://cdn.example.com/video.mp4",
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.PROCESSING,
        summary_retry_count=len(SUMMARY_RETRY_DELAYS),
        transcript_status=EnrichmentStatus.COMPLETE,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("still broken")),
    )
    assert enrich_item_metadata(item) is False

    item.refresh_from_db()
    assert item.summary_status == EnrichmentStatus.FAILED
    assert item.summary_retry_count == len(SUMMARY_RETRY_DELAYS) + 1
    assert item.summary_retry_at is None
    assert claim_pending_item() is None


@pytest.mark.django_db
def test_enrich_item_transcript_marks_failures(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/failure.mp3",
        title="Transcript failure",
        kind=ItemKind.PODCAST_EPISODE,
        transcript_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_transcript",
        lambda item, timeout: (_ for _ in ()).throw(RuntimeError("bad audio")),
    )

    assert enrich_item_transcript(item) is False

    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.transcript_error == "bad audio"


@pytest.mark.django_db
def test_enrich_item_article_audio_records_pending_job(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        title="Article headline",
        short_summary="Short summary",
        long_summary="Long summary for audio.",
        kind=ItemKind.ARTICLE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        article_audio_status=EnrichmentStatus.PROCESSING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_article_audio",
        lambda item, timeout: ArticleAudioJobUpdate(job_id="job-123", state="queued"),
    )

    assert enrich_item_article_audio(item) is True

    item.refresh_from_db()
    assert item.article_audio_status == EnrichmentStatus.PENDING
    assert item.article_audio_job_id == "job-123"
    assert item.article_audio_poll_at is not None


@pytest.mark.django_db
def test_enrich_pending_items_completes_generated_article_audio(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        title="Article headline",
        short_summary="Short summary",
        long_summary="Long summary for audio.",
        kind=ItemKind.ARTICLE,
        enrichment_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.COMPLETE,
        article_audio_status=EnrichmentStatus.PENDING,
    )

    monkeypatch.setattr(
        "archive.services.generate_item_article_audio",
        lambda item, timeout: ArticleAudioJobUpdate(
            job_id="job-123",
            state="succeeded",
            artifact_path="/v1/jobs/job-123/artifacts/speech.mp3",
        ),
    )

    assert enrich_pending_items(limit=1) == 1

    item.refresh_from_db()
    assert item.article_audio_status == EnrichmentStatus.COMPLETE
    assert item.article_audio_generated is True
    assert item.article_audio_artifact_path == "/v1/jobs/job-123/artifacts/speech.mp3"
