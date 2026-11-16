import pytest

from archive.classification import MediaCandidate, classify_item, resolve_media_sources_for_item
from archive.models import Item, ItemKind


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_video_selection_skips_ineligible_first_candidate(reverse, direct):
    weak = MediaCandidate("https://example.test/decoration.mp4", "video", "html_video")
    usable = MediaCandidate("https://www.youtube.com/watch?v=synthetic", "page_video", "og_video")
    candidates = [weak, usable]
    original = "https://example.test/story"
    if direct:
        original = "https://example.test/talk.mp4"
        usable = MediaCandidate(original, "video", "original_url")
        candidates = [weak, usable]
    if reverse:
        candidates.reverse()
    decision = classify_item(original_url=original, metadata_candidates=candidates)
    expected = original if direct else usable.url
    assert decision.kind == ItemKind.VIDEO
    assert decision.evidence["selected_media"]["video"] == expected
    assert (
        resolve_media_sources_for_item(
            Item(
                original_url=original,
                kind=ItemKind.ARTICLE,
                classification_evidence=decision.evidence,
            )
        )[1]
        == expected
    )


def test_weak_video_alone_does_not_promote_but_explicit_video_can_use_it():
    weak = MediaCandidate("https://example.test/decoration.mp4", "video", "html_video")
    decision = classify_item(original_url="https://example.test/story", metadata_candidates=[weak])
    assert decision.kind == ItemKind.LINK
    assert decision.evidence["selected_media"]["video"] == ""
    explicit = classify_item(
        original_url="https://example.test/story",
        explicit_kind=ItemKind.VIDEO,
        metadata_candidates=[weak],
    )
    assert explicit.evidence["selected_media"]["video"] == weak.url


def test_explicit_suffixless_audio_is_selected_but_plain_discovery_is_not():
    url = "https://example.test/download?id=123"
    explicit = classify_item(original_url="https://example.test/story", audio_url=url)
    assert explicit.kind == ItemKind.PODCAST_EPISODE
    assert explicit.evidence["selected_media"]["audio"] == url
    discovered = classify_item(original_url=url)
    assert discovered.kind == ItemKind.LINK
    assert discovered.evidence["selected_media"]["audio"] == ""


@pytest.mark.parametrize(
    "url", ["file:///tmp/audio.mp3", "javascript:audio.mp3", "https:///audio.mp3"]
)
def test_explicit_audio_refuses_non_http_sources(url):
    decision = classify_item(original_url="https://example.test/story", audio_url=url)
    assert decision.evidence["selected_media"]["audio"] == ""
    assert decision.kind == ItemKind.LINK


@pytest.mark.parametrize("changed_field", ["original_url", "audio_url", "media_url"])
def test_cached_discoveries_are_bound_to_all_current_sources(changed_field):
    inputs = dict(original_url="https://example.test/a", audio_url="", media_url="")
    old = classify_item(
        **inputs,
        metadata_candidates=[MediaCandidate("https://example.test/old.mp3", "audio", "html_audio")],
        metadata_signals={"kind_hint": "old"},
    )
    assert classify_item(**inputs, existing_evidence=old.evidence).evidence == old.evidence
    inputs[changed_field] = "https://example.test/new.mp3"
    current = classify_item(**inputs, existing_evidence=old.evidence)
    assert current.evidence["selected_media"]["audio"] == inputs[changed_field]
    assert current.evidence["metadata_signals"] == {}
    assert all(
        c["url"] != "https://example.test/old.mp3" for c in current.evidence["media_candidates"]
    )
    assert (
        resolve_media_sources_for_item(Item(**inputs, classification_evidence=old.evidence))[0]
        == inputs[changed_field]
    )


def test_legacy_unbound_cache_does_not_invent_source_identity():
    decision = classify_item(
        original_url="https://example.test/current",
        existing_evidence={
            "media_candidates": [
                MediaCandidate("https://example.test/old.mp3", "audio", "html_audio").as_dict()
            ],
            "metadata_signals": {"kind_hint": "old"},
        },
    )
    assert decision.kind == ItemKind.LINK
    assert decision.evidence["media_candidates"] == []
    assert decision.evidence["metadata_signals"] == {}


@pytest.mark.parametrize(
    "base,path,kind",
    [
        ("youtube.com", "/watch?v=synthetic", ItemKind.VIDEO),
        ("youtu.be", "/synthetic", ItemKind.VIDEO),
        ("vimeo.com", "/123", ItemKind.VIDEO),
        ("castro.fm", "/episode/synthetic", ItemKind.PODCAST_EPISODE),
    ],
)
@pytest.mark.parametrize("scheme,port", [("https", "443"), ("http", "80")])
def test_adapter_identity_accepts_case_and_default_port(base, path, kind, scheme, port):
    plain = classify_item(original_url=f"{scheme}://{base}{path}")
    equivalent = classify_item(original_url=f"{scheme}://{base.upper()}:{port}{path}")
    assert equivalent.kind == plain.kind == kind
    assert equivalent.rule == plain.rule
    assert bool(equivalent.evidence["selected_media"]["video"]) == bool(
        plain.evidence["selected_media"]["video"]
    )


@pytest.mark.parametrize(
    "authority",
    [
        "user@youtube.com",
        "user:password@youtube.com",
        "youtube.com:8443",
        "youtube.com.attacker.test",
        "youtube.com.",
    ],
)
def test_adapter_identity_refuses_non_equivalent_authorities(authority):
    decision = classify_item(original_url=f"https://{authority}/watch?v=synthetic")
    assert decision.kind == ItemKind.LINK
    assert decision.evidence["source_adapter"] == ""
    assert decision.evidence["selected_media"]["video"] == ""


@pytest.mark.parametrize(
    "bad",
    [
        {"url": None},
        {"url": []},
        {"url": {}},
        {"url": 123},
        {"url": "https://example.test/" + "x" * 8192},
        {"url": "file:///tmp/audio.mp3"},
        {"url": "relative.mp3"},
        {"url": "https://example.test:99999/audio.mp3"},
        {"candidate_type": "invented"},
        {"candidate_type": ["audio"]},
        {"detection_source": "invented"},
        {"detection_source": {"audio": True}},
        {"detection_source": "html_video"},
        {"detection_source": "original_url"},
        {"candidate_type": "page_video", "detection_source": "og_video"},
    ],
)
def test_stored_candidates_skip_corrupt_fields_and_keep_valid_later_evidence(bad):
    original = "https://example.test/story"
    corrupt = MediaCandidate("https://example.test/corrupt.mp3", "audio", "html_audio").as_dict()
    corrupt.update(bad)
    healthy = MediaCandidate("https://example.test/valid", "audio", "html_audio")
    decision = classify_item(
        original_url=original,
        existing_evidence={
            "original_url": original,
            "audio_url": "",
            "media_url": "",
            "media_candidates": [corrupt, healthy.as_dict()],
        },
    )
    assert decision.evidence["media_candidates"] == [healthy.as_dict()]
    assert decision.evidence["selected_media"]["audio"] == healthy.url


@pytest.mark.parametrize(
    "source",
    [
        "html_audio",
        "og_audio",
        "twitter_player_stream",
        "json_ld_audio",
        "ard_media_object",
    ],
)
def test_stored_typed_audio_can_be_suffixless(source):
    original = "https://example.test/story"
    candidate = MediaCandidate("https://example.test/download?id=123", "audio", source)
    decision = classify_item(
        original_url=original,
        existing_evidence={
            "original_url": original,
            "audio_url": "",
            "media_url": "",
            "media_candidates": [candidate.as_dict()],
        },
    )
    assert decision.evidence["selected_media"]["audio"] == candidate.url
