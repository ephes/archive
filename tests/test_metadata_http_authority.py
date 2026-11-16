"""Page media references need a usable HTTP(S) authority to be retained."""

from __future__ import annotations

import pytest

from archive.metadata import extract_metadata_from_html

BASE_URL = "https://example.com/post"


def _page(reference: str) -> str:
    return f"""
    <html><head>
      <meta property="og:title" content="Kept title">
      <meta property="article:published_time" content="2026-03-01T12:00:00+00:00">
      <meta property="og:video" content="{reference}">
      <script type="application/ld+json">
        {{"@type": "AudioObject", "contentUrl": "{reference}"}}
      </script>
    </head><body>
      <audio src="{reference}"></audio>
      <video><source src="{reference}"></video>
    </body></html>
    """


@pytest.mark.parametrize(
    "reference",
    [
        "http://:80/episode.mp3",
        "http:///episode.mp3",
        "http://user@/episode.mp3",
        "http://user:pw@:443/episode.mp3",
        "http://cdn.example.com:99999/episode.mp3",
        "http://cdn.example.com:65536/episode.mp3",
        "http://cdn.example.com:abc/episode.mp3",
        "http://cdn.example.com:+80/episode.mp3",
        "http://cdn.example.com:-1/episode.mp3",
        "http://cdn.example.com:0/episode.mp3",
        "http://[::1/episode.mp3",
        "http://::1]/episode.mp3",
        "http://[not-an-ip]/episode.mp3",
        "http://[fe80::1%25eth0]/episode.mp3",
        "http://cdn example.com/episode.mp3",
        "http://cdn.example.com%00/episode.mp3",
        "http://cdn.exa\x01mple.com/episode.mp3",
        "http://cdn.exa\x7fmple.com/episode.mp3",
        "http://cdn.example.com/epi\x00sode.mp3",
        "http://cdn<example>.com/episode.mp3",
        "http://cdn.example.com\\evil/episode.mp3",
    ],
)
def test_invalid_http_authorities_are_dropped_keeping_other_metadata(reference) -> None:
    metadata = extract_metadata_from_html(_page(reference), base_url=BASE_URL)

    assert metadata.media_url == ""
    assert metadata.audio_url == ""
    assert metadata.media_candidates == ()
    assert metadata.title == "Kept title"
    assert metadata.original_published_at is not None


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("/media/episode.mp3", "https://example.com/media/episode.mp3"),
        ("episode.mp3", "https://example.com/episode.mp3"),
        ("//cdn.example.com/episode.mp3", "https://cdn.example.com/episode.mp3"),
        ("http://cdn.example.com:8080/episode.mp3", "http://cdn.example.com:8080/episode.mp3"),
        ("https://cdn.example.com:/episode.mp3", "https://cdn.example.com:/episode.mp3"),
        ("http://[2001:db8::1]/episode.mp3", "http://[2001:db8::1]/episode.mp3"),
        ("http://[2001:db8::1]:8443/episode.mp3", "http://[2001:db8::1]:8443/episode.mp3"),
        ("https://user@cdn.example.com/episode.mp3", "https://user@cdn.example.com/episode.mp3"),
        ("https://xn--bcher-kva.example/episode.mp3", "https://xn--bcher-kva.example/episode.mp3"),
        ("https://bücher.example/episode.mp3", "https://bücher.example/episode.mp3"),
        ("https://cdn.example.com/a%20b.mp3", "https://cdn.example.com/a%20b.mp3"),
        ("https://cdn.example.com:65535/episode.mp3", "https://cdn.example.com:65535/episode.mp3"),
    ],
)
def test_valid_relative_ipv6_and_port_references_are_kept(reference, expected) -> None:
    metadata = extract_metadata_from_html(_page(reference), base_url=BASE_URL)

    assert metadata.media_url == expected
    assert metadata.audio_url == expected
