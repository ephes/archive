"""Ordered deduplication must not perform a scan of all previous candidates."""

import json

from archive import metadata as module


def test_ard_urls_keep_first_seen_order_with_linear_equality_checks(monkeypatch):
    class CountedURL(str):
        comparisons = 0
        __hash__ = str.__hash__

        def strip(self, *args):
            return self

        def __eq__(self, other):
            CountedURL.comparisons += 1
            return super().__eq__(other)

    urls = [f"https://cdn.example.com/{i}.mp3" for i in range(1000)]
    loads = json.loads
    monkeypatch.setattr(module.json, "loads", lambda blob: CountedURL(loads(blob)))
    blob = "mediaResource " + " ".join(
        '"audioURL":' + json.dumps(url) for url in [*urls, *urls, *reversed(urls)]
    )
    result = module._ard_audio_urls_from_script(blob)
    assert list(map(str, result)) == urls
    assert CountedURL.comparisons <= 3 * len(urls)


def test_html_candidates_keep_type_and_source_with_linear_equality_checks(monkeypatch):
    comparisons = []
    original_eq = module.ExtractedMediaCandidate.__eq__

    def compare(self, other):
        comparisons.append(True)
        return original_eq(self, other)

    monkeypatch.setattr(module.ExtractedMediaCandidate, "__eq__", compare)
    urls = [f"https://cdn.example.com/{i}.mp3" for i in range(1000)]
    html = "".join(f'<audio src="{url}"></audio>' for url in [*urls, *urls])
    # A URL's distinct media type and detection source remain separate identities.
    html += f'<video src="{urls[0]}"></video>'
    html += (
        "<script>mediaResource "
        + " ".join('"audioURL":' + json.dumps(url) for url in [*urls, *urls])
        + "</script>"
    )
    result = module.extract_metadata_from_html(html=html, base_url="https://example.com/")
    assert [(c.url, c.candidate_type, c.detection_source) for c in result.media_candidates] == [
        *((url, "audio", "html_audio") for url in urls),
        (urls[0], "video", "html_video"),
        *((url, "audio", "ard_media_object") for url in urls),
    ]
    assert len(comparisons) <= 3 * len(urls)
