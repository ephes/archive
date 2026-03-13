from __future__ import annotations

import json
import mimetypes
import re
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings

from archive.metadata import REQUEST_HEADERS
from archive.models import Item, ItemKind

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


@dataclass(frozen=True)
class DownloadedMedia:
    filename: str
    content_type: str
    payload: bytes


def can_transcribe_item(item: Item) -> bool:
    return _select_transcription_source_url(item) is not None


def generate_item_transcript(item: Item, timeout: int = 300) -> str:
    api_key = settings.ARCHIVE_TRANSCRIPTION_API_KEY.strip()
    if not api_key:
        raise TranscriptionGenerationError("Transcription is not configured")

    source_url = _select_transcription_source_url(item)
    if source_url is None:
        raise TranscriptionGenerationError("Item does not have a transcribable media source yet")

    media = _download_media(source_url=source_url, timeout=timeout)
    prompt = _build_transcription_prompt(item=item)
    raw_transcript = _request_transcription(media=media, prompt=prompt, timeout=timeout)
    transcript = _normalize_transcript(raw_transcript)
    if not transcript:
        raise TranscriptionGenerationError("Transcription response was empty")
    return transcript


def _select_transcription_source_url(item: Item) -> str | None:
    candidates = [item.audio_url.strip(), item.media_url.strip(), item.original_url.strip()]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if _looks_like_media_url(candidate):
            return candidate

    if item.kind in {ItemKind.PODCAST_EPISODE, ItemKind.VIDEO}:
        for candidate in candidates:
            if candidate:
                return candidate
    return None


def _looks_like_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(suffix) for suffix in SUPPORTED_SUFFIXES)


def _download_media(source_url: str, timeout: int) -> DownloadedMedia:
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
    base_url = settings.ARCHIVE_TRANSCRIPTION_API_BASE.rstrip("/")
    fields = {
        "model": settings.ARCHIVE_TRANSCRIPTION_MODEL,
    }
    if prompt.strip():
        fields["prompt"] = prompt.strip()

    boundary, request_body = _encode_multipart_formdata(fields=fields, media=media)
    request = Request(
        f"{base_url}/audio/transcriptions",
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
