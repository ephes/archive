from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from http.client import HTTPException
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import models, transaction

from archive.api_responses import read_api_error, read_api_response
from archive.media_archival import save_reserved_media_file
from archive.media_storage import (
    delete_unreferenced_archive_media_paths,
    release_pending_archive_media_paths,
)
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.summaries import MAX_ARTICLE_AUDIO_SOURCE_CHARS, extract_summary_source_from_url

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

    body = (
        source_text.strip()
        or item.long_summary.strip()
        or item.short_summary.strip()
        or item.notes.strip()
    )
    if not body:
        return ""

    parts = [part for part in (item.title.strip(), body) if part]
    script = "\n\n".join(parts).strip()
    if max_chars is not None:
        script = _truncate_script(script, max_chars=max_chars)
    return script


def generate_item_article_audio(item: Item, timeout: int = 30) -> ArticleAudioJobUpdate:
    api_key = settings.ARCHIVE_ARTICLE_AUDIO_API_KEY.strip()
    if not api_key:
        raise ArticleAudioGenerationError("Article audio generation is not configured")

    if item.article_audio_job_id.strip():
        return _poll_synthesis_job(job_id=item.article_audio_job_id.strip(), timeout=timeout)

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

    return _submit_synthesis_job(item=item, text=script, timeout=timeout)


def store_generated_article_audio(item: Item, timeout: int = 30) -> None:
    """Download to a disk temporary file, then publish one reserved storage object.

    A conditional database update prevents concurrent requests from replacing an
    already published copy. Successful publication resets legacy generation failure
    state and refreshes that state on the caller's item. Losers discard their upload
    through the normal media deletion guard. No storage object is written until
    the download is complete.
    """
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
                with urlopen(request, timeout=timeout) as response:
                    content_type = response.headers.get_content_type().lower()
                    if content_type not in {"audio/mpeg", "audio/mp3"}:
                        raise ArticleAudioGenerationError("Article audio artifact is not MP3 audio")
                    raw_length = response.headers.get("Content-Length")
                    expected_size = int(raw_length) if raw_length is not None else None
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
                )
            with transaction.atomic(using=using, durable=True):
                published = (
                    Item.objects.using(using)
                    .filter(
                        pk=item.pk,
                        article_audio_artifact_path=item.article_audio_artifact_path,
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
        message = read_api_error(exc)
        raise ArticleAudioGenerationError(
            f"Article audio artifact request failed: HTTP {exc.code}: {message}"
        ) from exc
    except (OSError, HTTPException) as exc:
        raise ArticleAudioGenerationError(f"Article audio artifact request failed: {exc}") from exc
    except Exception as exc:
        if isinstance(exc, ArticleAudioGenerationError):
            raise
        raise ArticleAudioGenerationError(
            "Article audio storage is temporarily unavailable"
        ) from exc


def _truncate_script(value: str, *, max_chars: int) -> str:
    normalized = value.strip()
    if len(normalized) <= max_chars:
        return normalized

    truncated = normalized[:max_chars].rstrip()
    whitespace_positions = [truncated.rfind(char) for char in (" ", "\n", "\t")]
    last_whitespace = max(whitespace_positions)
    if last_whitespace > 0:
        return truncated[:last_whitespace].rstrip()
    return truncated


def _task_ref_for_script(item: Item, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"archive-item-{item.pk}-article-audio-v2-{digest}"


def _best_effort_article_audio_source_text(item: Item, timeout: int) -> str:
    try:
        source = extract_summary_source_from_url(
            item.original_url,
            timeout=timeout,
            max_chars=MAX_ARTICLE_AUDIO_SOURCE_CHARS,
        )
    except Exception as exc:
        logger.warning("Article audio source extraction failed for item %s: %s", item.pk, exc)
        return ""
    return source.extracted_text.strip()


def _submit_synthesis_job(item: Item, text: str, timeout: int) -> ArticleAudioJobUpdate:
    request_body = json.dumps(
        {
            "job_type": "synthesize",
            "priority": "normal",
            "lane": "batch",
            "backend": "auto",
            "model": settings.ARCHIVE_ARTICLE_AUDIO_MODEL,
            "language": settings.ARCHIVE_ARTICLE_AUDIO_LANGUAGE,
            "voice": settings.ARCHIVE_ARTICLE_AUDIO_VOICE,
            "input": {
                "kind": "text",
                "text": text,
            },
            "output": {"formats": [DEFAULT_AUDIO_FORMAT]},
            "context": {"producer": "archive", "item_id": item.pk},
            "task_ref": _task_ref_for_script(item=item, text=text),
        }
    ).encode("utf-8")
    request = Request(
        _api_url("jobs"),
        data=request_body,
        headers={
            "Authorization": f"Bearer {settings.ARCHIVE_ARTICLE_AUDIO_API_KEY}",
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


def _poll_synthesis_job(job_id: str, timeout: int) -> ArticleAudioJobUpdate:
    request = Request(
        _api_url(f"jobs/{job_id}"),
        headers={"Authorization": f"Bearer {settings.ARCHIVE_ARTICLE_AUDIO_API_KEY}"},
    )
    payload = _request_json(
        request=request,
        timeout=timeout,
        error_prefix="Article audio job status request failed",
    )
    return _parse_job_update(payload)


def _request_json(request: Request, timeout: int, error_prefix: str) -> dict[str, object]:
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(read_api_response(response).decode("utf-8"))
    except HTTPError as exc:
        message = read_api_error(exc)
        raise ArticleAudioGenerationError(f"{error_prefix}: HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise ArticleAudioGenerationError(f"{error_prefix}: {exc.reason}") from exc
    except OSError as exc:
        raise ArticleAudioGenerationError(f"{error_prefix}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ArticleAudioGenerationError(f"{error_prefix}: invalid JSON response") from exc

    if not isinstance(payload, dict):
        raise ArticleAudioGenerationError(f"{error_prefix}: response was not an object")
    return payload


def _parse_job_update(payload: dict[str, object]) -> ArticleAudioJobUpdate:
    job_id = str(payload.get("id", "")).strip()
    state = str(payload.get("state", "")).strip().lower()
    if not job_id or not state:
        raise ArticleAudioGenerationError("Article audio job response was incomplete")

    if state == "succeeded":
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ArticleAudioGenerationError("Article audio job result was missing")
        artifacts = result.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ArticleAudioGenerationError("Article audio job artifacts were missing")
        artifact_path = str(artifacts.get(DEFAULT_AUDIO_FORMAT, "")).strip()
        if not artifact_path:
            raise ArticleAudioGenerationError("Article audio job did not expose an MP3 artifact")
        return ArticleAudioJobUpdate(job_id=job_id, state=state, artifact_path=artifact_path)

    if state == "failed":
        error = payload.get("error")
        if isinstance(error, dict):
            error_message = str(error.get("message", "")).strip()
        else:
            error_message = ""
        return ArticleAudioJobUpdate(
            job_id=job_id,
            state=state,
            error_message=error_message or "Article audio job failed",
        )

    return ArticleAudioJobUpdate(job_id=job_id, state=state)


def _api_url(path: str) -> str:
    base_url = settings.ARCHIVE_ARTICLE_AUDIO_API_BASE.rstrip("/")
    normalized_path = path.strip()
    if normalized_path.startswith(("https://", "http://")):
        return normalized_path
    if normalized_path.startswith("/"):
        parsed = urlsplit(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ArticleAudioGenerationError("Article audio API base must be an absolute URL")
        return f"{parsed.scheme}://{parsed.netloc}{normalized_path}"
    return f"{base_url}/{normalized_path.lstrip('/')}"
