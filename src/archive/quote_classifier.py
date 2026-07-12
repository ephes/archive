from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from archive.classification import CURRENT_CLASSIFICATION_ENGINE_VERSION
from archive.models import Item, ItemKind
from archive.services import normalize_item_kind_dependent_statuses
from archive.summaries import (
    MAX_SUMMARY_INPUT_CHARS,
    SummaryGenerationError,
    SummarySource,
    extract_summary_source_from_url,
)

logger = logging.getLogger(__name__)

CURRENT_QUOTE_CLASSIFIER_VERSION = 1
QUOTE_CONFIDENCE_THRESHOLD = 0.8
MAX_QUOTE_SOURCE_INPUT_CHARS = 6_000

SourceFetcher = Callable[[str, int], SummarySource]
Classifier = Callable[[str, int], str]


class QuoteClassificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuoteClassifierDecision:
    is_quote: bool
    confidence: float
    rationale: str
    reusable_text: str = ""

    @property
    def high_confidence_yes(self) -> bool:
        return self.is_quote and self.confidence >= QUOTE_CONFIDENCE_THRESHOLD


@dataclass(frozen=True)
class QuoteClassificationResult:
    item_id: int | None
    decision: QuoteClassifierDecision
    evidence: dict[str, Any]
    update_fields: tuple[str, ...]
    dry_run: bool

    @property
    def applied_kind_update(self) -> bool:
        return "kind" in self.update_fields


def quote_classifier_processed(item: Item) -> bool:
    evidence = item.classification_evidence
    if not isinstance(evidence, dict):
        return False
    quote_evidence = evidence.get("quote_classifier")
    if not isinstance(quote_evidence, dict):
        return False
    return quote_evidence.get("version") == CURRENT_QUOTE_CLASSIFIER_VERSION


def classify_quote_item(
    *,
    item: Item,
    timeout: int = 30,
    dry_run: bool = False,
    source_fetcher: SourceFetcher | None = None,
    classifier: Classifier | None = None,
) -> QuoteClassificationResult:
    api_key = settings.ARCHIVE_SUMMARY_API_KEY.strip()
    if classifier is None and not api_key:
        raise QuoteClassificationError("Quote classification is not configured")

    source = _best_effort_quote_source(
        item=item,
        timeout=timeout,
        source_fetcher=source_fetcher or extract_summary_source_from_url,
    )
    prompt = _build_quote_classifier_prompt(item=item, source=source)
    raw_response = (classifier or _request_quote_classification)(prompt, timeout)
    decision = _parse_quote_classifier_response(raw_response)
    evidence = _quote_classifier_evidence(decision=decision, prompt=prompt)
    update_fields: list[str] = []

    if not dry_run:
        existing_evidence = (
            item.classification_evidence
            if isinstance(item.classification_evidence, dict)
            else {}
        )
        next_evidence = {**existing_evidence, "quote_classifier": evidence}
        if item.classification_evidence != next_evidence:
            item.classification_evidence = next_evidence
            update_fields.append("classification_evidence")

        if decision.high_confidence_yes:
            if item.kind != ItemKind.QUOTE:
                item.kind = ItemKind.QUOTE
                update_fields.append("kind")
            if item.classification_rule != "quote_classifier":
                item.classification_rule = "quote_classifier"
                update_fields.append("classification_rule")
            if item.classification_engine_version != CURRENT_CLASSIFICATION_ENGINE_VERSION:
                item.classification_engine_version = CURRENT_CLASSIFICATION_ENGINE_VERSION
                update_fields.append("classification_engine_version")
            normalize_item_kind_dependent_statuses(item=item, update_fields=update_fields)

        if update_fields:
            item.save(update_fields=sorted(set(update_fields)))

    return QuoteClassificationResult(
        item_id=item.pk,
        decision=decision,
        evidence=evidence,
        update_fields=tuple(sorted(set(update_fields))),
        dry_run=dry_run,
    )


def _best_effort_quote_source(
    *,
    item: Item,
    timeout: int,
    source_fetcher: SourceFetcher,
) -> SummarySource:
    try:
        return source_fetcher(item.original_url, timeout)
    except SummaryGenerationError as exc:
        logger.warning("Quote classifier source extraction failed for item %s: %s", item.pk, exc)
        return SummarySource()


def _build_quote_classifier_prompt(item: Item, source: SummarySource) -> str:
    summary = "\n".join(
        part
        for part in (
            item.short_summary.strip(),
            item.long_summary.strip(),
        )
        if part
    )
    source_excerpt = _truncate_text(
        "\n\n".join(
            part
            for part in (
                source.meta_description.strip(),
                source.extracted_text.strip(),
            )
            if part
        ),
        max_chars=MAX_QUOTE_SOURCE_INPUT_CHARS,
    )
    sections = [
        "Classify whether this archive item is a reusable weeknote opening quote.",
        "Return JSON only with keys is_quote, confidence, rationale, and reusable_text.",
        (
            "is_quote must be true only when the saved item itself is primarily a "
            "standalone quote capture: a concise quoted passage, aphorism, joke, "
            "social post, or excerpt that can be reused as a weeknote opener."
        ),
        (
            "Return false for ordinary articles, videos, podcast episodes, bookmarks, "
            "project homepages, product pages, recommendations, marketing copy, "
            "taglines, and mixed notes even if they contain a quotable sentence or "
            "slogan somewhere."
        ),
        (
            "confidence must be a number from 0 to 1. Use high confidence only when "
            "the reusable quote nature is clear from the supplied fields or page source."
        ),
        (
            "reusable_text should contain the likely quote text when is_quote is true, "
            "otherwise an empty string."
        ),
        "Avoid markdown and explanations outside the JSON object.",
        "",
        f"Original URL: {item.original_url}",
        f"Title: {item.title or '(missing)'}",
        f"Source: {item.source or '(missing)'}",
        f"Author: {item.author or '(missing)'}",
        f"Kind: {item.kind}",
        f"Notes: {item.notes or '(missing)'}",
        f"Summary: {summary or '(missing)'}",
        f"Tags: {', '.join(item.tag_list) if item.tag_list else '(missing)'}",
        "",
        "Best-effort original page source:",
        source_excerpt or "(missing)",
    ]
    return "\n".join(sections)


def _request_quote_classification(prompt: str, timeout: int) -> str:
    base_url = settings.ARCHIVE_SUMMARY_API_BASE.rstrip("/")
    request_body = json.dumps(
        {
            "model": settings.ARCHIVE_SUMMARY_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You classify saved archive items for quote reuse. "
                        "Always return valid JSON and only JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
    ).encode("utf-8")
    request = Request(
        f"{base_url}/chat/completions",
        data=request_body,
        headers={
            "Authorization": f"Bearer {settings.ARCHIVE_SUMMARY_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise QuoteClassificationError(
            f"Quote classifier API request failed: HTTP {exc.code}: {message}"
        ) from exc
    except URLError as exc:
        raise QuoteClassificationError(
            f"Quote classifier API request failed: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise QuoteClassificationError(f"Quote classifier API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QuoteClassificationError("Quote classifier API returned invalid JSON") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise QuoteClassificationError(
            "Quote classifier API response did not include content"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise QuoteClassificationError("Quote classifier API response was empty")
    return content


def _parse_quote_classifier_response(raw_content: str) -> QuoteClassifierDecision:
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise QuoteClassificationError("Quote classifier response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise QuoteClassificationError("Quote classifier response must be a JSON object")

    is_quote = _parse_bool(payload.get("is_quote", payload.get("quote", False)))
    confidence = _parse_confidence(payload.get("confidence", 0))
    rationale = _required_normalized_string(
        payload.get("rationale", payload.get("reason", "")),
        "rationale",
    )
    reusable_text = _optional_normalized_string(
        payload.get("reusable_text", payload.get("quote_text", "")),
        "reusable_text",
    )

    return QuoteClassifierDecision(
        is_quote=is_quote,
        confidence=confidence,
        rationale=rationale,
        reusable_text=reusable_text,
    )


def _quote_classifier_evidence(
    *,
    decision: QuoteClassifierDecision,
    prompt: str,
) -> dict[str, Any]:
    applied = decision.high_confidence_yes
    return {
        "version": CURRENT_QUOTE_CLASSIFIER_VERSION,
        "processed_at": timezone.now().isoformat(),
        "decision": "yes" if decision.is_quote else "no",
        "applied": applied,
        "confidence": decision.confidence,
        "confidence_threshold": QUOTE_CONFIDENCE_THRESHOLD,
        "rationale": decision.rationale,
        "reusable_text": decision.reusable_text,
        "input_chars": len(prompt),
        "model": settings.ARCHIVE_SUMMARY_MODEL,
    }


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes"}:
            return True
        if lowered in {"false", "no"}:
            return False
    raise QuoteClassificationError("Quote classifier response field is_quote must be a boolean")


def _parse_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise QuoteClassificationError(
            "Quote classifier response field confidence must be a number"
        )
    confidence = float(value)
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise QuoteClassificationError(
            "Quote classifier response field confidence must be between 0 and 1"
        )
    return confidence


def _required_normalized_string(value: object, field: str) -> str:
    text = _optional_normalized_string(value, field)
    if not text:
        raise QuoteClassificationError(f"Quote classifier response did not include {field}")
    return text


def _optional_normalized_string(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise QuoteClassificationError(f"Quote classifier response field {field} must be a string")
    return _normalize_text(value)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _truncate_text(value: str, max_chars: int = MAX_SUMMARY_INPUT_CHARS) -> str:
    normalized = value.strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rsplit(" ", 1)[0].strip()
