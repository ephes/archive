from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection
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

MAX_SUMMARY_REQUEST_BYTES = 64 * 1024

MAX_SUMMARY_SOURCE_BYTES = 1024 * 1024
MAX_SUMMARY_HTML_DEPTH = 4096
MAX_SUMMARY_INPUT_CHARS = 12_000
MAX_ARTICLE_AUDIO_SOURCE_CHARS = 100_000
MAX_TRANSCRIPT_INPUT_CHARS = 8_000
MAX_SOURCE_WITH_TRANSCRIPT_INPUT_CHARS = 4_000


class SummaryGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FieldLimitViolation:
    field: str
    measured: int
    limit: int
    unit: str  # "characters", "tags" or "characters per tag"

    def describe(self) -> str:
        return f"{self.field}: {self.measured} > {self.limit} {self.unit}"


class SummaryLimitError(SummaryGenerationError):
    """A reply exceeded the generated field limits; names fields and sizes, never the text."""

    def __init__(
        self, violations: tuple[FieldLimitViolation, ...], reply: dict[str, object]
    ) -> None:
        details = ", ".join(violation.describe() for violation in violations)
        super().__init__(f"Summary response exceeded generated field limits ({details})")
        self.violations = violations
        self.reply = reply


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
    _LIST_TAGS = {"ul", "ol"}
    # Elements whose start or end tag implicitly closes an open <p> (HTML5 parsing rules).
    _P_CLOSING_TAGS = _TEXT_TAGS | _LIST_TAGS | {
        "address", "article", "aside", "details", "dialog", "div", "dl", "dd", "dt",
        "fieldset", "figcaption", "figure", "header", "hgroup", "hr", "main", "menu",
        "section", "summary", "table", "tbody", "td", "th", "thead", "tfoot", "tr",
    }
    # Text inside these elements flows with its neighbours; any other element separates.
    _INLINE_TAGS = {
        "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn", "em", "i",
        "ins", "kbd", "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup",
        "time", "u", "var", "wbr",
    }
    # Short top-level list items are usually navigation, share or tag-list noise.
    _MIN_TOP_LEVEL_LIST_ITEM_CHARS = 25

    def __init__(self) -> None:
        super().__init__()
        self.meta_description = ""
        self.text_chunks: list[str] = []
        self._ignored_depth = 0
        # Open text containers, plus list markers opened inside a container.
        self._text_containers: list[str] = []
        # Per-tag positions make absent/matching/implicit-close lookups constant
        # work. Each opened container is appended and removed exactly once.
        self._open_positions: dict[str, list[int]] = {}
        self._current_parts: list[str] = []
        # Index of the first chunk emitted by the open top-level <li>, if any.
        self._top_level_item_start: int | None = None

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

        if self._ignored_depth > 0:
            return
        if lowered in self._P_CLOSING_TAGS:
            self._close_through("p")
        if lowered == "li":
            self._close_through("li", boundary=self._LIST_TAGS)
        if lowered in self._TEXT_TAGS:
            self._flush_text()
            if lowered == "li" and not self._text_containers:
                self._top_level_item_start = len(self.text_chunks)
            self._open_container(lowered)
        elif lowered in self._LIST_TAGS and self._text_containers:
            self._flush_text()
            self._open_container(lowered)
        elif self._text_containers and lowered not in self._INLINE_TAGS:
            self._current_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS and self._ignored_depth > 0:
            self._ignored_depth -= 1
            return
        if self._ignored_depth > 0:
            return

        if lowered in self._LIST_TAGS:
            if lowered in self._open_positions:
                self._close_through(lowered)
            else:
                # A list opened outside any container closes every item still open in it.
                self._close_through_all()
        elif lowered in self._open_positions:
            # Also tolerate omitted inner closing tags without retaining a stale container.
            self._close_through(lowered)
        else:
            if lowered in self._P_CLOSING_TAGS:
                self._close_through("p")
            if self._text_containers and lowered not in self._INLINE_TAGS:
                self._current_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and self._text_containers:
            self._current_parts.append(data)

    def _open_container(self, tag: str) -> None:
        if len(self._text_containers) >= MAX_SUMMARY_HTML_DEPTH:
            raise SummaryGenerationError("Summary source HTML exceeded container depth limit")
        self._open_positions.setdefault(tag, []).append(len(self._text_containers))
        self._text_containers.append(tag)

    def _close_through(self, tag: str, *, boundary: Collection[str] = ()) -> None:
        positions = self._open_positions.get(tag)
        if not positions:
            return
        index = positions[-1]
        # The nearest list marker prevents closing a parent list item. The
        # boundary is the fixed, two-tag list set, independent of source depth.
        for boundary_tag in boundary:
            boundary_positions = self._open_positions.get(boundary_tag)
            if boundary_positions and boundary_positions[-1] >= index:
                return
        self._close_containers_from(index)

    def _close_containers_from(self, index: int) -> None:
        self._flush_text()
        while len(self._text_containers) > index:
            tag = self._text_containers.pop()
            positions = self._open_positions[tag]
            positions.pop()
            if not positions:
                del self._open_positions[tag]
        self._finish_top_level_item()

    def _close_through_all(self) -> None:
        if self._text_containers:
            self._close_containers_from(0)

    def _finish_top_level_item(self) -> None:
        """Drop a closed top-level <li> whose whole text, nested parts included, is short."""
        start = self._top_level_item_start
        if start is None or self._text_containers:
            return
        self._top_level_item_start = None
        item_text = " ".join(self.text_chunks[start:])
        if len(item_text) < self._MIN_TOP_LEVEL_LIST_ITEM_CHARS:
            del self.text_chunks[start:]

    def _flush_text(self) -> None:
        text = _normalize_text("".join(self._current_parts))
        self._current_parts = []
        if text:
            self.text_chunks.append(text)

    def close(self) -> None:
        super().close()
        self._close_through_all()
        self._flush_text()


def generate_item_summaries(item: Item, timeout: int = 60) -> GeneratedSummary:
    api_key = settings.ARCHIVE_SUMMARY_API_KEY.strip()
    if not api_key:
        raise SummaryGenerationError("Summary generation is not configured")

    source = _best_effort_summary_source(item=item, timeout=timeout)
    prompt = _build_summary_prompt(item=item, source=source)
    response = _request_summary(prompt=prompt, timeout=timeout)
    try:
        return _parse_generated_summary(response)
    except SummaryLimitError as exc:
        # Models miscount characters, mostly in long non-English replies. Ask once to
        # shorten the reply itself (no source text again); never truncate it here.
        repair = _build_shortening_prompt(exc)
        if repair is None:
            raise
        return _parse_generated_summary(_request_summary(prompt=repair, timeout=timeout))


def _best_effort_summary_source(item: Item, timeout: int) -> SummarySource:
    try:
        return extract_summary_source_from_url(
            item.original_url, timeout=timeout, truncate_oversized=True
        )
    except SummaryGenerationError as exc:
        logger.warning("Summary source extraction failed for item %s: %s", item.pk, exc)
        return SummarySource()


def extract_summary_source_from_url(
    url: str,
    timeout: int = 60,
    *,
    max_chars: int = MAX_SUMMARY_INPUT_CHARS,
    truncate_oversized: bool = False,
) -> SummarySource:
    """Extract page text. Larger pages fail unless ``truncate_oversized`` allows
    using their first 1 MiB (summaries only excerpt a few thousand characters)."""
    request = Request(url, headers=REQUEST_HEADERS)
    try:
        with open_public_url(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            if not _is_text_source(content_type):
                # Media and other binary bodies carry no summary text; do not download them.
                return SummarySource()
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
        if not truncate_oversized:
            raise SummaryGenerationError("Summary source exceeded 1 MiB limit")
        payload = payload[:MAX_SUMMARY_SOURCE_BYTES]

    try:
        # bytes.decode admits text codecs only; remote codec names must never
        # escape the source error boundary or appear in stored diagnostics.
        text = payload.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        raise SummaryGenerationError("Summary source charset is unsupported or invalid") from None
    if content_type.startswith("text/plain"):
        return SummarySource(extracted_text=_truncate_text(text, max_chars=max_chars))
    return extract_summary_source_from_html(text, max_chars=max_chars)


def _is_text_source(content_type: str) -> bool:
    return content_type.startswith("text/plain") or "html" in content_type or "xml" in content_type


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
        *_FIELD_LIMIT_INSTRUCTIONS,
        "Write summaries and tags in the same language as the transcript when present.",
        (
            "If there is no transcript, use the dominant language of the page description and "
            "extracted source text. Do not translate non-English content into English."
        ),
        "Avoid markdown, bullets, and filler.",
        "",
        f"Original URL: {item.original_url}",
        f"Kind: {item.kind}",
    ]
    optional_fields = [
        ("Current title: ", item.title),
        ("Source: ", item.source),
        ("Author: ", item.author),
        ("Shared notes: ", item.notes),
        ("Page description: ", source.meta_description),
        ("\nTranscript:\n", transcript_excerpt),
        ("\nExtracted source text:\n", source_excerpt),
    ]
    # Reserve instructions, URL, kind, labels, model and the entire wire envelope.
    # Share remaining serialized bytes across optional fields, carrying unused
    # space forward so large metadata cannot consume the transcript/source share.
    sections.extend(label for label, _ in optional_fields)
    remaining = MAX_SUMMARY_REQUEST_BYTES - len(_summary_request_body("\n".join(sections)))
    if remaining < 0:
        raise SummaryGenerationError("Summary request exceeded 64 KiB limit")
    first_optional = len(sections) - len(optional_fields)
    for index, (_, value) in enumerate(optional_fields):
        allowance = remaining // (len(optional_fields) - index)
        text = _truncate_prompt_value(value or "(missing)", max_bytes=allowance)
        sections[first_optional + index] += text
        remaining -= len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2
    return "\n".join(sections)


_FIELD_LIMIT_INSTRUCTIONS = (
    (
        "short_summary must be 1 or 2 sentences and at most "
        f"{MAX_SHORT_SUMMARY_CHARS} characters including spaces; aim for about 200."
    ),
    f"long_summary must be 2 to 4 sentences and at most {MAX_LONG_SUMMARY_CHARS} characters.",
    (
        f"tags must be a JSON array of 3 to {MAX_SUMMARY_TAGS} lowercase tags, "
        f"at most {MAX_SUMMARY_TAG_CHARS} characters each."
    ),
)


# Shortening targets sit well below the hard limits: models miscount characters.
SHORTENING_SHORT_SUMMARY_TARGET_CHARS = 160
SHORTENING_LONG_SUMMARY_TARGET_CHARS = 6_000


def _shortening_instruction(violation: FieldLimitViolation) -> str:
    if violation.field == "short_summary":
        return (
            f"short_summary has {violation.measured} characters. Rewrite it to at most "
            f"{SHORTENING_SHORT_SUMMARY_TARGET_CHARS} characters including spaces, preferably "
            f"one sentence (hard limit {MAX_SHORT_SUMMARY_CHARS})."
        )
    if violation.field == "long_summary":
        return (
            f"long_summary has {violation.measured} characters. Rewrite it to at most "
            f"{SHORTENING_LONG_SUMMARY_TARGET_CHARS} characters "
            f"(hard limit {MAX_LONG_SUMMARY_CHARS})."
        )
    if violation.unit == "tags":
        return (
            f"tags has {violation.measured} entries. Keep only the {MAX_SUMMARY_TAGS} "
            "most relevant tags."
        )
    return (
        f"A tag has {violation.measured} characters. Shorten every tag to at most "
        f"{MAX_SUMMARY_TAG_CHARS} characters."
    )


def _build_shortening_prompt(exc: SummaryLimitError) -> str | None:
    """A bounded follow-up asking the model to shorten its own over-limit reply."""
    prompt = "\n".join(
        [
            "Your archive metadata JSON below exceeded these limits:",
            *_FIELD_LIMIT_INSTRUCTIONS,
            "",
            *(_shortening_instruction(violation) for violation in exc.violations),
            (
                "Return the same JSON keys; change only "
                f"{', '.join(sorted({violation.field for violation in exc.violations}))}. "
                "Keep the meaning and the language; do not translate."
            ),
            "",
            json.dumps(exc.reply, ensure_ascii=False),
        ]
    )
    if len(_summary_request_body(prompt)) > MAX_SUMMARY_REQUEST_BYTES:
        return None
    return prompt


def _truncate_prompt_value(value: str, *, max_bytes: int) -> str:
    # JSON escaping counts too; prefixes end only at Unicode character boundaries.
    bounded = value[:max_bytes]
    if (
        len(value) <= max_bytes
        and len(json.dumps(bounded, ensure_ascii=False).encode()) - 2 <= max_bytes
    ):
        return value
    low, high = 0, len(bounded)
    while low < high:
        middle = (low + high + 1) // 2
        size = len(json.dumps(bounded[:middle] + "…", ensure_ascii=False).encode()) - 2
        if size <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return bounded[:low] + "…" if max_bytes >= 3 else ""


def _summary_request_body(prompt: str) -> bytes:
    return json.dumps(
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
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _request_summary(prompt: str, timeout: int) -> str:
    base_url = settings.ARCHIVE_SUMMARY_API_BASE.rstrip("/")
    request_body = _summary_request_body(prompt)
    if len(request_body) > MAX_SUMMARY_REQUEST_BYTES:
        raise SummaryGenerationError("Summary request exceeded 64 KiB limit")
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

    longest_tag = max((len(tag) for tag in tags), default=0)
    over_limit = tuple(
        violation
        for violation in (
            FieldLimitViolation(
                "short_summary", len(short_summary), MAX_SHORT_SUMMARY_CHARS, "characters"
            ),
            FieldLimitViolation(
                "long_summary", len(long_summary), MAX_LONG_SUMMARY_CHARS, "characters"
            ),
            FieldLimitViolation("tags", len(raw_tags), MAX_SUMMARY_TAGS, "tags"),
            FieldLimitViolation(
                "tags", longest_tag, MAX_SUMMARY_TAG_CHARS, "characters per tag"
            ),
        )
        if violation.measured > violation.limit
    )
    if over_limit:
        reply: dict[str, object] = {
            "short_summary": short_summary,
            "long_summary": long_summary,
            # Before deduplication, so the shown list matches the counted tags.
            "tags": [_normalize_text(tag) for tag in raw_tags],
        }
        raise SummaryLimitError(over_limit, reply)

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
