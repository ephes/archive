from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from http.client import HTTPException
from itertools import chain
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request

from django.conf import settings
from django.db import models, transaction

from archive.api_responses import (
    InvalidAPIJSON,
    decode_api_json,
    read_api_response,
    safe_api_error,
)
from archive.media_archival import save_reserved_media_file
from archive.media_storage import (
    delete_unreferenced_archive_media_paths,
    release_pending_archive_media_paths,
)
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.summaries import MAX_ARTICLE_AUDIO_SOURCE_CHARS, extract_summary_source_from_url
from archive.upstream_http import api_url
from archive.upstream_http import open_authenticated_url as urlopen
from archive.upstream_jobs import artifact_path, wire_id, wire_state

logger = logging.getLogger(__name__)

DEFAULT_AUDIO_FORMAT = "mp3"
DEFAULT_AUDIO_CONTENT_TYPE = "audio/mpeg"


class ArticleAudioGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArticleAudioJobUpdate:
    job_id: str
    state: str
    artifact_path: str = ""
    error_message: str = ""

    @property
    def is_complete(self) -> bool:
        return self.state == "succeeded" and bool(self.artifact_path)

    @property
    def is_pending(self) -> bool:
        return self.state in {"queued", "running"}


def can_generate_article_audio(item: Item) -> bool:
    """Return whether article audio has local fallback text ready.

    This gate intentionally checks only stored summary/notes fields. Extracted
    source text is fetched later at submission time on a best-effort basis.
    """

    return item.kind == ItemKind.ARTICLE and bool(build_article_audio_script(item))


def build_article_audio_script(
    item: Item,
    source_text: str = "",
    *,
    max_chars: int | None = None,
) -> str:
    if item.kind != ItemKind.ARTICLE:
        return ""

    stored_bodies = (getattr(item, field) for field in ("long_summary", "short_summary", "notes"))
    for body in chain((source_text,), stored_bodies):
        body_start, body_end = _script_part_bounds(body)
        if body_start < body_end:
            break
    else:
        return ""

    title = item.title
    title_start, title_end = _script_part_bounds(title)
    parts = []
    if title_start < title_end:
        parts.extend([(title, title_start, title_end), ("\n\n", 0, 2)])
    parts.append((body, body_start, body_end))

    length = sum(end - start for _, start, end in parts)
    if max_chars is None or max_chars >= length:
        return "".join(text[start:end] for text, start, end in parts)

    # Match slicing for direct callers that pass a negative cap, too.
    remaining = max_chars if max_chars >= 0 else max(0, length + max_chars)
    prefix = []
    for text, start, end in parts:
        stop = min(end, start + remaining)
        prefix.append(text[start:stop])
        if stop < end:
            return _trim_partial_script("".join(prefix), text[stop])
        remaining -= stop - start
    return "".join(prefix)


def _script_part_bounds(value: str) -> tuple[int, int]:
    """Find the stripped span without copying the text."""
    start, end = 0, len(value)
    while start < end and value[start].isspace():
        start += 1
    while end > start and value[end - 1].isspace():
        end -= 1
    return start, end


def generate_item_article_audio(item: Item, timeout: int = 30) -> ArticleAudioJobUpdate:
    api_key = settings.ARCHIVE_ARTICLE_AUDIO_API_KEY.strip()
    if not api_key:
        raise ArticleAudioGenerationError("Article audio generation is not configured")

    job_id = item.article_audio_job_id.strip()
    if job_id:
        return _poll_synthesis_job(job_id=job_id, timeout=timeout, api_key=api_key)

    extracted_source_text = _best_effort_article_audio_source_text(item=item, timeout=timeout)
    script_max_chars = settings.ARCHIVE_ARTICLE_AUDIO_SCRIPT_MAX_CHARS
    if script_max_chars <= 0:
        raise ArticleAudioGenerationError("Article audio script max chars must be positive")

    script = build_article_audio_script(
        item,
        source_text=extracted_source_text,
        max_chars=script_max_chars,
    )
    if not script:
        raise ArticleAudioGenerationError("Item does not have article-audio source text yet")

    return _submit_synthesis_job(item=item, text=script, timeout=timeout, api_key=api_key)


def store_generated_article_audio(item: Item, timeout: int = 30) -> None:
    """Download to a disk temporary file, then publish one reserved storage object.

    A conditional database update prevents concurrent requests from replacing an
    already published copy. Successful publication resets legacy generation failure
    state and refreshes that state on the caller's item. Losers discard their upload
    through the normal media deletion guard. No storage object is written until
    the download is complete.
    """
    expected = {
        field: getattr(item, field)
        for field in (
            "original_url",
            "kind",
            "title",
            "short_summary",
            "long_summary",
            "notes",
            "summary_status",
            "article_audio_job_id",
            "article_audio_artifact_path",
            "article_audio_status",
            "article_audio_error",
            "article_audio_generated",
            "article_audio_poll_at",
            "processing_started_at",
        )
    }
    if item.pk:
        item.refresh_from_db(
            fields=[
                "article_audio_file",
                "article_audio_size_bytes",
                "article_audio_content_type",
            ]
        )
    if item.article_audio_file:
        return
    api_key = settings.ARCHIVE_ARTICLE_AUDIO_API_KEY.strip()
    if not api_key:
        raise ArticleAudioGenerationError("Article audio download is not configured")
    artifact_path = item.article_audio_artifact_path.strip()
    if not artifact_path:
        raise ArticleAudioGenerationError("Item does not have generated article audio")
    request = Request(_api_url(artifact_path), headers={"Authorization": f"Bearer {api_key}"})
    using = item._state.db or "default"
    stored_name = ""
    try:
        try:
            with NamedTemporaryFile(prefix="archive-article-audio-") as temporary:
                size = 0
                with urlopen(
                    request,
                    timeout=timeout,
                    max_bytes=settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES,
                    audio=True,
                ) as response:
                    content_type = response.headers.get_content_type().lower()
                    if content_type not in {"audio/mpeg", "audio/mp3"}:
                        raise ArticleAudioGenerationError("Article audio artifact is not MP3 audio")
                    raw_length = response.headers.get("Content-Length")
                    try:
                        expected_size = int(raw_length) if raw_length is not None else None
                    except ValueError:
                        raise ArticleAudioGenerationError(
                            "Article audio artifact has an invalid Content-Length"
                        ) from None
                    if (
                        expected_size is not None
                        and expected_size > settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES
                    ):
                        raise ArticleAudioGenerationError(
                            "Article audio artifact exceeded download limit"
                        )
                    while chunk := response.read(64 * 1024):
                        size += len(chunk)
                        if size > settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES:
                            raise ArticleAudioGenerationError(
                                "Article audio artifact exceeded download limit"
                            )
                        temporary.write(chunk)
                if expected_size is not None and size != expected_size:
                    raise ArticleAudioGenerationError(
                        "Article audio artifact download was incomplete"
                    )
                if not size:
                    raise ArticleAudioGenerationError("Article audio artifact is empty")
                temporary.seek(0)
                stored_name = save_reserved_media_file(
                    target_name=f"article-audio/{item.pk}/speech.mp3",
                    upload_name="speech.mp3",
                    file_handle=temporary,
                    using=using,
                    owner=item,
                )
            with transaction.atomic(using=using, durable=True):
                published = (
                    Item.objects.using(using)
                    .filter(
                        pk=item.pk,
                        **expected,
                    )
                    .filter(
                        models.Q(article_audio_file="") | models.Q(article_audio_file__isnull=True)
                    )
                    .update(
                        article_audio_file=stored_name,
                        article_audio_size_bytes=size,
                        article_audio_content_type=DEFAULT_AUDIO_CONTENT_TYPE,
                        article_audio_generated=True,
                        article_audio_status=EnrichmentStatus.COMPLETE,
                        article_audio_error="",
                        article_audio_poll_at=None,
                    )
                )
                if published:
                    release_pending_archive_media_paths((stored_name,), using=using)
            if not published:
                delete_unreferenced_archive_media_paths(
                    (stored_name,),
                    using=using,
                    owns_reservation=True,
                )
            item.refresh_from_db(
                fields=[
                    "article_audio_file",
                    "article_audio_size_bytes",
                    "article_audio_content_type",
                    "article_audio_generated",
                    "article_audio_status",
                    "article_audio_error",
                    "article_audio_poll_at",
                ]
            )
            if not item.article_audio_file:
                raise ArticleAudioGenerationError("Article audio changed during download")
        except BaseException:
            if stored_name:
                try:
                    delete_unreferenced_archive_media_paths(
                        (stored_name,),
                        using=using,
                        owns_reservation=True,
                    )
                except Exception:
                    logger.exception("Failed to discard unpublished article audio %s", stored_name)
            raise
    except HTTPError as exc:
        raise ArticleAudioGenerationError(
            f"Article audio artifact request failed: {safe_api_error(exc)}"
        ) from None
    except (OSError, HTTPException) as exc:
        raise ArticleAudioGenerationError(
            f"Article audio artifact request failed: {safe_api_error(exc)}"
        ) from None
    except Exception as exc:
        if isinstance(exc, ArticleAudioGenerationError):
            raise
        raise ArticleAudioGenerationError(
            "Article audio storage is temporarily unavailable"
        ) from None


def _trim_partial_script(prefix: str, next_char: str) -> str:
    if not prefix or prefix[-1].isspace() or next_char.isspace():
        return prefix.rstrip()

    for index in range(len(prefix) - 1, -1, -1):
        if prefix[index].isspace():
            return prefix[:index].rstrip()
    return prefix


def _synthesis_job_payload(item: Item, text: str) -> dict[str, object]:
    return {
        "job_type": "synthesize",
        "priority": "normal",
        "lane": "batch",
        "backend": "auto",
        "model": settings.ARCHIVE_ARTICLE_AUDIO_MODEL,
        "language": settings.ARCHIVE_ARTICLE_AUDIO_LANGUAGE,
        "voice": settings.ARCHIVE_ARTICLE_AUDIO_VOICE,
        "input": {"kind": "text", "text": text},
        "output": {"formats": [DEFAULT_AUDIO_FORMAT]},
        "context": {"producer": "archive", "item_id": item.pk},
    }


def _task_ref_for_script(item: Item, text: str) -> str:
    return _synthesis_task_ref(item, _synthesis_job_payload(item, text))


def _synthesis_task_ref(item: Item, payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"archive-item-{item.pk}-article-audio-v3-{digest}"


def _best_effort_article_audio_source_text(item: Item, timeout: int) -> str:
    try:
        source = extract_summary_source_from_url(
            item.original_url,
            timeout=timeout,
            max_chars=MAX_ARTICLE_AUDIO_SOURCE_CHARS,
        )
    except Exception:
        logger.warning("Article audio source_extraction_failed for item %s", item.pk)
        return ""
    return source.extracted_text.strip()


def _submit_synthesis_job(
    item: Item, text: str, timeout: int, *, api_key: str
) -> ArticleAudioJobUpdate:
    payload = _synthesis_job_payload(item, text)
    # Hash this exact request snapshot so every output-affecting field participates.
    payload["task_ref"] = _synthesis_task_ref(item, payload)
    request_body = json.dumps(payload).encode("utf-8")
    request = Request(
        _api_url("jobs"),
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    payload = _request_json(
        request=request,
        timeout=timeout,
        error_prefix="Article audio job submission failed",
    )
    return _parse_job_update(payload)


def _poll_synthesis_job(job_id: str, timeout: int, *, api_key: str) -> ArticleAudioJobUpdate:
    try:
        job_id = wire_id(job_id)
    except ValueError:
        raise ArticleAudioGenerationError("Invalid upstream job identifier") from None
    request = Request(
        _api_url(f"jobs/{job_id}"),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    payload = _request_json(
        request=request,
        timeout=timeout,
        error_prefix="Article audio job status request failed",
    )
    update = _parse_job_update(payload)
    if update.job_id != job_id:
        raise ArticleAudioGenerationError("Upstream poll job identity mismatch")
    return update


def _request_json(request: Request, timeout: int, error_prefix: str) -> dict[str, object]:
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = decode_api_json(read_api_response(response))
    except HTTPError as exc:
        raise ArticleAudioGenerationError(f"{error_prefix}: {safe_api_error(exc)}") from None
    except URLError as exc:
        raise ArticleAudioGenerationError(f"{error_prefix}: {safe_api_error(exc)}") from None
    except OSError as exc:
        raise ArticleAudioGenerationError(f"{error_prefix}: {safe_api_error(exc)}") from None
    except InvalidAPIJSON:
        raise ArticleAudioGenerationError(f"{error_prefix}: invalid JSON response") from None

    if not isinstance(payload, dict):
        raise ArticleAudioGenerationError(f"{error_prefix}: response was not an object")
    return payload


def _parse_job_update(payload: dict[str, object]) -> ArticleAudioJobUpdate:
    try:
        job_id = wire_id(payload.get("id"))
        state = wire_state(payload.get("state"))
    except ValueError:
        raise ArticleAudioGenerationError("Invalid upstream job envelope") from None

    if state == "succeeded":
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ArticleAudioGenerationError("Article audio job result was missing")
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ArticleAudioGenerationError("Article audio job artifacts were missing")
        try:
            path = artifact_path(artifacts.get(DEFAULT_AUDIO_FORMAT))
        except ValueError:
            raise ArticleAudioGenerationError(
                "Article audio job did not expose an MP3 artifact"
            ) from None
        return ArticleAudioJobUpdate(job_id=job_id, state=state, artifact_path=path)

    if state == "failed":
        return ArticleAudioJobUpdate(
            job_id=job_id,
            state=state,
            error_message="Article audio job failed",
        )

    return ArticleAudioJobUpdate(job_id=job_id, state=state)


def _api_url(path: str) -> str:
    try:
        return api_url(settings.ARCHIVE_ARTICLE_AUDIO_API_BASE, path)
    except OSError as exc:
        raise ArticleAudioGenerationError(safe_api_error(exc)) from None
