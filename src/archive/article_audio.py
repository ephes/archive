from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from django.conf import settings

from archive.models import Item, ItemKind
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


@dataclass(frozen=True)
class DownloadedArticleAudio:
    content_type: str
    payload: bytes


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


def download_generated_article_audio(item: Item, timeout: int = 30) -> DownloadedArticleAudio:
    api_key = settings.ARCHIVE_ARTICLE_AUDIO_API_KEY.strip()
    if not api_key:
        raise ArticleAudioGenerationError("Article audio download is not configured")
    artifact_path = item.article_audio_artifact_path.strip()
    if not artifact_path:
        raise ArticleAudioGenerationError("Item does not have generated article audio")

    request = Request(
        _api_url(artifact_path),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES + 1)
            content_type = (
                (response.headers.get_content_type() or "").lower() or DEFAULT_AUDIO_CONTENT_TYPE
            )
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ArticleAudioGenerationError(
            f"Article audio artifact request failed: HTTP {exc.code}: {message}"
        ) from exc
    except URLError as exc:
        raise ArticleAudioGenerationError(
            f"Article audio artifact request failed: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise ArticleAudioGenerationError(f"Article audio artifact request failed: {exc}") from exc

    if len(payload) > settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES:
        raise ArticleAudioGenerationError(
            "Article audio artifact exceeded the configured "
            f"{settings.ARCHIVE_ARTICLE_AUDIO_MAX_BYTES}-byte download limit"
        )

    return DownloadedArticleAudio(content_type=content_type, payload=payload)


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
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise ArticleAudioGenerationError(
            f"{error_prefix}: HTTP {exc.code}: {message}"
        ) from exc
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
