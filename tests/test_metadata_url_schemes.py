from __future__ import annotations

import pytest
from django.urls import reverse

from archive.metadata import extract_metadata_from_html
from archive.models import Item, ItemKind
from archive.services import _enrich_item_metadata_fields

BASE_URL = "https://example.com/post"


@pytest.mark.parametrize(
    "reference",
    [
        "javascript:alert(document.domain)",
        " JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///srv/archive/db.sqlite3",
        "vbscript:msgbox(1)",
        "ftp://example.com/episode.mp3",
    ],
)
def test_page_media_references_with_other_schemes_are_dropped(reference) -> None:
    html = f"""
    <html><head>
      <meta property="og:video" content="{reference}">
      <script type="application/ld+json">
        {{"@type": "AudioObject", "contentUrl": "{reference}"}}
      </script>
    </head><body>
      <audio src="{reference}"></audio>
      <video><source src="{reference}"></video>
    </body></html>
    """
    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.media_url == ""
    assert metadata.audio_url == ""
    assert metadata.media_candidates == ()


def test_http_and_relative_page_media_references_are_kept() -> None:
    html = """
    <html><head><meta property="og:video" content="//cdn.example.com/clip.mp4"></head>
    <body><audio src="/media/episode.mp3"></audio></body></html>
    """
    metadata = extract_metadata_from_html(html, base_url=BASE_URL)

    assert metadata.media_url == "https://cdn.example.com/clip.mp4"
    assert metadata.audio_url == "https://example.com/media/episode.mp3"


@pytest.mark.django_db
def test_script_url_from_page_never_reaches_the_public_item_page(client, monkeypatch) -> None:
    html = (
        "<html><head><title>Post</title>"
        '<meta property="og:video" content="javascript:alert(1)"></head></html>'
    )
    monkeypatch.setattr(
        "archive.services.extract_metadata_from_url",
        lambda url, timeout=15: extract_metadata_from_html(html, base_url=url),
    )
    item = Item.objects.create(original_url=BASE_URL, kind=ItemKind.LINK, title="Post")

    _enrich_item_metadata_fields(item)
    item.refresh_from_db()

    assert item.media_url == ""
    response = client.get(reverse("archive:item-detail", kwargs={"pk": item.pk}))
    assert response.status_code == 200
    assert b"javascript:" not in response.content
