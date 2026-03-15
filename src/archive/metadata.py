from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".m3u8")
MAX_METADATA_BYTES = 1024 * 1024
REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "User-Agent": "archive-bot/1.0 (+https://archive.home.xn--wersdrfer-47a.de/)",
}


class MetadataExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedMetadata:
    title: str = ""
    source: str = ""
    author: str = ""
    original_published_at: datetime | None = None
    media_url: str = ""
    audio_url: str = ""
    kind_hint: str = ""
    media_candidates: tuple[ExtractedMediaCandidate, ...] = ()


@dataclass(frozen=True)
class ExtractedMediaCandidate:
    url: str
    candidate_type: str
    detection_source: str


class _MetadataHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.jsonld_blobs: list[str] = []
        self.media_candidates: list[ExtractedMediaCandidate] = []
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []
        self._media_context_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attr_map = {
            key.lower(): value
            for key, value in attrs
            if key is not None and value is not None and value != ""
        }
        if tag == "title":
            self._in_title = True
            return

        if tag == "meta":
            content = (attr_map.get("content") or "").strip()
            if not content:
                return
            for key_name in ("property", "name", "itemprop"):
                key = (attr_map.get(key_name) or "").strip().lower()
                if key and key not in self.meta:
                    self.meta[key] = content
            return

        if tag in {"audio", "video"}:
            self._media_context_stack.append(tag)
            src = (attr_map.get("src") or "").strip()
            if src:
                self._add_media_candidate(
                    url=src,
                    candidate_type="audio" if tag == "audio" else "video",
                    detection_source=f"html_{tag}",
                )
            return

        if tag == "source":
            parent_tag = self._media_context_stack[-1] if self._media_context_stack else ""
            src = (attr_map.get("src") or "").strip()
            if src and parent_tag in {"audio", "video"}:
                self._add_media_candidate(
                    url=src,
                    candidate_type="audio" if parent_tag == "audio" else "video",
                    detection_source=f"html_{parent_tag}",
                )
            return

        if tag == "script":
            script_type = (attr_map.get("type") or "").strip().lower()
            if "ld+json" in script_type:
                self._in_jsonld = True
                self._jsonld_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return
        if tag in {"audio", "video"} and self._media_context_stack:
            for index in range(len(self._media_context_stack) - 1, -1, -1):
                if self._media_context_stack[index] == tag:
                    del self._media_context_stack[index]
                    break
            return
        if tag == "script" and self._in_jsonld:
            blob = "".join(self._jsonld_parts).strip()
            if blob:
                self.jsonld_blobs.append(blob)
            self._in_jsonld = False
            self._jsonld_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_jsonld:
            self._jsonld_parts.append(data)

    def _add_media_candidate(self, *, url: str, candidate_type: str, detection_source: str) -> None:
        candidate = ExtractedMediaCandidate(
            url=url,
            candidate_type=candidate_type,
            detection_source=detection_source,
        )
        if candidate not in self.media_candidates:
            self.media_candidates.append(candidate)


def extract_metadata_from_url(url: str, timeout: int = 15) -> ExtractedMetadata:
    request = Request(url, headers=REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type and "html" not in content_type and "xml" not in content_type:
                raise MetadataExtractionError(f"Unsupported content type: {content_type}")
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as exc:
        raise MetadataExtractionError(f"HTTP {exc.code} while fetching metadata") from exc
    except URLError as exc:
        raise MetadataExtractionError(f"Could not fetch metadata: {exc.reason}") from exc
    except OSError as exc:
        raise MetadataExtractionError(f"Could not fetch metadata: {exc}") from exc

    if len(payload) > MAX_METADATA_BYTES:
        raise MetadataExtractionError("Metadata response exceeded 1 MiB limit")

    html = payload.decode(charset, errors="replace")
    return extract_metadata_from_html(html=html, base_url=url)


def extract_metadata_from_html(html: str, base_url: str) -> ExtractedMetadata:
    parser = _MetadataHTMLParser()
    parser.feed(html)
    parser.close()

    jsonld_metadata = _extract_jsonld_metadata(parser.jsonld_blobs, base_url=base_url)
    meta = parser.meta
    source_from_host = _humanize_host(base_url)
    title = _first_nonempty(
        meta.get("og:title"),
        meta.get("twitter:title"),
        jsonld_metadata.title,
        " ".join(part.strip() for part in parser.title_parts).strip(),
    )
    source = _first_nonempty(
        meta.get("og:site_name"),
        meta.get("application-name"),
        meta.get("publisher"),
        jsonld_metadata.source,
        source_from_host,
    )
    author = _first_nonempty(
        meta.get("author"),
        meta.get("article:author"),
        meta.get("og:article:author"),
        jsonld_metadata.author,
    )
    original_published_at = _parse_datetime_value(
        _first_nonempty(
            meta.get("article:published_time"),
            meta.get("og:published_time"),
            meta.get("date"),
            meta.get("pubdate"),
            jsonld_metadata.original_published_at.isoformat()
            if jsonld_metadata.original_published_at
            else "",
        )
    )
    media_url = _normalize_url(
        _first_nonempty(
            meta.get("og:audio"),
            meta.get("og:audio:url"),
            meta.get("twitter:player:stream"),
            meta.get("og:video"),
            meta.get("og:video:url"),
            jsonld_metadata.media_url,
            next(
                (
                    candidate.url
                    for candidate in parser.media_candidates
                    if candidate.candidate_type == "video"
                ),
                "",
            ),
        ),
        base_url=base_url,
    )
    audio_url = _normalize_url(
        _first_nonempty(
            jsonld_metadata.audio_url,
            next(
                (
                    candidate.url
                    for candidate in parser.media_candidates
                    if candidate.candidate_type == "audio"
                ),
                "",
            ),
        ),
        base_url=base_url,
    )
    if not audio_url and media_url and _looks_like_audio(media_url):
        audio_url = media_url

    kind_hint = _first_nonempty(
        _kind_hint_from_og_type(meta.get("og:type", "")),
        jsonld_metadata.kind_hint,
    )
    media_candidates = tuple(
        ExtractedMediaCandidate(
            url=_normalize_url(candidate.url, base_url=base_url),
            candidate_type=candidate.candidate_type,
            detection_source=candidate.detection_source,
        )
        for candidate in parser.media_candidates
        if _normalize_url(candidate.url, base_url=base_url)
    )

    return ExtractedMetadata(
        title=title,
        source=source,
        author=author,
        original_published_at=original_published_at,
        media_url=media_url,
        audio_url=_normalize_url(audio_url, base_url=base_url),
        kind_hint=kind_hint,
        media_candidates=media_candidates,
    )


def _extract_jsonld_metadata(blobs: list[str], base_url: str) -> ExtractedMetadata:
    values: list[ExtractedMetadata] = []
    for blob in blobs:
        for record in _iter_jsonld_records(blob):
            values.append(_extract_from_jsonld_record(record, base_url=base_url))

    title = _first_nonempty(*(value.title for value in values))
    source = _first_nonempty(*(value.source for value in values))
    author = _first_nonempty(*(value.author for value in values))
    published = next(
        (value.original_published_at for value in values if value.original_published_at),
        None,
    )
    media_url = _first_nonempty(*(value.media_url for value in values))
    audio_url = _first_nonempty(*(value.audio_url for value in values))
    kind_hint = _first_nonempty(*(value.kind_hint for value in values))
    return ExtractedMetadata(
        title=title,
        source=source,
        author=author,
        original_published_at=published,
        media_url=media_url,
        audio_url=audio_url,
        kind_hint=kind_hint,
    )


def _iter_jsonld_records(blob: str) -> list[dict]:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []

    records: list[dict] = []

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        records.append(value)
        graph = value.get("@graph")
        if graph:
            visit(graph)

    visit(data)
    return records


def _extract_from_jsonld_record(record: dict, base_url: str) -> ExtractedMetadata:
    title = _first_nonempty(
        _string_value(record.get("headline")),
        _string_value(record.get("name")),
        _string_value(record.get("title")),
    )
    source = _first_nonempty(
        _entity_name(record.get("publisher")),
        _entity_name(record.get("provider")),
        _entity_name(record.get("isPartOf")),
    )
    author = _first_nonempty(
        _entity_name(record.get("author")),
        _entity_name(record.get("creator")),
    )
    record_url = _string_value(record.get("url"))
    original_published_at = _parse_datetime_value(
        _first_nonempty(
            _string_value(record.get("datePublished")),
            _string_value(record.get("dateCreated")),
            _string_value(record.get("uploadDate")),
        )
    )

    media_url = _normalize_url(
        _first_nonempty(
            _string_value(record.get("contentUrl")),
            _string_value(record.get("embedUrl")),
            _string_value(record.get("associatedMedia")),
            record_url if _looks_like_media(record_url) else "",
        ),
        base_url=base_url,
    )
    audio_url = media_url if media_url and _looks_like_audio(media_url) else ""
    kind_hint = _kind_hint_from_jsonld_type(record.get("@type"))

    return ExtractedMetadata(
        title=title,
        source=source,
        author=author,
        original_published_at=original_published_at,
        media_url=media_url,
        audio_url=audio_url,
        kind_hint=kind_hint,
    )


def _kind_hint_from_jsonld_type(value) -> str:
    types = value if isinstance(value, list) else [value]
    lowered = {str(item).lower() for item in types if item}
    if {"videoobject", "movie", "clip"} & lowered:
        return "video"
    if {"audioobject", "podcastepisode", "episode"} & lowered:
        return "podcast_episode"
    if {"article", "newsarticle", "blogposting", "report"} & lowered:
        return "article"
    return ""


def _kind_hint_from_og_type(value: str) -> str:
    lowered = value.lower()
    if lowered.startswith("video"):
        return "video"
    if lowered.startswith("article"):
        return "article"
    if lowered.startswith("music") or lowered.startswith("audio"):
        return "podcast_episode"
    return ""


def _entity_name(value) -> str:
    if isinstance(value, list):
        return _first_nonempty(*(_entity_name(item) for item in value))
    if isinstance(value, dict):
        return _first_nonempty(
            _string_value(value.get("name")),
            _string_value(value.get("headline")),
        )
    return _string_value(value)


def _string_value(value) -> str:
    if isinstance(value, dict):
        return _string_value(value.get("url")) or _string_value(value.get("contentUrl"))
    if value is None:
        return ""
    return str(value).strip()


def _parse_datetime_value(value: str) -> datetime | None:
    cleaned = value.strip()
    if not cleaned:
        return None

    parsed = parse_datetime(cleaned)
    if parsed:
        return _ensure_aware(parsed)

    parsed_date = parse_date(cleaned)
    if parsed_date:
        return timezone.make_aware(
            datetime.combine(parsed_date, time.min),
            timezone.get_default_timezone(),
        )

    try:
        return _ensure_aware(parsedate_to_datetime(cleaned))
    except (TypeError, ValueError, IndexError):
        return None


def _ensure_aware(value: datetime) -> datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_default_timezone())
    return value


def _normalize_url(value: str, base_url: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    return urljoin(base_url, cleaned)


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def _humanize_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _looks_like_audio(url: str) -> bool:
    return urlparse(url).path.lower().endswith(AUDIO_SUFFIXES)


def _looks_like_media(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(AUDIO_SUFFIXES + VIDEO_SUFFIXES)
