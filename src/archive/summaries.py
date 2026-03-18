from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from archive.metadata import REQUEST_HEADERS
from archive.models import Item

logger = logging.getLogger(__name__)

MAX_SUMMARY_SOURCE_BYTES = 1024 * 1024
MAX_SUMMARY_INPUT_CHARS = 12_000
MAX_ARTICLE_AUDIO_SOURCE_CHARS = 100_000
MAX_TRANSCRIPT_INPUT_CHARS = 8_000
MAX_SOURCE_WITH_TRANSCRIPT_INPUT_CHARS = 4_000


class SummaryGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedSummary:
    short_summary: str
    long_summary: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class SummarySource:
    meta_description: str = ""
    extracted_text: str = ""


class _SummaryHTMLParser(HTMLParser):
    _IGNORED_TAGS = {
        "footer",
        "form",
        "head",
        "nav",
        "noscript",
        "script",
        "style",
        "svg",
    }
    _TEXT_TAGS = {"h1", "h2", "h3", "h4", "li", "p", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.meta_description = ""
        self.text_chunks: list[str] = []
        self._ignored_depth = 0
        self._current_tag: str | None = None
        self._current_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return

        if lowered == "meta":
            attr_map = {
                key.lower(): value
                for key, value in attrs
                if key is not None and value is not None and value != ""
            }
            meta_key = (attr_map.get("name") or attr_map.get("property") or "").strip().lower()
            if not self.meta_description and meta_key in {
                "description",
                "og:description",
                "twitter:description",
            }:
                self.meta_description = _normalize_text(attr_map.get("content", ""))
            return

        if self._ignored_depth == 0 and lowered in self._TEXT_TAGS:
            self._current_tag = lowered
            self._current_parts = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return

        if self._ignored_depth == 0 and lowered == self._current_tag:
            text = _normalize_text(" ".join(self._current_parts))
            if len(text) >= 25:
                self.text_chunks.append(text)
            self._current_tag = None
            self._current_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and self._current_tag is not None:
            self._current_parts.append(data)


def generate_item_summaries(item: Item, timeout: int = 60) -> GeneratedSummary:
    api_key = settings.ARCHIVE_SUMMARY_API_KEY.strip()
    if not api_key:
        raise SummaryGenerationError("Summary generation is not configured")

    source = _best_effort_summary_source(item=item, timeout=timeout)
    prompt = _build_summary_prompt(item=item, source=source)
    response = _request_summary(prompt=prompt, timeout=timeout)
    return _parse_generated_summary(response)


def _best_effort_summary_source(item: Item, timeout: int) -> SummarySource:
    try:
        return extract_summary_source_from_url(item.original_url, timeout=timeout)
    except SummaryGenerationError as exc:
        logger.warning("Summary source extraction failed for item %s: %s", item.pk, exc)
        return SummarySource()


def extract_summary_source_from_url(
    url: str,
    timeout: int = 60,
    *,
    max_chars: int = MAX_SUMMARY_INPUT_CHARS,
) -> SummarySource:
    request = Request(url, headers=REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read(MAX_SUMMARY_SOURCE_BYTES + 1)
    except HTTPError as exc:
        raise SummaryGenerationError(f"HTTP {exc.code} while fetching summary source") from exc
    except URLError as exc:
        raise SummaryGenerationError(f"Could not fetch summary source: {exc.reason}") from exc
    except OSError as exc:
        raise SummaryGenerationError(f"Could not fetch summary source: {exc}") from exc

    if len(payload) > MAX_SUMMARY_SOURCE_BYTES:
        raise SummaryGenerationError("Summary source exceeded 1 MiB limit")

    text = payload.decode(charset, errors="replace")
    if content_type.startswith("text/plain"):
        return SummarySource(extracted_text=_truncate_text(text, max_chars=max_chars))
    if "html" in content_type or "xml" in content_type:
        return extract_summary_source_from_html(text, max_chars=max_chars)
    return SummarySource()


def extract_summary_source_from_html(
    html: str,
    *,
    max_chars: int = MAX_SUMMARY_INPUT_CHARS,
) -> SummarySource:
    parser = _SummaryHTMLParser()
    parser.feed(html)
    parser.close()
    combined = _truncate_text("\n\n".join(parser.text_chunks), max_chars=max_chars)
    return SummarySource(
        meta_description=parser.meta_description,
        extracted_text=combined,
    )


def _build_summary_prompt(item: Item, source: SummarySource) -> str:
    transcript_excerpt = _truncate_text(item.transcript, max_chars=MAX_TRANSCRIPT_INPUT_CHARS)
    source_excerpt = _truncate_text(
        source.extracted_text,
        max_chars=(
            MAX_SOURCE_WITH_TRANSCRIPT_INPUT_CHARS
            if transcript_excerpt
            else MAX_SUMMARY_INPUT_CHARS
        ),
    )
    sections = [
        "Generate archive metadata for this captured item.",
        "Return JSON with keys short_summary, long_summary, and tags.",
        "short_summary must be 1 or 2 sentences and under 240 characters.",
        "long_summary must be 2 to 4 sentences.",
        "tags must be a JSON array of 3 to 8 short lowercase tags.",
        "Write summaries and tags in the same language as the transcript when present.",
        (
            "If there is no transcript, use the dominant language of the page description and "
            "extracted source text. Do not translate non-English content into English."
        ),
        "Avoid markdown, bullets, and filler.",
        "",
        f"Original URL: {item.original_url}",
        f"Kind: {item.kind}",
        f"Current title: {item.title or '(missing)'}",
        f"Source: {item.source or '(missing)'}",
        f"Author: {item.author or '(missing)'}",
        f"Shared notes: {item.notes or '(missing)'}",
        f"Page description: {source.meta_description or '(missing)'}",
        "",
        "Transcript:",
        transcript_excerpt or "(missing)",
        "",
        "Extracted source text:",
        source_excerpt or "(missing)",
    ]
    return "\n".join(sections)


def _request_summary(prompt: str, timeout: int) -> str:
    base_url = settings.ARCHIVE_SUMMARY_API_BASE.rstrip("/")
    request_body = json.dumps(
        {
            "model": settings.ARCHIVE_SUMMARY_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You generate concise archive metadata for saved links and media. "
                        "Always return valid JSON and only JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
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
        raise SummaryGenerationError(
            f"Summary API request failed: HTTP {exc.code}: {message}"
        ) from exc
    except URLError as exc:
        raise SummaryGenerationError(f"Summary API request failed: {exc.reason}") from exc
    except OSError as exc:
        raise SummaryGenerationError(f"Summary API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SummaryGenerationError("Summary API returned invalid JSON") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummaryGenerationError("Summary API response did not include content") from exc
    if not isinstance(content, str) or not content.strip():
        raise SummaryGenerationError("Summary API response was empty")
    return content


def _parse_generated_summary(raw_content: str) -> GeneratedSummary:
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise SummaryGenerationError("Summary response was not valid JSON") from exc

    short_summary = _normalize_text(str(payload.get("short_summary", "")))
    long_summary = _normalize_text(str(payload.get("long_summary", "")))
    tags = _normalize_tags(payload.get("tags"))

    if not short_summary or not long_summary or not tags:
        raise SummaryGenerationError("Summary response did not include all required fields")

    return GeneratedSummary(
        short_summary=short_summary,
        long_summary=long_summary,
        tags=tags,
    )


def _normalize_tags(value) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = re.split(r"[\n,]", value)
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []

    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in candidates:
        cleaned = _normalize_text(raw_tag).strip(" -#").lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        tags.append(cleaned)
        if len(tags) == 8:
            break
    return tuple(tags)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _truncate_text(value: str, max_chars: int = MAX_SUMMARY_INPUT_CHARS) -> str:
    normalized = value.strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rsplit(" ", 1)[0].strip()
