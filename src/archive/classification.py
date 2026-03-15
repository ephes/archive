from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from django.utils import timezone

from archive.metadata import AUDIO_SUFFIXES, VIDEO_SUFFIXES
from archive.models import Item, ItemKind, PodcastFeedPolicy

VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}
YOUTUBE_PAGE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
OVERRIDE_CLASSIFICATION_RULES = {"explicit_kind", "operator_override"}
CURRENT_CLASSIFICATION_ENGINE_VERSION = 2


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    candidate_type: str
    detection_source: str

    def as_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "candidate_type": self.candidate_type,
            "detection_source": self.detection_source,
        }


@dataclass(frozen=True)
class ClassificationDecision:
    kind: str
    rule: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class PodcastFeedDecision:
    eligible: bool
    reason: str
    enclosure_source: str = ""


def infer_kind(url: str, explicit_kind: str = "", audio_url: str = "") -> str:
    decision = classify_item(
        original_url=url,
        explicit_kind=explicit_kind,
        audio_url=audio_url,
    )
    return decision.kind


def classify_item(
    *,
    original_url: str,
    current_kind: str = "",
    explicit_kind: str = "",
    audio_url: str = "",
    media_url: str = "",
    kind_hint: str = "",
    metadata_candidates: Sequence[MediaCandidate] = (),
    existing_rule: str = "",
    existing_evidence: dict[str, Any] | None = None,
    preserve_override: bool = True,
) -> ClassificationDecision:
    media_candidates = build_media_candidates(
        original_url=original_url,
        audio_url=audio_url,
        media_url=media_url,
        metadata_candidates=metadata_candidates,
        existing_evidence=existing_evidence,
    )
    selected_audio = select_audio_archive_source_url_from_candidates(media_candidates)
    selected_video = select_video_archive_source_url_from_candidates(media_candidates)
    adapter_name, adapter_kind = _match_source_adapter(original_url)

    if (
        preserve_override
        and existing_rule in OVERRIDE_CLASSIFICATION_RULES
        and current_kind in ItemKind.values
    ):
        kind = current_kind
        rule = existing_rule
    elif explicit_kind in ItemKind.values:
        kind = explicit_kind
        rule = "explicit_kind"
    elif adapter_kind:
        kind = adapter_kind
        rule = adapter_name
    elif kind_hint in ItemKind.values:
        kind = kind_hint
        rule = "metadata_kind_hint"
    else:
        generic_kind, generic_rule = _generic_url_rule(
            original_url=original_url,
            audio_url=audio_url,
        )
        if generic_kind != str(ItemKind.LINK):
            kind = generic_kind
            rule = generic_rule
        elif selected_video:
            kind = str(ItemKind.VIDEO)
            rule = "media_candidate_video"
        elif selected_audio:
            kind = str(ItemKind.PODCAST_EPISODE)
            rule = "media_candidate_audio"
        else:
            kind = str(ItemKind.LINK)
            rule = "default_link"

    return ClassificationDecision(
        kind=kind,
        rule=rule,
        evidence=_build_evidence(
            original_url=original_url,
            audio_url=audio_url,
            media_url=media_url,
            kind_hint=kind_hint,
            matched_adapter=adapter_name,
            media_candidates=media_candidates,
            selected_audio=selected_audio,
            selected_video=selected_video,
            existing_evidence=existing_evidence,
        ),
    )


def build_media_candidates(
    *,
    original_url: str,
    audio_url: str = "",
    media_url: str = "",
    metadata_candidates: Sequence[MediaCandidate] = (),
    existing_evidence: dict[str, Any] | None = None,
) -> list[MediaCandidate]:
    candidates: list[MediaCandidate] = []
    seen: set[tuple[str, str]] = set()

    def add_candidate(candidate: MediaCandidate) -> None:
        key = (candidate.url, candidate.candidate_type)
        if not candidate.url or key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    explicit_audio_url = audio_url.strip()
    if explicit_audio_url:
        candidate_type = "audio" if _looks_like_audio_url(explicit_audio_url) else ""
        if not candidate_type and _looks_like_ambiguous_audio_url(explicit_audio_url):
            candidate_type = "audio"
        if candidate_type:
            add_candidate(
                MediaCandidate(
                    url=explicit_audio_url,
                    candidate_type=candidate_type,
                    detection_source="explicit_audio_url",
                )
            )

    for candidate in metadata_candidates:
        add_candidate(candidate)

    for candidate in _media_candidates_from_evidence(existing_evidence):
        add_candidate(candidate)

    for url, detection_source in (
        (media_url.strip(), "media_url"),
        (original_url.strip(), "original_url"),
    ):
        if not url:
            continue
        candidate_type = _candidate_type_for_url(url)
        if candidate_type:
            add_candidate(
                MediaCandidate(
                    url=url,
                    candidate_type=candidate_type,
                    detection_source=detection_source,
                )
            )

    return candidates


def resolve_media_sources_for_item(item: Item) -> tuple[str | None, str | None]:
    candidates = build_media_candidates(
        original_url=item.original_url,
        audio_url=item.audio_url,
        media_url=item.media_url,
        existing_evidence=item.classification_evidence,
    )
    return (
        select_audio_archive_source_url_from_candidates(candidates),
        select_video_archive_source_url_from_candidates(candidates),
    )


def selected_media_from_evidence(evidence: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(evidence, dict):
        return {"audio": "", "video": ""}

    raw_selected = evidence.get("selected_media")
    if not isinstance(raw_selected, dict):
        return {"audio": "", "video": ""}

    return {
        "audio": str(raw_selected.get("audio", "")).strip(),
        "video": str(raw_selected.get("video", "")).strip(),
    }


def classification_is_stale(item: Item) -> bool:
    return item.classification_engine_version < CURRENT_CLASSIFICATION_ENGINE_VERSION


def normalized_classification_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    return {
        key: value
        for key, value in evidence.items()
        if key != "classified_at"
    }


def select_audio_archive_source_url_from_candidates(
    candidates: Iterable[MediaCandidate],
) -> str | None:
    for candidate in candidates:
        if candidate.candidate_type == "audio":
            return candidate.url
    return None


def select_video_archive_source_url_from_candidates(
    candidates: Iterable[MediaCandidate],
) -> str | None:
    for candidate in candidates:
        if candidate.candidate_type in {"video", "page_video"}:
            return candidate.url
    return None


def podcast_feed_decision_for_item(item: Item) -> PodcastFeedDecision:
    policy = item.podcast_feed_policy or PodcastFeedPolicy.AUTO
    enclosure_source = podcast_enclosure_source_for_item(item)
    if policy == PodcastFeedPolicy.EXCLUDE:
        return PodcastFeedDecision(eligible=False, reason="operator_exclude")
    if not item.is_public:
        return PodcastFeedDecision(eligible=False, reason="not_public")
    if item.published_at is None:
        return PodcastFeedDecision(eligible=False, reason="not_published")
    if not item.title.strip():
        return PodcastFeedDecision(eligible=False, reason="missing_title")
    if not item.short_summary.strip():
        return PodcastFeedDecision(eligible=False, reason="missing_summary")
    if not enclosure_source:
        return PodcastFeedDecision(eligible=False, reason="missing_audio")
    if policy == PodcastFeedPolicy.INCLUDE:
        return PodcastFeedDecision(
            eligible=True,
            reason="operator_include",
            enclosure_source=enclosure_source,
        )
    if enclosure_source == "archived_audio":
        return PodcastFeedDecision(
            eligible=True,
            reason="archived_audio_auto",
            enclosure_source=enclosure_source,
        )
    if enclosure_source == "generated_article_audio" and _article_audio_is_feed_worthy(item):
        return PodcastFeedDecision(
            eligible=True,
            reason="generated_article_audio_auto",
            enclosure_source=enclosure_source,
        )
    return PodcastFeedDecision(
        eligible=False,
        reason="generated_article_audio_not_substantial",
        enclosure_source=enclosure_source,
    )


def podcast_enclosure_source_for_item(item: Item) -> str:
    if item.has_archived_audio:
        return "archived_audio"
    if item.kind == ItemKind.ARTICLE and item.has_generated_article_audio:
        return "generated_article_audio"
    return ""


def _article_audio_is_feed_worthy(item: Item) -> bool:
    if item.kind != ItemKind.ARTICLE or not item.has_generated_article_audio:
        return False

    body = item.long_summary.strip() or item.short_summary.strip()
    if not body:
        return False

    word_count = len(re.findall(r"\w+", body))
    sentence_count = len(re.findall(r"[.!?]", body))
    nonempty_lines = [line.strip() for line in body.splitlines() if line.strip()]
    list_like_lines = sum(
        1
        for line in nonempty_lines
        if line.startswith(("-", "*", "•"))
        or line[:3].isdigit()
        or line.count(":") >= 2
    )
    url_count = body.count("http://") + body.count("https://")
    separator_count = body.count(" / ") + body.count(" | ") + body.count(" - ")

    substantial = word_count >= 45 or len(body) >= 280
    coherent = sentence_count >= 3
    mixed_topic = url_count >= 2 or list_like_lines >= 3 or separator_count >= 4
    return substantial and coherent and not mixed_topic


def _generic_url_rule(*, original_url: str, audio_url: str) -> tuple[str, str]:
    if audio_url.strip():
        return str(ItemKind.PODCAST_EPISODE), "audio_url_signal"

    parsed = urlparse(original_url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if host in VIDEO_HOSTS:
        return str(ItemKind.VIDEO), "video_host"
    if path.endswith(AUDIO_SUFFIXES):
        return str(ItemKind.PODCAST_EPISODE), "audio_suffix"
    return str(ItemKind.LINK), "default_link"


def _match_source_adapter(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if host in {"castro.fm", "www.castro.fm"} and path.startswith("/episode/"):
        episode_id = path.removeprefix("/episode/").strip("/")
        if episode_id:
            return "adapter_castro_episode", str(ItemKind.PODCAST_EPISODE)
    if _looks_like_supported_video_page_url(url):
        return "adapter_supported_video_page", str(ItemKind.VIDEO)
    return "", ""


def _looks_like_audio_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(AUDIO_SUFFIXES)


def _looks_like_ambiguous_audio_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".webm")


def _looks_like_direct_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(VIDEO_SUFFIXES)


def _looks_like_supported_video_page_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_PAGE_HOSTS:
        return False
    if _looks_like_direct_video_url(url):
        return False
    if host == "youtu.be":
        return bool(parsed.path.strip("/"))
    if parsed.path == "/watch":
        return bool(parse_qs(parsed.query).get("v"))
    return parsed.path.startswith(("/embed/", "/live/", "/shorts/"))


def _candidate_type_for_url(url: str) -> str:
    if _looks_like_audio_url(url):
        return "audio"
    if _looks_like_direct_video_url(url):
        return "video"
    if _looks_like_supported_video_page_url(url):
        return "page_video"
    return ""


def _media_candidates_from_evidence(
    evidence: dict[str, Any] | None,
) -> list[MediaCandidate]:
    if not isinstance(evidence, dict):
        return []

    values = evidence.get("media_candidates")
    if not isinstance(values, list):
        return []

    candidates: list[MediaCandidate] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        url = str(value.get("url", "")).strip()
        candidate_type = str(value.get("candidate_type", "")).strip()
        detection_source = str(value.get("detection_source", "")).strip()
        if not url or not candidate_type or not detection_source:
            continue
        candidates.append(
            MediaCandidate(
                url=url,
                candidate_type=candidate_type,
                detection_source=detection_source,
            )
        )
    return candidates


def _build_evidence(
    *,
    original_url: str,
    audio_url: str,
    media_url: str,
    kind_hint: str,
    matched_adapter: str,
    media_candidates: Sequence[MediaCandidate],
    selected_audio: str | None,
    selected_video: str | None,
    existing_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata_signals: dict[str, Any] = {}
    if isinstance(existing_evidence, dict):
        raw_signals = existing_evidence.get("metadata_signals")
        if isinstance(raw_signals, dict):
            metadata_signals = dict(raw_signals)
    evidence = {
        "source_adapter": matched_adapter,
        "original_url": original_url,
        "audio_url": audio_url,
        "media_url": media_url,
        "kind_hint": kind_hint,
        "metadata_signals": metadata_signals,
        "media_candidates": [candidate.as_dict() for candidate in media_candidates],
        "selected_media": {
            "audio": selected_audio or "",
            "video": selected_video or "",
        },
    }
    evidence["classified_at"] = _classified_at_value(
        evidence=evidence,
        existing_evidence=existing_evidence,
    )
    return evidence


def _classified_at_value(
    *,
    evidence: dict[str, Any],
    existing_evidence: dict[str, Any] | None,
) -> str:
    if not isinstance(existing_evidence, dict):
        return timezone.now().isoformat()

    existing_classified_at = existing_evidence.get("classified_at")
    if (
        isinstance(existing_classified_at, str)
        and normalized_classification_evidence(existing_evidence) == evidence
    ):
        return existing_classified_at

    return timezone.now().isoformat()
