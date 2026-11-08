from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.request import Request

from django.conf import settings

from archive.api_responses import (
    InvalidAPIJSON,
    decode_api_json,
    read_api_response,
    safe_api_error,
)
from archive.metadata import REQUEST_HEADERS
from archive.models import Item
from archive.safe_fetch import open_public_url
from archive.upstream_http import open_authenticated_url as urlopen

logger = logging.getLogger(__name__)

MAX_SHORT_SUMMARY_CHARS = 239
MAX_LONG_SUMMARY_CHARS = 8_000
MAX_SUMMARY_TAGS = 8
MAX_SUMMARY_TAG_CHARS = 64

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
    _TEXT_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.meta_description = ""
        self.text_chunks: list[str] = []
        self._ignored_depth = 0
        self._text_containers: list[str] = []
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
            self._flush_text()
            self._text_containers.append(lowered)
        elif self._ignored_depth == 0 and lowered == "br" and self._text_containers:
            self._current_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return

        if self._ignored_depth == 0 and lowered in self._text_containers:
            self._flush_text()
            # Also tolerate omitted inner closing tags without retaining a stale container.
            while self._text_containers.pop() != lowered:
                pass

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and self._text_containers:
            self._current_parts.append(data)

    def _flush_text(self) -> None:
        text = _normalize_text("".join(self._current_parts))
        if text:
            self.text_chunks.append(text)
        self._current_parts = []

    def close(self) -> None:
        super().close()
        self._flush_text()


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
        with open_public_url(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read(MAX_SUMMARY_SOURCE_BYTES + 1)
    except HTTPError as exc:
        raise SummaryGenerationError(
            f"{safe_api_error(exc)} while fetching summary source"
        ) from None
    except URLError as exc:
        raise SummaryGenerationError(
            f"Could not fetch summary source: {safe_api_error(exc)}"
        ) from None
    except OSError as exc:
        raise SummaryGenerationError(
            f"Could not fetch summary source: {safe_api_error(exc)}"
        ) from None

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
        f"long_summary must be 2 to 4 sentences and at most {MAX_LONG_SUMMARY_CHARS} characters.",
        (
            "tags must be a JSON array of 3 to 8 lowercase tags, "
            f"at most {MAX_SUMMARY_TAG_CHARS} characters each."
        ),
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
            payload = decode_api_json(read_api_response(response))
    except HTTPError as exc:
        raise SummaryGenerationError(f"Summary API request failed: {safe_api_error(exc)}") from None
    except URLError as exc:
        raise SummaryGenerationError(f"Summary API request failed: {safe_api_error(exc)}") from None
    except OSError as exc:
        raise SummaryGenerationError(f"Summary API request failed: {safe_api_error(exc)}") from None
    except InvalidAPIJSON:
        raise SummaryGenerationError("Summary API returned invalid JSON") from None

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SummaryGenerationError("Summary API response did not include content") from None
    if not isinstance(content, str) or not content.strip():
        raise SummaryGenerationError("Summary API response was empty")
    return content


def _parse_generated_summary(raw_content: str) -> GeneratedSummary:
    try:
        payload = decode_api_json(raw_content)
    except InvalidAPIJSON:
        raise SummaryGenerationError("Summary response was not valid JSON") from None

    if not isinstance(payload, dict):
        raise SummaryGenerationError("Summary response must be an object")
    short = payload.get("short_summary")
    long = payload.get("long_summary")
    raw_tags = payload.get("tags")
    if (
        not isinstance(short, str)
        or not isinstance(long, str)
        or not isinstance(raw_tags, list)
        or not all(isinstance(tag, str) and _normalize_text(tag).strip(" -#") for tag in raw_tags)
    ):
        raise SummaryGenerationError("Summary response fields must contain text")
    short_summary = _normalize_text(short)
    long_summary = _normalize_text(long)
    tags = _normalize_tags(raw_tags)

    if (
        len(short_summary) > MAX_SHORT_SUMMARY_CHARS
        or len(long_summary) > MAX_LONG_SUMMARY_CHARS
        or len(raw_tags) > MAX_SUMMARY_TAGS
        or any(len(tag) > MAX_SUMMARY_TAG_CHARS for tag in tags)
    ):
        raise SummaryGenerationError("Summary response exceeded generated field limits")

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
    return tuple(tags)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _truncate_text(value: str, max_chars: int = MAX_SUMMARY_INPUT_CHARS) -> str:
    normalized = value.strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rsplit(" ", 1)[0].strip()
