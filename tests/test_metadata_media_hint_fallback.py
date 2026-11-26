from __future__ import annotations

from archive.metadata import extract_metadata_from_html

BASE_URL = "https://example.com/post"
REFUSED = "javascript:alert(1)"


def test_refused_open_graph_hint_falls_through_to_the_next_admitted_hint() -> None:
    html = f"""
    <html><head>
      <meta property="og:audio" content="{REFUSED}">
      <meta property="og:audio:url" content="/media/episode.mp3">
    </head><body>
      <video src="https://cdn.example.com/lower-priority.mp4"></video>
    </body></html>
    """
    result = extract_metadata_from_html(html, base_url=BASE_URL)

    assert result.media_url == "https://example.com/media/episode.mp3"
    assert result.audio_url == "https://example.com/media/episode.mp3"


def test_refused_jsonld_hint_falls_through_to_the_next_admitted_field() -> None:
    html = f"""
    <html><head>
      <script type="application/ld+json">
        {{"@type": "AudioObject", "contentUrl": "{REFUSED}",
          "embedUrl": "/media/episode.mp3"}}
      </script>
    </head><body>
      <video src="https://cdn.example.com/lower-priority.mp4"></video>
    </body></html>
    """
    result = extract_metadata_from_html(html, base_url=BASE_URL)

    assert result.media_url == "https://example.com/media/episode.mp3"
    assert result.audio_url == "https://example.com/media/episode.mp3"


def test_refused_first_html_audio_source_falls_through_to_the_next_one() -> None:
    html = f"""
    <html><body>
      <audio src="{REFUSED}"></audio>
      <audio><source src="/media/episode.mp3"></audio>
    </body></html>
    """
    result = extract_metadata_from_html(html, base_url=BASE_URL)

    assert result.audio_url == "https://example.com/media/episode.mp3"


def test_refused_first_html_video_source_falls_through_to_the_next_one() -> None:
    html = f"""
    <html><body>
      <video src="{REFUSED}"></video>
      <video src="/media/clip.mp4"></video>
    </body></html>
    """
    result = extract_metadata_from_html(html, base_url=BASE_URL)

    assert result.media_url == "https://example.com/media/clip.mp4"
