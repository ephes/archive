from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import uuid
from contextlib import closing
from dataclasses import dataclass
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings

from archive.classification import resolve_media_sources_for_item
from archive.media_archival import MediaArchivalError, open_archived_audio, open_archived_video
from archive.metadata import REQUEST_HEADERS
from archive.models import Item

MAX_TRANSCRIPTION_BYTES = 25 * 1024 * 1024
SUPPORTED_SUFFIXES = {
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}
CONTENT_TYPE_SUFFIXES = {
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mpga": ".mpga",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


class TranscriptionGenerationError(RuntimeError):
    pass


class _ArchivedAudioRequiresBatchUpload(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedMedia:
    filename: str
    content_type: str
    payload: bytes


@dataclass(frozen=True)
class TranscriptionSource:
    kind: str
    location: str
    content_type: str = ""
    size_bytes: int = 0


@dataclass(frozen=True)
class BatchTranscriptionJobUpdate:
    job_id: str
    state: str
    transcript: str = ""
    error_message: str = ""

    @property
    def is_complete(self) -> bool:
        return self.state == "succeeded" and bool(self.transcript)

    @property
    def is_pending(self) -> bool:
        return self.state in {"queued", "running"}


def can_transcribe_item(item: Item) -> bool:
    return _select_transcription_source(item) is not None


def generate_item_transcript(item: Item, timeout: int = 300) -> str:
    api_key = settings.ARCHIVE_TRANSCRIPTION_API_KEY.strip()
    if not api_key:
        raise TranscriptionGenerationError("Transcription is not configured")

    source = _select_transcription_source(item)
    if source is None:
        raise TranscriptionGenerationError("Item does not have a transcribable media source yet")

    prompt = _build_transcription_prompt(item=item)
    if source.kind == "archived_audio" and source.size_bytes > MAX_TRANSCRIPTION_BYTES:
        raw_transcript = _request_staged_archived_audio_transcription(
            item=item,
            source=source,
            timeout=timeout,
        )
    elif source.kind == "archived_video" and source.size_bytes > MAX_TRANSCRIPTION_BYTES:
        raise _oversized_archived_video_not_supported()
    else:
        try:
            media = _load_transcription_media(item=item, source=source, timeout=timeout)
        except _ArchivedAudioRequiresBatchUpload:
            raw_transcript = _request_staged_archived_audio_transcription(
                item=item,
                source=source,
                timeout=timeout,
            )
        else:
            raw_transcript = _request_transcription(media=media, prompt=prompt, timeout=timeout)
    transcript = _normalize_transcript(raw_transcript)
    if not transcript:
        raise TranscriptionGenerationError("Transcription response was empty")
    return transcript


def _select_transcription_source(item: Item) -> TranscriptionSource | None:
    archived_audio_path = item.archived_audio_path.strip()
    if archived_audio_path:
        return TranscriptionSource(
            kind="archived_audio",
            location=archived_audio_path,
            content_type=item.archived_audio_content_type.strip(),
            size_bytes=item.archived_audio_size_bytes,
        )

    archived_video_path = item.archived_video_path.strip()
    if archived_video_path:
        return TranscriptionSource(
            kind="archived_video",
            location=archived_video_path,
            content_type=item.archived_video_content_type.strip(),
            size_bytes=item.archived_video_size_bytes,
        )

    source_url = _select_remote_transcription_source_url(item)
    if source_url is None:
        return None
    return TranscriptionSource(kind="remote_url", location=source_url)


def _select_remote_transcription_source_url(item: Item) -> str | None:
    audio_source_url, video_source_url = resolve_media_sources_for_item(item)
    for candidate in (audio_source_url, video_source_url):
        if candidate:
            return candidate
    return None


def _load_transcription_media(
    *,
    item: Item,
    source: TranscriptionSource,
    timeout: int,
) -> DownloadedMedia:
    if source.kind == "remote_url":
        return _download_remote_media(source_url=source.location, timeout=timeout)
    return _read_archived_media(item=item, source=source)


def _looks_like_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(suffix) for suffix in SUPPORTED_SUFFIXES)


def _download_remote_media(source_url: str, timeout: int) -> DownloadedMedia:
    request = Request(
        source_url,
        headers={
            **REQUEST_HEADERS,
            "Accept": "audio/*,video/*;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = (response.headers.get_content_type() or "").lower()
            final_url = response.geturl() or source_url
            suffix = _detect_suffix(url=final_url, content_type=content_type)
            if not suffix:
                raise TranscriptionGenerationError(
                    f"Unsupported media type for transcription: {content_type or 'unknown'}"
                )
            payload = response.read(MAX_TRANSCRIPTION_BYTES + 1)
    except HTTPError as exc:
        raise TranscriptionGenerationError(f"Media download failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise TranscriptionGenerationError(f"Media download failed: {exc.reason}") from exc
    except OSError as exc:
        raise TranscriptionGenerationError(f"Media download failed: {exc}") from exc

    if len(payload) > MAX_TRANSCRIPTION_BYTES:
        raise TranscriptionGenerationError("Media source exceeded 25 MiB transcription limit")

    guessed_content_type = content_type or SUPPORTED_SUFFIXES[suffix]
    if guessed_content_type == "application/octet-stream":
        guessed_content_type = SUPPORTED_SUFFIXES[suffix]

    return DownloadedMedia(
        filename=f"transcription-input{suffix}",
        content_type=guessed_content_type,
        payload=payload,
    )


def _read_archived_media(item: Item, source: TranscriptionSource) -> DownloadedMedia:
    opener = open_archived_audio if source.kind == "archived_audio" else open_archived_video
    source_label = "audio" if source.kind == "archived_audio" else "video"
    content_type = source.content_type.lower()
    suffix = _detect_suffix(url=source.location, content_type=content_type)
    if not suffix:
        raise TranscriptionGenerationError(
            f"Unsupported archived {source_label} type for transcription: "
            f"{content_type or source.location or 'unknown'}"
        )

    if source.size_bytes > MAX_TRANSCRIPTION_BYTES:
        if source.kind == "archived_audio":
            raise _ArchivedAudioRequiresBatchUpload
        raise _oversized_archived_video_not_supported()

    try:
        with closing(opener(item)) as media_file:
            payload = media_file.read(MAX_TRANSCRIPTION_BYTES + 1)
    except MediaArchivalError as exc:
        raise TranscriptionGenerationError(str(exc)) from exc
    except OSError as exc:
        raise TranscriptionGenerationError(f"Archived {source_label} read failed: {exc}") from exc

    if len(payload) > MAX_TRANSCRIPTION_BYTES:
        if source.kind == "archived_audio":
            raise _ArchivedAudioRequiresBatchUpload
        raise _oversized_archived_video_not_supported()

    guessed_content_type = content_type or SUPPORTED_SUFFIXES[suffix]
    if guessed_content_type == "application/octet-stream":
        guessed_content_type = SUPPORTED_SUFFIXES[suffix]

    return DownloadedMedia(
        filename=f"transcription-input{suffix}",
        content_type=guessed_content_type,
        payload=payload,
    )


def _request_staged_archived_audio_transcription(
    *,
    item: Item,
    source: TranscriptionSource,
    timeout: int,
) -> str:
    upload_id = _stage_archived_audio_upload(item=item, source=source, timeout=timeout)
    update = _submit_batch_transcription_job(
        item=item,
        source=source,
        upload_id=upload_id,
        timeout=timeout,
    )
    completed = _wait_for_batch_transcription_job(update=update, timeout=timeout)
    return completed.transcript


def _stage_archived_audio_upload(
    *,
    item: Item,
    source: TranscriptionSource,
    timeout: int,
) -> str:
    suffix = _detect_suffix(url=source.location, content_type=source.content_type.lower())
    if not suffix:
        raise TranscriptionGenerationError(
            "Unsupported archived audio type for transcription: "
            f"{source.content_type or source.location or 'unknown'}"
        )

    filename = _transcription_source_filename(source=source, default_suffix=suffix)
    content_type = source.content_type.lower() or SUPPORTED_SUFFIXES[suffix]
    boundary = f"archive-{uuid.uuid4().hex}"
    request = Request(
        _api_url("uploads"),
        data=_iter_file_multipart_formdata(
            boundary=boundary,
            field_name="file",
            filename=filename,
            content_type=content_type,
            file_opener=lambda: open_archived_audio(item),
        ),
        headers={
            "Authorization": f"Bearer {settings.ARCHIVE_TRANSCRIPTION_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **_multipart_content_length_header(
                boundary=boundary,
                field_name="file",
                filename=filename,
                content_type=content_type,
                payload_size=source.size_bytes,
            ),
        },
        method="POST",
    )
    payload = _request_json(
        request=request,
        timeout=timeout,
        error_prefix="Archived audio staging failed",
    )
    upload_id = str(payload.get("id", "")).strip()
    if not upload_id:
        raise TranscriptionGenerationError("Archived audio staging response was incomplete")
    return upload_id


def _submit_batch_transcription_job(
    *,
    item: Item,
    source: TranscriptionSource,
    upload_id: str,
    timeout: int,
) -> BatchTranscriptionJobUpdate:
    request_body = json.dumps(
        {
            "job_type": "transcribe",
            "priority": "normal",
            "lane": "batch",
            "backend": "auto",
            "model": settings.ARCHIVE_TRANSCRIPTION_MODEL,
            "input": {"kind": "upload", "upload_id": upload_id},
            "output": {"formats": ["text", "json"]},
            "context": {"producer": "archive", "item_id": item.pk},
            "task_ref": _transcription_job_task_ref(item=item, source=source),
        }
    ).encode("utf-8")
    request = Request(
        _api_url("jobs"),
        data=request_body,
        headers={
            "Authorization": f"Bearer {settings.ARCHIVE_TRANSCRIPTION_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    payload = _request_json(
        request=request,
        timeout=timeout,
        error_prefix="Batch transcription job submission failed",
    )
    return _parse_batch_transcription_job_update(payload)


def _wait_for_batch_transcription_job(
    *,
    update: BatchTranscriptionJobUpdate,
    timeout: int,
) -> BatchTranscriptionJobUpdate:
    if update.is_complete:
        return update
    if not update.is_pending:
        raise TranscriptionGenerationError(update.error_message or "Batch transcription job failed")

    deadline = monotonic() + timeout
    current = update
    while current.is_pending:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TranscriptionGenerationError(
                f"Timed out waiting for batch transcription job {current.job_id}"
            )
        sleep(min(settings.ARCHIVE_TRANSCRIPTION_POLL_SECONDS, remaining))
        current = _poll_batch_transcription_job(
            job_id=current.job_id,
            timeout=max(1, int(deadline - monotonic())),
        )

    if current.is_complete:
        return current
    raise TranscriptionGenerationError(current.error_message or "Batch transcription job failed")


def _poll_batch_transcription_job(job_id: str, timeout: int) -> BatchTranscriptionJobUpdate:
    request = Request(
        _api_url(f"jobs/{job_id}"),
        headers={"Authorization": f"Bearer {settings.ARCHIVE_TRANSCRIPTION_API_KEY}"},
    )
    payload = _request_json(
        request=request,
        timeout=timeout,
        error_prefix="Batch transcription job status request failed",
    )
    return _parse_batch_transcription_job_update(payload)


def _parse_batch_transcription_job_update(
    payload: dict[str, object],
) -> BatchTranscriptionJobUpdate:
    job_id = str(payload.get("id", "")).strip()
    state = str(payload.get("state", "")).strip().lower()
    if not job_id or not state:
        raise TranscriptionGenerationError("Batch transcription job response was incomplete")

    if state == "succeeded":
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TranscriptionGenerationError("Batch transcription job result was missing")
        transcript = str(result.get("text", "")).strip()
        if not transcript:
            raise TranscriptionGenerationError(
                "Batch transcription job did not return transcript text"
            )
        return BatchTranscriptionJobUpdate(job_id=job_id, state=state, transcript=transcript)

    if state == "failed":
        error = payload.get("error")
        if isinstance(error, dict):
            error_message = str(error.get("message", "")).strip()
        else:
            error_message = ""
        return BatchTranscriptionJobUpdate(
            job_id=job_id,
            state=state,
            error_message=error_message or "Batch transcription job failed",
        )

    return BatchTranscriptionJobUpdate(job_id=job_id, state=state)


def _iter_file_multipart_formdata(
    *,
    boundary: str,
    field_name: str,
    filename: str,
    content_type: str,
    file_opener,
):
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield prefix
        try:
            with closing(file_opener()) as file_handle:
                while True:
                    chunk = file_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        except MediaArchivalError as exc:
            raise TranscriptionGenerationError(str(exc)) from exc
        except OSError as exc:
            raise TranscriptionGenerationError(f"Archived audio read failed: {exc}") from exc
        yield suffix

    return body()


def _multipart_content_length_header(
    *,
    boundary: str,
    field_name: str,
    filename: str,
    content_type: str,
    payload_size: int,
) -> dict[str, str]:
    if payload_size <= 0:
        return {}
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    return {"Content-Length": str(len(prefix) + payload_size + len(suffix))}


def _transcription_source_filename(*, source: TranscriptionSource, default_suffix: str) -> str:
    location = source.location.strip()
    parsed_path = urlparse(location).path
    filename = parsed_path.rsplit("/", 1)[-1] if parsed_path else location.rsplit("/", 1)[-1]
    return filename or f"transcription-input{default_suffix}"


def _transcription_job_task_ref(*, item: Item, source: TranscriptionSource) -> str:
    source_fingerprint = hashlib.sha256(
        "|".join(
            (
                source.kind,
                source.location.strip(),
                source.content_type.strip(),
                str(source.size_bytes),
            )
        ).encode()
    ).hexdigest()[:16]
    return f"archive-item-{item.pk}-transcript-{source_fingerprint}"


def _oversized_archived_video_not_supported() -> TranscriptionGenerationError:
    return TranscriptionGenerationError(
        "Archived video exceeded 25 MiB sync transcription limit, and Voxhelm batch "
        "uploaded video transcription is not supported yet"
    )


def _detect_suffix(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for suffix in SUPPORTED_SUFFIXES:
        if path.endswith(suffix):
            return suffix

    if content_type in CONTENT_TYPE_SUFFIXES:
        return CONTENT_TYPE_SUFFIXES[content_type]

    guessed_suffix = mimetypes.guess_extension(content_type or "", strict=False) or ""
    if guessed_suffix in SUPPORTED_SUFFIXES:
        return guessed_suffix
    return ""


def _build_transcription_prompt(item: Item) -> str:
    context = [
        "Transcribe the spoken audio faithfully in plain text.",
        "Preserve paragraph breaks when they are obvious from the speech.",
    ]
    if item.title.strip():
        context.append(f"Title: {item.title.strip()}")
    if item.source.strip():
        context.append(f"Source: {item.source.strip()}")
    if item.author.strip():
        context.append(f"Author: {item.author.strip()}")
    return "\n".join(context)


def _request_transcription(media: DownloadedMedia, prompt: str, timeout: int) -> str:
    fields = {
        "model": settings.ARCHIVE_TRANSCRIPTION_MODEL,
    }
    if prompt.strip():
        fields["prompt"] = prompt.strip()

    boundary, request_body = _encode_multipart_formdata(fields=fields, media=media)
    request = Request(
        _api_url("audio/transcriptions"),
        data=request_body,
        headers={
            "Authorization": f"Bearer {settings.ARCHIVE_TRANSCRIPTION_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
            content_type = (response.headers.get_content_type() or "").lower()
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise TranscriptionGenerationError(
            f"Transcription API request failed: HTTP {exc.code}: {message}"
        ) from exc
    except URLError as exc:
        raise TranscriptionGenerationError(
            f"Transcription API request failed: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise TranscriptionGenerationError(f"Transcription API request failed: {exc}") from exc

    if "json" not in content_type:
        return payload

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TranscriptionGenerationError("Transcription API returned invalid JSON") from exc

    transcript = parsed.get("text")
    if not isinstance(transcript, str):
        raise TranscriptionGenerationError(
            "Transcription API response did not include transcript text"
        )
    return transcript


def _encode_multipart_formdata(fields: dict[str, str], media: DownloadedMedia) -> tuple[str, bytes]:
    boundary = f"archive-{uuid.uuid4().hex}"
    body: list[bytes] = []
    for key, value in fields.items():
        body.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    body.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{media.filename}"\r\n'
            ).encode(),
            f"Content-Type: {media.content_type}\r\n\r\n".encode(),
            media.payload,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return boundary, b"".join(body)


def _request_json(request: Request, timeout: int, error_prefix: str) -> dict[str, object]:
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise TranscriptionGenerationError(f"{error_prefix}: HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise TranscriptionGenerationError(f"{error_prefix}: {exc.reason}") from exc
    except OSError as exc:
        raise TranscriptionGenerationError(f"{error_prefix}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TranscriptionGenerationError(f"{error_prefix}: invalid JSON response") from exc

    if not isinstance(payload, dict):
        raise TranscriptionGenerationError(f"{error_prefix}: response was not an object")
    return payload


def _api_url(path: str) -> str:
    base_url = settings.ARCHIVE_TRANSCRIPTION_API_BASE.rstrip("/")
    normalized_path = path.strip()
    if normalized_path.startswith(("https://", "http://")):
        return normalized_path
    if normalized_path.startswith("/"):
        parsed = urlsplit(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise TranscriptionGenerationError("Transcription API base must be an absolute URL")
        return f"{parsed.scheme}://{parsed.netloc}{normalized_path}"
    return f"{base_url}/{normalized_path.lstrip('/')}"


def _normalize_transcript(value: str) -> str:
    cleaned_lines: list[str] = []
    saw_blank = False
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not normalized:
            if cleaned_lines and not saw_blank:
                cleaned_lines.append("")
            saw_blank = True
            continue
        cleaned_lines.append(normalized)
        saw_blank = False
    return "\n".join(cleaned_lines).strip()
