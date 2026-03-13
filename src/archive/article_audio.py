from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from django.conf import settings

from archive.models import Item, ItemKind

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
    return item.kind == ItemKind.ARTICLE and bool(build_article_audio_script(item))


def build_article_audio_script(item: Item) -> str:
    if item.kind != ItemKind.ARTICLE:
        return ""

    body = item.long_summary.strip() or item.short_summary.strip() or item.notes.strip()
    if not body:
        return ""

    parts = [part for part in (item.title.strip(), body) if part]
    return "\n\n".join(parts).strip()


def generate_item_article_audio(item: Item, timeout: int = 30) -> ArticleAudioJobUpdate:
    api_key = settings.ARCHIVE_ARTICLE_AUDIO_API_KEY.strip()
    if not api_key:
        raise ArticleAudioGenerationError("Article audio generation is not configured")

    script = build_article_audio_script(item)
    if not script:
        raise ArticleAudioGenerationError("Item does not have article-audio source text yet")

    if item.article_audio_job_id.strip():
        return _poll_synthesis_job(job_id=item.article_audio_job_id.strip(), timeout=timeout)

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
            "task_ref": f"archive-item-{item.pk}-article-audio-v1",
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
