"""Synthetic remote-script decoding and resource-bound regressions."""

import json
import sys

import pytest

from archive import metadata as module


def extract(*blobs):
    html = '<meta name="author" content="Independent author">' + "".join(
        f'<script type="application/ld+json">{blob}</script>' for blob in blobs
    )
    return module.extract_metadata_from_html(html=html, base_url="https://example.com/page")


@pytest.mark.parametrize("wrapper", ["array", "graph"])
@pytest.mark.parametrize("depth", [80, 1500])
def test_jsonld_deep_blob_does_not_hide_independent_metadata(wrapper, depth):
    blob = '{"headline":"Refused"}'
    for _ in range(depth):
        blob = f"[{blob}]" if wrapper == "array" else '{"@graph":' + blob + "}"
    result = extract(blob, '{"headline":"Valid"}')
    assert result.title == "Valid"
    assert result.author == "Independent author"


def test_jsonld_huge_integer_does_not_hide_independent_metadata():
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        result = extract('{"number":' + "9" * 5000 + "}", '{"headline":"Valid"}')
    finally:
        sys.set_int_max_str_digits(previous)
    assert result.title == "Valid"


@pytest.mark.parametrize("error", [ValueError, RecursionError, OverflowError])
def test_jsonld_decoder_resource_errors_are_local_to_blob(monkeypatch, error):
    loads = json.loads

    def decode(blob):
        if blob == "refuse":
            raise error("synthetic decoder refusal")
        return loads(blob)

    monkeypatch.setattr(module.json, "loads", decode)
    assert extract("refuse", '{"headline":"Valid"}').title == "Valid"


def test_jsonld_node_limit_refuses_whole_blob():
    blob = json.dumps([{"headline": "Refused"}] + [None] * 10000)
    assert extract(blob, '{"headline":"Valid"}').title == "Valid"


def test_jsonld_depth_bound_includes_nonrecord_fields():
    nested = "[" * 80 + '"author"' + "]" * 80
    assert (
        extract('{"headline":"Refused","author":' + nested + "}", '{"headline":"Valid"}').title
        == "Valid"
    )


def test_jsonld_walk_preserves_preorder_and_exact_bounds(monkeypatch):
    monkeypatch.setattr(module, "MAX_JSONLD_DEPTH", 3)
    monkeypatch.setattr(module, "MAX_JSONLD_NODES", 5)
    blob = '[{"@graph":[{}]},{}]'
    assert module._iter_jsonld_records(blob) == [{"@graph": [{}]}, {}, {}]
    monkeypatch.setattr(module, "MAX_JSONLD_NODES", 4)
    assert module._iter_jsonld_records(blob) == []
    monkeypatch.setattr(module, "MAX_JSONLD_NODES", 5)
    monkeypatch.setattr(module, "MAX_JSONLD_DEPTH", 1)
    assert module._iter_jsonld_records(blob) == []


@pytest.mark.parametrize(
    "encoded,expected",
    [
        (
            r'"https:\/\/cdn.example.com\/a.mp3?x=1\u0026y=2"',
            "https://cdn.example.com/a.mp3?x=1&y=2",
        ),
        (r'"//cdn.example.com/a.mp3?q=\"quoted\""', 'https://cdn.example.com/a.mp3?q="quoted"'),
        (r'"https://cdn.example.com/a.mp3?q=\\u0026"', r"https://cdn.example.com/a.mp3?q=\u0026"),
        (r'"https://cdn.example.com/\q.mp3"', ""),
        ('"https://cdn.example.com/a\n.mp3"', ""),
        (r'"https://cdn.example.com/a\ud800.mp3"', ""),
        (r'"https://cdn.example.com/a\udfff.mp3"', ""),
        (r'"https://cdn.example.com/a\ud83d\ude00.mp3"', "https://cdn.example.com/a😀.mp3"),
        (r'"javascript:alert(1)"', ""),
        (r'"https://cdn.example.com/unterminated', ""),
    ],
)
def test_ard_audio_json_strings_are_decoded_once(encoded, expected):
    blob = '{"mediaResource":{"audioURL":' + encoded + "}}"
    result = module.extract_metadata_from_html(
        html=f"<script>{blob}</script>", base_url="https://example.com/page"
    )
    assert [candidate.url for candidate in result.media_candidates] == (
        [expected] if expected else []
    )
