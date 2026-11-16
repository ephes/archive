from __future__ import annotations

from datetime import UTC, datetime

import pytest

from archive.metadata import extract_metadata_from_html

BASE_URL = "https://example.com/articles/demo"


def _page(published_meta: str = "", jsonld_date: str = "") -> str:
    jsonld = (
        '<script type="application/ld+json">'
        f'{{"@type":"Article","headline":"LD Title","datePublished":"{jsonld_date}"}}'
        "</script>"
        if jsonld_date
        else ""
    )
    return f"""
    <html><head>
      <title>Fallback</title>
      <meta property="og:title" content="Useful Title">
      <meta property="og:audio" content="/media/episode.mp3">
      {published_meta}
      {jsonld}
    </head><body></body></html>
    """


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-31",
        "2026-02-31T10:00:00",
        "2026-02-31T10:00:00+00:00",
        "2026-13-01",
        "2026-01-01T25:00:00",
        "9999-12-31T23:59:59-23:59",
        "Mon, 31 Feb 2026 10:00:00 +0000",
    ],
)
def test_impossible_optional_publication_date_keeps_other_metadata(value) -> None:
    html = _page(f'<meta property="article:published_time" content="{value}">')

    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.original_published_at is None
    assert metadata.title == "Useful Title"
    assert metadata.audio_url == "https://example.com/media/episode.mp3"


def test_impossible_jsonld_date_keeps_other_jsonld_and_html_metadata() -> None:
    html = _page(jsonld_date="2026-02-31T08:00:00Z")

    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.original_published_at is None
    assert metadata.title == "Useful Title"
    assert metadata.media_url == "https://example.com/media/episode.mp3"


def test_invalid_first_date_candidate_falls_through_to_next_valid_candidate() -> None:
    html = _page(
        '<meta property="article:published_time" content="2026-02-31T10:00:00Z">'
        '<meta name="date" content="2026-03-02T08:15:00+00:00">'
    )

    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.original_published_at == datetime(2026, 3, 2, 8, 15, tzinfo=UTC)


def test_invalid_html_date_falls_through_to_valid_jsonld_date() -> None:
    html = _page(
        '<meta property="article:published_time" content="2026-02-31">',
        jsonld_date="2026-03-04T05:06:07Z",
    )

    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.original_published_at == datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-01T12:34:56+00:00", datetime(2026, 3, 1, 12, 34, 56, tzinfo=UTC)),
        ("2026-03-01T14:34:56+02:00", datetime(2026, 3, 1, 12, 34, 56, tzinfo=UTC)),
        ("Sun, 01 Mar 2026 12:34:56 +0000", datetime(2026, 3, 1, 12, 34, 56, tzinfo=UTC)),
        ("Sun, 01 Mar 2026 13:34:56 +0100", datetime(2026, 3, 1, 12, 34, 56, tzinfo=UTC)),
    ],
)
def test_valid_iso_and_rfc_publication_dates_are_retained(value, expected) -> None:
    html = _page(f'<meta property="article:published_time" content="{value}">')

    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.original_published_at == expected


def test_valid_date_only_value_uses_default_timezone_midnight(settings) -> None:
    html = _page('<meta property="article:published_time" content="2026-03-01">')

    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.original_published_at is not None
    assert metadata.original_published_at.date().isoformat() == "2026-03-01"
    assert metadata.original_published_at.utcoffset() is not None


def test_unrelated_extraction_errors_are_not_hidden(monkeypatch) -> None:
    def broken_title(*values):
        raise RuntimeError("unrelated")

    monkeypatch.setattr("archive.metadata._first_nonempty", broken_title)

    with pytest.raises(RuntimeError, match="unrelated"):
        extract_metadata_from_html(_page(), base_url=BASE_URL)
