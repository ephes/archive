from __future__ import annotations

import json

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages

from archive.models import Item, ItemKind
from archive.transcriptions import (
    MAX_TRANSCRIPTION_BYTES,
    TranscriptionGenerationError,
    can_transcribe_item,
    find_transcript_gaps,
    generate_item_transcript,
)


class _FakeHeaders:
    def __init__(self, content_type: str, charset: str = "utf-8") -> None:
        self._content_type = content_type
        self._charset = charset

    def get_content_type(self) -> str:
        return self._content_type

    def get_content_charset(self) -> str:
        return self._charset


class _FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        content_type: str,
        url: str,
    ) -> None:
        self._payload = payload
        self.headers = _FakeHeaders(content_type=content_type)
        self._url = url

    def read(self, _size: int | None = None) -> bytes:
        return self._payload

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _delete_archived_object(path: str) -> None:
    if path and storages["archive_media"].exists(path):
        storages["archive_media"].delete(path)


@pytest.mark.django_db
def test_can_transcribe_item_detects_media_sources() -> None:
    item = Item(
        original_url="https://example.com/article",
        kind=ItemKind.LINK,
        archived_audio_path="items/1/audio/source.mp3",
    )

    assert can_transcribe_item(item) is True


@pytest.mark.django_db
def test_can_transcribe_item_rejects_supported_video_pages_without_archived_media() -> None:
    item = Item(
        original_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
    )

    assert can_transcribe_item(item) is False


@pytest.mark.django_db
def test_generate_item_transcript_downloads_media_and_parses_response(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        title="Example episode",
        source="Example Radio",
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.openai.com/v1"
    settings.ARCHIVE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

    def fake_urlopen(request, timeout):
        if request.full_url == "https://cdn.example.com/episode.mp3":
            return _FakeResponse(
                b"audio-bytes",
                content_type="audio/mpeg",
                url="https://cdn.example.com/episode.mp3",
            )

        assert request.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert b'name="model"' in request.data
        assert b"gpt-4o-mini-transcribe" in request.data
        assert b'name="prompt"' in request.data
        return _FakeResponse(
            json.dumps({"text": " Hello  \n\n world "}).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)
    monkeypatch.setattr("archive.transcriptions.open_public_url", fake_urlopen)

    transcript = generate_item_transcript(item=item)

    assert transcript == "Hello\n\nworld"


@pytest.mark.django_db
def test_generate_item_transcript_rejects_oversized_media(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"

    monkeypatch.setattr(
        "archive.transcriptions.open_public_url",
        lambda request, timeout: _FakeResponse(
            b"x" * (MAX_TRANSCRIPTION_BYTES + 1),
            content_type="audio/mpeg",
            url=request.full_url,
        ),
    )

    with pytest.raises(TranscriptionGenerationError, match="25 MiB"):
        generate_item_transcript(item=item)


@pytest.mark.django_db
def test_generate_item_transcript_prefers_archived_audio_over_remote_urls(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=len(b"archived-audio"),
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.openai.com/v1"
    settings.ARCHIVE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
    _delete_archived_object(item.archived_audio_path)
    storages["archive_media"].save(item.archived_audio_path, ContentFile(b"archived-audio"))

    def fake_urlopen(request, timeout):
        assert request.full_url != "https://cdn.example.com/episode.mp3"
        assert request.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert b"archived-audio" in request.data
        return _FakeResponse(
            json.dumps({"text": "Archived transcript"}).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    transcript = generate_item_transcript(item=item)

    assert transcript == "Archived transcript"


@pytest.mark.django_db
def test_generate_item_transcript_falls_back_to_archived_video_when_audio_is_missing(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
        archived_video_path="items/1/video/source.mp4",
        archived_video_content_type="video/mp4",
        archived_video_size_bytes=len(b"archived-video"),
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://api.openai.com/v1"
    settings.ARCHIVE_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
    _delete_archived_object(item.archived_video_path)
    storages["archive_media"].save(item.archived_video_path, ContentFile(b"archived-video"))

    def fake_urlopen(request, timeout):
        assert request.full_url != "https://www.youtube.com/watch?v=demo123"
        assert request.full_url == "https://api.openai.com/v1/audio/transcriptions"
        assert b"archived-video" in request.data
        return _FakeResponse(
            json.dumps({"text": "Video transcript"}).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    transcript = generate_item_transcript(item=item)

    assert transcript == "Video transcript"


@pytest.mark.django_db
def test_generate_item_transcript_does_not_fallback_to_remote_when_archived_audio_open_fails(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/missing.mp3",
        archived_audio_content_type="audio/mpeg",
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("remote fallback should not run when archived audio exists")
        ),
    )

    with pytest.raises(TranscriptionGenerationError, match="Archived audio open failed"):
        generate_item_transcript(item=item)


@pytest.mark.django_db
def test_generate_item_transcript_stages_oversized_archived_audio_for_batch_job(
    monkeypatch, settings
) -> None:
    # This upload-contract fixture represents an older service without segment artifacts.
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 0
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=MAX_TRANSCRIPTION_BYTES + 1,
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    _delete_archived_object(item.archived_audio_path)
    storages["archive_media"].save(
        item.archived_audio_path,
        ContentFile(b"oversized-archived-audio"),
    )
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)

    def fake_urlopen(request, timeout):
        assert request.full_url != "https://cdn.example.com/episode.mp3"
        if request.full_url == "https://voxhelm.example.test/v1/uploads":
            request_body = (
                request.data if isinstance(request.data, bytes) else b"".join(request.data)
            )
            assert b'name="file"' in request_body
            assert b"oversized-archived-audio" in request_body
            return _FakeResponse(
                json.dumps({"id": "upload-123"}).encode("utf-8"),
                content_type="application/json",
                url=request.full_url,
            )
        if request.full_url == "https://voxhelm.example.test/v1/jobs":
            payload = json.loads(request.data.decode("utf-8"))
            assert payload["input"] == {"kind": "upload", "upload_id": "upload-123"}
            assert payload["output"]["formats"] == ["text", "json"]
            assert payload["task_ref"].startswith(f"archive-item-{item.pk}-transcript-")
            assert payload["task_ref"] != f"archive-item-{item.pk}-transcript-v1"
            return _FakeResponse(
                json.dumps({"id": "job-123", "state": "queued"}).encode("utf-8"),
                content_type="application/json",
                url=request.full_url,
            )
        assert request.full_url == "https://voxhelm.example.test/v1/jobs/job-123"
        return _FakeResponse(
            json.dumps(
                {
                    "id": "job-123",
                    "state": "succeeded",
                    "result": {"text": " Batch staged transcript "},
                }
            ).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    assert generate_item_transcript(item=item) == "Batch staged transcript"


def _oversized_archived_audio_item() -> Item:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=MAX_TRANSCRIPTION_BYTES + 1,
    )
    _delete_archived_object(item.archived_audio_path)
    storages["archive_media"].save(
        item.archived_audio_path,
        ContentFile(b"oversized-archived-audio"),
    )
    return item


def _batch_job_urlopen(*, segments: list[dict[str, object]], requested: list[str]):
    """Fake Voxhelm: upload, job submission, a succeeded job, and its segment JSON."""

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        if request.full_url == "https://voxhelm.example.test/v1/uploads":
            payload: dict[str, object] = {"id": "upload-123"}
        elif request.full_url == "https://voxhelm.example.test/v1/jobs":
            payload = {"id": "job-123", "state": "queued"}
        elif request.full_url == "https://voxhelm.example.test/v1/jobs/job-123":
            payload = {
                "id": "job-123",
                "state": "succeeded",
                "result": {
                    "text": "Batch staged transcript",
                    "artifacts": {
                        "text": "/v1/jobs/job-123/artifacts/transcript.txt",
                        "json": "/v1/jobs/job-123/artifacts/transcript.json",
                    },
                },
            }
        else:
            assert (
                request.full_url
                == "https://voxhelm.example.test/v1/jobs/job-123/artifacts/transcript.json"
            )
            assert request.get_header("Authorization") == "Bearer key"
            payload = {"language": "de", "segments": segments, "text": "..."}
        return _FakeResponse(
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    return fake_urlopen


def _segments(*spans: tuple[float, float], step: float = 5.0) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    for span_start, span_end in spans:
        start = span_start
        while start < span_end:
            segments.append({"start": start, "end": min(start + step, span_end), "text": "x"})
            start += step
    return segments


@pytest.mark.django_db
def test_generate_item_transcript_rejects_batch_transcript_with_missing_audio_parts(
    monkeypatch, settings
) -> None:
    # Item 408 (a 3:09 h podcast): whisper.cpp split the audio into four parts and three
    # of them stopped after about 700 s, yet the job succeeded with a partial transcript.
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)
    requested: list[str] = []
    segments = _segments((0, 3536), (5672, 6378), (8508, 9208))
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        _batch_job_urlopen(segments=segments, requested=requested),
    )

    with pytest.raises(
        TranscriptionGenerationError,
        match=r"job-123 is incomplete: 2 stretch\(es\) .* from 0:58:56 to 1:34:32",
    ):
        generate_item_transcript(item=item)
    assert requested[-1].endswith("/jobs/job-123/artifacts/transcript.json")


@pytest.mark.django_db
def test_generate_item_transcript_accepts_batch_transcript_covering_the_audio(
    monkeypatch, settings
) -> None:
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)
    requested: list[str] = []
    # A five-minute music break stays below the default ten-minute limit.
    segments = _segments((0, 3000), (3300, 11343))
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        _batch_job_urlopen(segments=segments, requested=requested),
    )

    assert generate_item_transcript(item=item) == "Batch staged transcript"
    assert requested[-1].endswith("/jobs/job-123/artifacts/transcript.json")


@pytest.mark.django_db
def test_generate_item_transcript_skips_coverage_check_when_disabled(monkeypatch, settings) -> None:
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 0
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)
    requested: list[str] = []
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        _batch_job_urlopen(segments=_segments((0, 60), (5000, 5060)), requested=requested),
    )

    assert generate_item_transcript(item=item) == "Batch staged transcript"
    assert not any(url.endswith("transcript.json") for url in requested)


def test_find_transcript_gaps_reports_uncovered_stretches_including_the_start() -> None:
    segments = [
        {"start": 700.0, "end": 710.0},
        {"start": 705.0, "end": 900.0},
        {"start": 950.0, "end": 960.0},
        {"start": 2000.0, "end": 2010.0},
    ]

    assert find_transcript_gaps(segments, max_gap_seconds=600) == [(0.0, 700.0), (960.0, 2000.0)]


@pytest.mark.parametrize(
    "segments",
    [None, "segments", [{"start": "0", "end": 1}], [["0", "1"]], [{"start": True, "end": 9}]],
)
def test_find_transcript_gaps_rejects_unreadable_segments(segments) -> None:
    with pytest.raises(TranscriptionGenerationError, match="segment"):
        find_transcript_gaps(segments, max_gap_seconds=1)


@pytest.mark.django_db
def test_generate_item_transcript_reports_failed_batch_job_for_oversized_archived_audio(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=MAX_TRANSCRIPTION_BYTES + 1,
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    _delete_archived_object(item.archived_audio_path)
    storages["archive_media"].save(
        item.archived_audio_path,
        ContentFile(b"oversized-archived-audio"),
    )
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)

    def fake_urlopen(request, timeout):
        assert request.full_url != "https://cdn.example.com/episode.mp3"
        if request.full_url == "https://voxhelm.example.test/v1/uploads":
            return _FakeResponse(
                json.dumps({"id": "upload-123"}).encode("utf-8"),
                content_type="application/json",
                url=request.full_url,
            )
        if request.full_url == "https://voxhelm.example.test/v1/jobs":
            return _FakeResponse(
                json.dumps({"id": "job-123", "state": "queued"}).encode("utf-8"),
                content_type="application/json",
                url=request.full_url,
            )
        assert request.full_url == "https://voxhelm.example.test/v1/jobs/job-123"
        return _FakeResponse(
            json.dumps(
                {
                    "id": "job-123",
                    "state": "failed",
                    "error": {"message": "batch boom"},
                }
            ).encode("utf-8"),
            content_type="application/json",
            url=request.full_url,
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)

    with pytest.raises(TranscriptionGenerationError, match="Batch transcription job failed"):
        generate_item_transcript(item=item)


@pytest.mark.django_db
def test_generate_item_transcript_rejects_oversized_archived_video_until_voxhelm_supports_it(
    monkeypatch, settings
) -> None:
    item = Item.objects.create(
        original_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
        archived_video_path="items/1/video/source.mp4",
        archived_video_content_type="video/mp4",
        archived_video_size_bytes=MAX_TRANSCRIPTION_BYTES + 1,
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("remote or batch fallback should not run for oversized archived video")
        ),
    )

    with pytest.raises(
        TranscriptionGenerationError,
        match="uploaded video transcription is not supported yet",
    ):
        generate_item_transcript(item=item)


@pytest.mark.django_db
def test_generate_item_transcript_rejects_unsupported_archived_media(monkeypatch, settings) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url="https://cdn.example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.bin",
        archived_audio_content_type="application/octet-stream",
    )
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("remote fallback should not run when archived audio exists")
        ),
    )

    with pytest.raises(TranscriptionGenerationError, match="Unsupported archived audio type"):
        generate_item_transcript(item=item)


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, float("inf")), (float("nan"), 1), (0, 10**500), (-1, 1), (2, 1)],
)
def test_gap_check_rejects_invalid_time_domain(start, end) -> None:
    with pytest.raises(TranscriptionGenerationError, match="segment"):
        find_transcript_gaps(
            [{"start": start, "end": end}, {"start": 900, "end": 905}],
            max_gap_seconds=600,
        )


@pytest.mark.parametrize(
    "intervals",
    [((900, 905), (0, 900)), ((10, 20), (0, 15), (5, 8)), ((0, 15), (5, 8), (10, 20))],
)
def test_gap_check_uses_interval_union(intervals) -> None:
    assert (
        find_transcript_gaps(
            [{"start": start, "end": end} for start, end in intervals], max_gap_seconds=1
        )
        == []
    )


def test_gap_check_preserves_threshold_and_input_order() -> None:
    segments = [{"start": 20, "end": 25}, {"start": 5, "end": 10}]
    assert find_transcript_gaps(segments, max_gap_seconds=5) == [(10.0, 20.0)]
    assert segments == [{"start": 20, "end": 25}, {"start": 5, "end": 10}]


@pytest.mark.parametrize("text", [None, {}, [], True, 12, 1.5, "", "  "])
def test_batch_result_requires_nonempty_string_text(text) -> None:
    from archive.transcriptions import _parse_batch_transcription_job_update

    with pytest.raises(TranscriptionGenerationError, match="transcript text"):
        _parse_batch_transcription_job_update(
            {"id": "job-synthetic", "state": "succeeded", "result": {"text": text}}
        )


def test_batch_result_preserves_unicode_text() -> None:
    from archive.transcriptions import _parse_batch_transcription_job_update

    text = " Grüße 👋\n第二行 "
    update = _parse_batch_transcription_job_update(
        {"id": "job-synthetic", "state": "succeeded", "result": {"text": text}}
    )
    assert update.transcript == text.strip()


@pytest.mark.django_db
@pytest.mark.parametrize("malformed_text", [False, True])
def test_generate_transcript_rejects_malformed_batch_evidence(
    monkeypatch, settings, malformed_text
) -> None:
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)
    requested: list[str] = []
    response = _batch_job_urlopen(
        segments=[{"start": 0, "end": float("inf")}, {"start": 900, "end": 905}],
        requested=requested,
    )

    def fake_urlopen(request, timeout):
        if malformed_text and request.full_url.endswith("/jobs/job-123"):
            return _FakeResponse(
                json.dumps(
                    {"id": "job-123", "state": "succeeded", "result": {"text": None}}
                ).encode(),
                content_type="application/json",
                url=request.full_url,
            )
        return response(request, timeout)

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)
    expected = "did not return transcript text" if malformed_text else "segment times were invalid"
    with pytest.raises(TranscriptionGenerationError, match=expected):
        generate_item_transcript(item=item)
    assert any(url.endswith("transcript.json") for url in requested) is not malformed_text


@pytest.mark.django_db
@pytest.mark.parametrize("artifact", [None, {}, {"json": None}, {"json": []}, {"json": 12}])
def test_enabled_coverage_requires_json_artifact(monkeypatch, settings, artifact) -> None:
    from archive.transcriptions import (
        BatchTranscriptionJobUpdate,
        _check_batch_transcript_coverage,
        _parse_batch_transcription_job_update,
    )

    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 600
    update = _parse_batch_transcription_job_update(
        {
            "id": "job-synthetic",
            "state": "succeeded",
            "result": {"text": "Valid transcript", "artifacts": artifact},
        }
    )
    assert isinstance(update, BatchTranscriptionJobUpdate)
    with pytest.raises(TranscriptionGenerationError, match="segment artifact"):
        _check_batch_transcript_coverage(completed=update, timeout=1)


@pytest.mark.parametrize("segments", [None, [], {}, "segments"])
def test_enabled_coverage_requires_readable_segments(segments) -> None:
    with pytest.raises(TranscriptionGenerationError, match="segment"):
        find_transcript_gaps(segments, max_gap_seconds=600)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "evidence",
    ["missing-artifact", "bad-artifact-shape", "missing-segments", "empty-segments", "nonobject"],
)
def test_missing_coverage_evidence_cannot_publish_transcript(monkeypatch, settings, evidence):
    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _oversized_archived_audio_item()
    item.short_summary = "Existing summary"
    item.save(update_fields=["short_summary"])
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)
    requested: list[str] = []
    response = _batch_job_urlopen(segments=[], requested=requested)

    def fake_urlopen(request, timeout):
        payload: object
        if request.full_url.endswith("/jobs/job-123") and evidence.endswith("artifact"):
            payload = {"id": "job-123", "state": "succeeded", "result": {"text": "Unchecked"}}
        elif request.full_url.endswith("/jobs/job-123") and evidence == "bad-artifact-shape":
            payload = {
                "id": "job-123",
                "state": "succeeded",
                "result": {"text": "Unchecked", "artifacts": {"json": {"url": "/v1/synthetic"}}},
            }
        elif request.full_url.endswith("transcript.json") and evidence == "missing-segments":
            payload = {"text": "Unchecked"}
        elif request.full_url.endswith("transcript.json") and evidence == "nonobject":
            payload = []
        else:
            return response(request, timeout)
        return _FakeResponse(
            json.dumps(payload).encode(), content_type="application/json", url=request.full_url
        )

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)
    assert enrich_item_transcript(item=item) is False
    item.refresh_from_db()
    assert item.transcript == ""
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.short_summary == "Existing summary"
    assert item.transcript_error


@pytest.mark.django_db
@pytest.mark.parametrize("threshold", [0, -1])
def test_old_batch_service_requires_explicit_coverage_opt_out(monkeypatch, settings, threshold):
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = threshold
    monkeypatch.setattr("archive.transcriptions.sleep", lambda seconds: None)
    requested: list[str] = []
    response = _batch_job_urlopen(segments=[], requested=requested)

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/jobs/job-123"):
            return _FakeResponse(
                json.dumps(
                    {"id": "job-123", "state": "succeeded", "result": {"text": "Valid"}}
                ).encode(),
                content_type="application/json",
                url=request.full_url,
            )
        return response(request, timeout)

    monkeypatch.setattr("archive.transcriptions.urlopen", fake_urlopen)
    assert generate_item_transcript(item=item) == "Valid"
    assert not any(url.endswith("transcript.json") for url in requested)
