from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import re
import uuid
from contextlib import closing
from dataclasses import dataclass
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from django.conf import settings

from archive.api_responses import (
    InvalidAPIJSON,
    decode_api_json,
    read_api_response,
    safe_api_error,
)
from archive.classification import resolve_media_sources_for_item
from archive.media_archival import (
    DIRECT_MEDIA_BUDGET_EXCEEDED,
    MediaArchivalError,
    MediaSizeLimitError,
    _open_direct_media,
    open_archived_audio,
    open_archived_video,
)
from archive.metadata import REQUEST_HEADERS
from archive.models import Item
from archive.transcription_config import transcription_max_gap_seconds
from archive.upstream_http import api_url
from archive.upstream_http import open_authenticated_url as urlopen
from archive.upstream_jobs import artifact_path, wire_id, wire_state

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


class TranscriptionJobFailed(TranscriptionGenerationError):
    """The batch job can never yield a usable transcript; a retry needs a new job."""


class TranscriptionPending(Exception):  # noqa: N818 - a state signal, not an error
    """A Voxhelm batch job is still running; poll it again on a later pass."""

    def __init__(self, job: BatchTranscriptionJob, *, poll_error: str = "") -> None:
        super().__init__(f"Batch transcription job {job.job_id} is still running")
        self.job = job
        # Set when this pass could not read the job's status or result evidence.
        self.poll_error = poll_error


class _ArchivedAudioRequiresBatchUpload(RuntimeError):
    pass


@dataclass(frozen=True)
class BatchTranscriptionJob:
    """A submitted Voxhelm batch job and the task_ref of the source it transcribes."""

    job_id: str
    job_ref: str


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
    json_artifact_path: str = ""

    @property
    def is_complete(self) -> bool:
        return self.state == "succeeded" and bool(self.transcript)

    @property
    def is_pending(self) -> bool:
        return self.state in {"queued", "running"}


def can_transcribe_item(item: Item) -> bool:
    return _select_transcription_source(item) is not None


def generate_item_transcript(
    item: Item,
    timeout: float = 300,
    *,
    batch_job: BatchTranscriptionJob | None = None,
) -> str:
    """Return the item's transcript, or raise ``TranscriptionPending`` for a running job.

    Oversized archived audio goes to a Voxhelm batch job. This call uploads the audio
    and submits the job, but never waits for it: it raises ``TranscriptionPending``
    with the job, which the caller stores and passes back as ``batch_job`` on a later
    pass. That pass makes one status request. A job made for an older source is
    ignored and the current source is transcribed afresh.
    """
    transcription_max_gap_seconds(settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS)
    deadline = monotonic() + timeout
    api_key = settings.ARCHIVE_TRANSCRIPTION_API_KEY.strip()
    if not api_key:
        raise TranscriptionGenerationError("Transcription is not configured")

    source = _select_transcription_source(item)
    if source is None:
        raise TranscriptionGenerationError("Item does not have a transcribable media source yet")

    if batch_job is not None and batch_job.job_ref == _transcription_job_task_ref(
        item=item, source=source
    ):
        return _poll_stored_batch_transcription_job(job=batch_job, deadline=deadline)

    prompt = _build_transcription_prompt(item=item)
    if source.kind == "archived_audio" and source.size_bytes > MAX_TRANSCRIPTION_BYTES:
        raw_transcript = _request_staged_archived_audio_transcription(
            item=item,
            source=source,
            timeout=_remaining(deadline),
            deadline=deadline,
        )
    elif source.kind == "archived_video" and source.size_bytes > MAX_TRANSCRIPTION_BYTES:
        raise _oversized_archived_video_not_supported()
    else:
        try:
            media = _load_transcription_media(
                item=item, source=source, timeout=_remaining(deadline), deadline=deadline
            )
        except _ArchivedAudioRequiresBatchUpload:
            raw_transcript = _request_staged_archived_audio_transcription(
                item=item,
                source=source,
                timeout=_remaining(deadline),
                deadline=deadline,
            )
        else:
            raw_transcript = _request_transcription(
                media=media, prompt=prompt, timeout=_remaining(deadline), deadline=deadline
            )
    _remaining(deadline)
    transcript = _normalize_transcript(raw_transcript)
    if not transcript:
        raise TranscriptionGenerationError("Transcription response was empty")
    _remaining(deadline)
    return transcript


def transcription_job_ref(item: Item) -> str:
    """Return the batch task_ref of the item's current source, or "" without a source."""
    source = _select_transcription_source(item)
    return "" if source is None else _transcription_job_task_ref(item=item, source=source)


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TranscriptionGenerationError("Transcription pipeline timeout exceeded")
    return remaining


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
        if candidate and _looks_like_media_url(candidate):
            return candidate
    return None


def _load_transcription_media(
    *,
    item: Item,
    source: TranscriptionSource,
    timeout: float,
    deadline: float | None = None,
) -> DownloadedMedia:
    if source.kind == "remote_url":
        return _download_remote_media(
            source_url=source.location, timeout=timeout, deadline=deadline
        )
    return _read_archived_media(item=item, source=source)


def _looks_like_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(suffix) for suffix in SUPPORTED_SUFFIXES)


def _download_remote_media(
    source_url: str, timeout: float, deadline: float | None = None
) -> DownloadedMedia:
    request = Request(
        source_url,
        headers={
            **REQUEST_HEADERS,
            "Accept": "audio/*,video/*;q=0.9,*/*;q=0.8",
        },
    )
    budget = _remaining(deadline) if deadline is not None else timeout
    try:
        # A disposable child process performs the fetch and is killed at the
        # remaining pipeline budget, so DNS, redirects and drip-fed bodies all
        # count against the one elapsed deadline.
        with _open_direct_media(
            request, timeout=budget, max_bytes=MAX_TRANSCRIPTION_BYTES
        ) as response:
            content_type = (response.headers.get_content_type() or "").lower()
            final_url = response.geturl() or source_url
            suffix = _detect_suffix(url=final_url, content_type=content_type)
            if not suffix:
                raise TranscriptionGenerationError(
                    f"Unsupported media type for transcription: {content_type or 'unknown'}"
                )
            payload = response.read(MAX_TRANSCRIPTION_BYTES + 1)
    except MediaSizeLimitError:
        raise TranscriptionGenerationError(
            "Media source exceeded 25 MiB transcription limit"
        ) from None
    except MediaArchivalError as exc:
        raise TranscriptionGenerationError(_safe_download_failure(exc)) from None
    except OSError as exc:
        raise TranscriptionGenerationError(
            f"Media download failed: {safe_api_error(exc)}"
        ) from None

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


def _safe_download_failure(exc: MediaArchivalError) -> str:
    # The downloader's raw messages can name hosts or upstream text; only fixed
    # texts reach the stored transcription error.
    if exc.safe_detail:
        return f"Media download failed: {exc.safe_detail}"
    if str(exc) == DIRECT_MEDIA_BUDGET_EXCEEDED:
        return DIRECT_MEDIA_BUDGET_EXCEEDED
    return "Media download failed: transport error"


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
        raise TranscriptionGenerationError(
            f"Archived {source_label} read failed: {safe_api_error(exc)}"
        ) from None

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
    timeout: float,
    deadline: float | None = None,
) -> str:
    if deadline is None:
        deadline = monotonic() + timeout
    upload_id = _stage_archived_audio_upload(
        item=item, source=source, timeout=_remaining(deadline), deadline=deadline
    )
    update = _submit_batch_transcription_job(
        item=item,
        source=source,
        upload_id=upload_id,
        timeout=_remaining(deadline),
        deadline=deadline,
    )
    if update.is_pending:
        raise TranscriptionPending(
            BatchTranscriptionJob(
                job_id=update.job_id,
                job_ref=_transcription_job_task_ref(item=item, source=source),
            )
        )
    return _finish_batch_transcription_job(update=update, deadline=deadline)


def _poll_stored_batch_transcription_job(*, job: BatchTranscriptionJob, deadline: float) -> str:
    """One status request for a stored job; unreadable status or evidence stays pending."""
    try:
        update = _poll_batch_transcription_job(
            job_id=job.job_id, timeout=_remaining(deadline), deadline=deadline
        )
        if update.is_pending:
            raise TranscriptionPending(job)
        transcript = _normalize_transcript(
            _finish_batch_transcription_job(update=update, deadline=deadline)
        )
        # Admission is the last step of the pass: an expired pass keeps the job.
        _remaining(deadline)
    except (TranscriptionPending, TranscriptionJobFailed):
        raise
    except TranscriptionGenerationError as exc:
        raise TranscriptionPending(job, poll_error=str(exc)) from None
    if not transcript:
        raise TranscriptionJobFailed(f"Batch transcription job {job.job_id} returned no text")
    return transcript


def _finish_batch_transcription_job(*, update: BatchTranscriptionJobUpdate, deadline: float) -> str:
    if not update.is_complete:
        raise TranscriptionJobFailed(
            update.error_message or f"Batch transcription job {update.job_id} failed"
        )
    _check_batch_transcript_coverage(
        completed=update, timeout=_remaining(deadline), deadline=deadline
    )
    _remaining(deadline)
    return update.transcript


def _check_batch_transcript_coverage(
    *,
    completed: BatchTranscriptionJobUpdate,
    timeout: float,
    deadline: float | None = None,
) -> None:
    """Reject a batch transcript whose segments leave a long stretch of audio uncovered.

    whisper.cpp splits long audio across processors and ignores a failing worker thread:
    the job still succeeds, but whole parts of the audio are missing from the text.
    """
    max_gap_seconds = transcription_max_gap_seconds(settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS)
    if max_gap_seconds <= 0:
        return
    if not completed.json_artifact_path:
        raise TranscriptionJobFailed(
            f"Batch transcription job {completed.job_id} is missing its JSON segment artifact; "
            "retry with a service that provides segment evidence, or explicitly disable "
            "coverage checking with ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS<=0"
        )

    request = Request(
        _api_url(completed.json_artifact_path),
        headers={"Authorization": f"Bearer {settings.ARCHIVE_TRANSCRIPTION_API_KEY}"},
    )
    payload = _request_json(
        request=request,
        timeout=timeout,
        deadline=deadline,
        error_prefix="Batch transcription segment download failed",
        # A finished job's artifact does not change; only transport errors may pass.
        invalid_payload_error=TranscriptionJobFailed,
    )
    artifact_text = payload.get("text")
    if (
        not isinstance(artifact_text, str)
        or not _normalize_transcript(artifact_text)
        or _normalize_transcript(artifact_text) != _normalize_transcript(completed.transcript)
    ):
        raise TranscriptionJobFailed("Batch transcription artifact text does not match its result")
    # Verbose JSON need not carry an ID, but supplied job identity must agree.
    for key in ("job_id", "id"):
        if key not in payload:
            continue
        try:
            artifact_job_id = wire_id(payload[key])
        except ValueError:
            raise TranscriptionJobFailed(
                "Batch transcription artifact job identity is invalid"
            ) from None
        if artifact_job_id != completed.job_id:
            raise TranscriptionJobFailed("Batch transcription artifact job identity does not match")
    try:
        gaps = find_transcript_gaps(payload.get("segments"), max_gap_seconds=max_gap_seconds)
    except TranscriptionGenerationError as exc:
        raise TranscriptionJobFailed(f"Batch transcription evidence: {exc}") from None
    if not gaps:
        return
    start, end = max(gaps, key=lambda gap: gap[1] - gap[0])
    raise TranscriptionJobFailed(
        f"Batch transcription job {completed.job_id} is incomplete: {len(gaps)} stretch(es) "
        f"of audio longer than {max_gap_seconds:g} s have no transcript, the longest from "
        f"{_format_seconds(start)} to {_format_seconds(end)}"
    )


def find_transcript_gaps(
    segments: object,
    *,
    max_gap_seconds: float,
) -> list[tuple[float, float]]:
    """Return ``(start, end)`` stretches longer than ``max_gap_seconds`` without a segment.

    Reject malformed evidence; measure coverage from the union without reordering text.
    """
    max_gap_seconds = transcription_max_gap_seconds(max_gap_seconds)
    if max_gap_seconds <= 0:
        return []
    if not isinstance(segments, list) or not segments:
        raise TranscriptionGenerationError("Batch transcription segment list was unreadable")
    intervals: list[tuple[float, float]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise TranscriptionGenerationError("Batch transcription segment was unreadable")
        start, end = _seconds(segment.get("start")), _seconds(segment.get("end"))
        if start is None or end is None or start > end:
            raise TranscriptionGenerationError("Batch transcription segment times were invalid")
        intervals.append((start, end))

    gaps: list[tuple[float, float]] = []
    covered_until = 0.0
    for start, end in sorted(intervals):
        if start - covered_until > max_gap_seconds:
            gaps.append((covered_until, start))
        covered_until = max(covered_until, end)
    return gaps


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _format_seconds(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _stage_archived_audio_upload(
    *,
    item: Item,
    source: TranscriptionSource,
    timeout: float,
    deadline: float | None = None,
) -> str:
    if deadline is None:
        deadline = monotonic() + timeout
    suffix = _detect_suffix(url=source.location, content_type=source.content_type.lower())
    if not suffix:
        raise TranscriptionGenerationError("Unsupported archived audio type for transcription")

    if not 0 < source.size_bytes <= settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES:
        raise TranscriptionGenerationError("Archived audio staging length exceeds archive limit")
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
            expected_size=source.size_bytes,
            deadline=deadline,
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
        deadline=deadline,
        error_prefix="Archived audio staging failed",
    )
    try:
        return wire_id(payload.get("id"))
    except ValueError:
        raise TranscriptionGenerationError("Invalid upstream upload identifier") from None


def _submit_batch_transcription_job(
    *,
    item: Item,
    source: TranscriptionSource,
    upload_id: str,
    timeout: float,
    deadline: float | None = None,
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
        deadline=deadline,
        error_prefix="Batch transcription job submission failed",
    )
    return _parse_batch_transcription_job_update(payload)


def _poll_batch_transcription_job(
    job_id: str, timeout: float, deadline: float | None = None
) -> BatchTranscriptionJobUpdate:
    try:
        job_id = wire_id(job_id)
    except ValueError:
        raise TranscriptionGenerationError("Invalid upstream job identifier") from None
    request = Request(
        _api_url(f"jobs/{job_id}"),
        headers={"Authorization": f"Bearer {settings.ARCHIVE_TRANSCRIPTION_API_KEY}"},
    )
    payload = _request_json(
        request=request,
        timeout=timeout,
        deadline=deadline,
        error_prefix="Batch transcription job status request failed",
    )
    # Check identity first: another job's result must not decide this job's fate.
    try:
        polled_id = wire_id(payload.get("id"))
    except ValueError:
        raise TranscriptionGenerationError("Invalid upstream job envelope") from None
    if polled_id != job_id:
        raise TranscriptionGenerationError("Upstream poll job identity mismatch")
    return _parse_batch_transcription_job_update(payload)


def _parse_batch_transcription_job_update(
    payload: dict[str, object],
) -> BatchTranscriptionJobUpdate:
    try:
        job_id = wire_id(payload.get("id"))
        state = wire_state(payload.get("state"))
    except ValueError:
        raise TranscriptionGenerationError("Invalid upstream job envelope") from None

    if state == "succeeded":
        # A succeeded job keeps its result, so a malformed one never becomes usable.
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TranscriptionJobFailed("Batch transcription job result was missing")
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionJobFailed("Batch transcription job did not return transcript text")
        artifacts = result.get("artifacts")
        json_artifact_path = ""
        # Explicit nulls mean "no artifact", as for older services; only a
        # present value of the wrong type is a malformed envelope.
        if artifacts is not None and not isinstance(artifacts, dict):
            raise TranscriptionJobFailed("Invalid JSON segment artifact container")
        if isinstance(artifacts, dict) and artifacts.get("json") is not None:
            try:
                json_artifact_path = artifact_path(artifacts["json"])
            except ValueError:
                raise TranscriptionJobFailed("Invalid JSON segment artifact path") from None
        return BatchTranscriptionJobUpdate(
            job_id=job_id,
            state=state,
            transcript=text.strip(),
            json_artifact_path=json_artifact_path,
        )

    if state == "failed":
        return BatchTranscriptionJobUpdate(
            job_id=job_id,
            state=state,
            error_message="Batch transcription job failed",
        )

    return BatchTranscriptionJobUpdate(job_id=job_id, state=state)


def _multipart_prefix(*, boundary: str, field_name: str, filename: str, content_type: str) -> bytes:
    # Unicode filenames are UTF-8, as for the existing browser-style multipart wire.
    if (
        not filename
        or len(filename) > 255
        or any(ord(char) < 32 or ord(char) == 127 or char in '"\\' for char in filename)
        or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type)
        or field_name != "file"
        or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", boundary)
    ):
        raise TranscriptionGenerationError("Invalid multipart filename or content type")
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()


def _iter_file_multipart_formdata(
    *,
    boundary: str,
    field_name: str,
    filename: str,
    content_type: str,
    file_opener,
    expected_size: int | None = None,
    deadline: float | None = None,
):
    prefix = _multipart_prefix(
        boundary=boundary, field_name=field_name, filename=filename, content_type=content_type
    )
    suffix = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield prefix
        total = 0
        try:
            if deadline is not None:
                _remaining(deadline)
            with closing(file_opener()) as file_handle:
                while True:
                    if deadline is not None:
                        _remaining(deadline)
                    # Read at most one extra byte to detect a replaced/growing object.
                    limit = settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES
                    budget = min(expected_size, limit) if expected_size is not None else limit
                    chunk = file_handle.read(min(1024 * 1024, budget - total + 1))
                    if deadline is not None:
                        _remaining(deadline)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > budget:
                        raise TranscriptionGenerationError("Archived audio staging length mismatch")
                    yield chunk
                if expected_size is not None and total != expected_size:
                    raise TranscriptionGenerationError("Archived audio staging length mismatch")
        except MediaArchivalError as exc:
            raise TranscriptionGenerationError(str(exc)) from exc
        except OSError as exc:
            raise TranscriptionGenerationError(
                f"Archived audio read failed: {safe_api_error(exc)}"
            ) from None
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
    prefix = _multipart_prefix(
        boundary=boundary, field_name=field_name, filename=filename, content_type=content_type
    )
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


def _request_transcription(
    media: DownloadedMedia, prompt: str, timeout: float, deadline: float | None = None
) -> str:
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
        with urlopen(
            request, timeout=_remaining(deadline) if deadline is not None else timeout
        ) as response:
            payload = read_api_response(response)
            content_type = (response.headers.get_content_type() or "").lower()
    except HTTPError as exc:
        raise TranscriptionGenerationError(
            f"Transcription API request failed: {safe_api_error(exc)}"
        ) from None
    except URLError as exc:
        raise TranscriptionGenerationError(
            f"Transcription API request failed: {safe_api_error(exc)}"
        ) from None
    except OSError as exc:
        raise TranscriptionGenerationError(
            f"Transcription API request failed: {safe_api_error(exc)}"
        ) from None

    if "json" not in content_type:
        return payload.decode("utf-8", errors="replace")

    try:
        parsed = decode_api_json(payload)
    except InvalidAPIJSON:
        raise TranscriptionGenerationError("Transcription API returned invalid JSON") from None

    transcript = parsed.get("text") if isinstance(parsed, dict) else None
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
            _multipart_prefix(
                boundary=boundary,
                field_name="file",
                filename=media.filename,
                content_type=media.content_type,
            ),
            media.payload,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return boundary, b"".join(body)


def _request_json(
    request: Request,
    timeout: float,
    error_prefix: str,
    deadline: float | None = None,
    *,
    invalid_payload_error: type[TranscriptionGenerationError] = TranscriptionGenerationError,
) -> dict[str, object]:
    try:
        with urlopen(
            request, timeout=_remaining(deadline) if deadline is not None else timeout
        ) as response:
            payload = decode_api_json(read_api_response(response))
    except HTTPError as exc:
        raise TranscriptionGenerationError(f"{error_prefix}: {safe_api_error(exc)}") from None
    except URLError as exc:
        raise TranscriptionGenerationError(f"{error_prefix}: {safe_api_error(exc)}") from None
    except OSError as exc:
        raise TranscriptionGenerationError(f"{error_prefix}: {safe_api_error(exc)}") from None
    except InvalidAPIJSON:
        raise invalid_payload_error(f"{error_prefix}: invalid JSON response") from None

    if deadline is not None:
        _remaining(deadline)
    if not isinstance(payload, dict):
        raise invalid_payload_error(f"{error_prefix}: response was not an object")
    return payload


def _api_url(path: str) -> str:
    try:
        return api_url(settings.ARCHIVE_TRANSCRIPTION_API_BASE, path)
    except OSError as exc:
        raise TranscriptionGenerationError(safe_api_error(exc)) from None


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
