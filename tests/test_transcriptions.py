from __future__ import annotations

import json

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages

from archive.models import Item, ItemKind
from archive.transcriptions import (
    MAX_TRANSCRIPTION_BYTES,
    TranscriptionGenerationError,
    TranscriptionJobFailed,
    TranscriptionPending,
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


def _transcribe_through_batch_job(item: Item) -> str:
    """Upload and submit the batch job, then poll it once as a later worker pass does."""
    with pytest.raises(TranscriptionPending) as pending:
        generate_item_transcript(item=item)
    return generate_item_transcript(item=item, batch_job=pending.value.job)


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
    monkeypatch.setattr(
        "archive.transcriptions._open_direct_media",
        lambda request, *, timeout, max_bytes: fake_urlopen(request, timeout),
    )

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
        "archive.transcriptions._open_direct_media",
        lambda request, *, timeout, max_bytes: _FakeResponse(
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
        ContentFile(b"oversized-archived-audio".ljust(MAX_TRANSCRIPTION_BYTES + 1, b"x")),
    )

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

    assert _transcribe_through_batch_job(item) == "Batch staged transcript"


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
        ContentFile(b"oversized-archived-audio".ljust(MAX_TRANSCRIPTION_BYTES + 1, b"x")),
    )
    return item


def _batch_job_urlopen(*, segments: list[dict[str, object]], requested: list[str]):
    """Fake Voxhelm: upload, job submission, a succeeded job, and its segment JSON."""

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        if request.full_url == "https://voxhelm.example.test/v1/uploads":
            b"".join(request.data)
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
            payload = {"language": "de", "segments": segments, "text": "Batch staged transcript"}
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
        _transcribe_through_batch_job(item)
    assert requested[-1].endswith("/jobs/job-123/artifacts/transcript.json")


@pytest.mark.django_db
def test_generate_item_transcript_accepts_batch_transcript_covering_the_audio(
    monkeypatch, settings
) -> None:
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    requested: list[str] = []
    # A five-minute music break stays below the default ten-minute limit.
    segments = _segments((0, 3000), (3300, 11343))
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        _batch_job_urlopen(segments=segments, requested=requested),
    )

    assert _transcribe_through_batch_job(item) == "Batch staged transcript"
    assert requested[-1].endswith("/jobs/job-123/artifacts/transcript.json")


@pytest.mark.django_db
def test_generate_item_transcript_skips_coverage_check_when_disabled(monkeypatch, settings) -> None:
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 0
    requested: list[str] = []
    monkeypatch.setattr(
        "archive.transcriptions.urlopen",
        _batch_job_urlopen(segments=_segments((0, 60), (5000, 5060)), requested=requested),
    )

    assert _transcribe_through_batch_job(item) == "Batch staged transcript"
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
        ContentFile(b"oversized-archived-audio".ljust(MAX_TRANSCRIPTION_BYTES + 1, b"x")),
    )

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

    with pytest.raises(TranscriptionJobFailed, match="Batch transcription job failed"):
        _transcribe_through_batch_job(item)


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
    requested: list[str] = []
    response = _batch_job_urlopen(
        segments=[{"start": 0, "end": -1}, {"start": 900, "end": 905}],
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
        _transcribe_through_batch_job(item)
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
    if isinstance(artifact, dict) and artifact.get("json") is not None:
        with pytest.raises(TranscriptionGenerationError, match="segment artifact"):
            _parse_batch_transcription_job_update(
                {
                    "id": "job-synthetic",
                    "state": "succeeded",
                    "result": {"text": "Valid transcript", "artifacts": artifact},
                }
            )
        return
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
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_job_id == "job-123"
    item.transcript_poll_at = None
    item.save(update_fields=["transcript_poll_at"])
    assert enrich_item_transcript(item=item) is False
    item.refresh_from_db()
    assert item.transcript == ""
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.short_summary == "Existing summary"
    assert item.transcript_error
    # The evidence of a finished job never improves; a retry needs a new job.
    assert item.transcript_job_id == ""


@pytest.mark.django_db
@pytest.mark.parametrize("threshold", [0, -1])
def test_old_batch_service_requires_explicit_coverage_opt_out(monkeypatch, settings, threshold):
    item = _oversized_archived_audio_item()
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = threshold
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
    assert _transcribe_through_batch_job(item) == "Valid"
    assert not any(url.endswith("transcript.json") for url in requested)


class _FakeVoxhelm:
    """A batch transcription service whose job state the test advances between passes."""

    BASE = "https://voxhelm.example.test/v1"

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.state = "queued"
        self.submitted_state = "queued"
        self.poll_error: Exception | None = None
        self.segments = _segments((0, 3000))
        self.artifact: dict[str, object] = {"text": "Async batch transcript"}
        self.artifact_error: Exception | None = None
        self.result_metadata: dict[str, object] = {}
        self.jobs = 0

    def __call__(self, request, timeout):
        url = request.full_url
        self.requested.append(url)
        payload: object
        if url == f"{self.BASE}/uploads":
            b"".join(request.data)
            payload = {"id": "upload-123"}
        elif url == f"{self.BASE}/jobs":
            self.jobs += 1
            payload = self.job_payload(f"job-{self.jobs}", self.submitted_state)
        elif url.endswith("/artifacts/transcript.json"):
            if self.artifact_error is not None:
                raise self.artifact_error
            payload = {"segments": self.segments, **self.artifact}
        else:
            assert url.startswith(f"{self.BASE}/jobs/job-")
            if self.poll_error is not None:
                raise self.poll_error
            job_id = url.rsplit("/", 1)[1]
            payload = self.job_payload(job_id, self.state)
        return _FakeResponse(
            json.dumps(payload).encode("utf-8"), content_type="application/json", url=url
        )

    def job_payload(self, job_id: str, state: str) -> dict[str, object]:
        payload: dict[str, object] = {"id": job_id, "state": state}
        if state == "succeeded":
            payload["result"] = {
                "text": " Async   batch transcript ",
                "metadata": self.result_metadata,
                "artifacts": {"json": f"/v1/jobs/{job_id}/artifacts/transcript.json"},
            }
        return payload

    def uploads(self) -> int:
        return self.requested.count(f"{self.BASE}/uploads")


@pytest.fixture
def voxhelm(monkeypatch, settings) -> _FakeVoxhelm:
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = _FakeVoxhelm.BASE
    settings.ARCHIVE_TRANSCRIPTION_POLL_SECONDS = 60
    settings.ARCHIVE_TRANSCRIPTION_MAX_JOB_AGE_SECONDS = 7200
    fake = _FakeVoxhelm()
    monkeypatch.setattr("archive.transcriptions.urlopen", fake)
    return fake


def _claimed_batch_item() -> Item:
    from archive.models import EnrichmentStatus

    item = _oversized_archived_audio_item()
    Item.objects.filter(pk=item.pk).update(
        enrichment_status=EnrichmentStatus.COMPLETE,
        media_archive_status=EnrichmentStatus.COMPLETE,
        summary_status=EnrichmentStatus.COMPLETE,
        short_summary="Summary from metadata",
        long_summary="Long summary from metadata",
        tags="podcast",
        short_summary_generated=True,
        long_summary_generated=True,
        tags_generated=True,
        transcript_status=EnrichmentStatus.PROCESSING,
    )
    item.refresh_from_db()
    return item


def _make_poll_due(item: Item) -> None:
    from django.utils import timezone

    Item.objects.filter(pk=item.pk).update(transcript_poll_at=timezone.now())
    item.refresh_from_db()


@pytest.mark.django_db
def test_batch_job_is_stored_and_polled_later_instead_of_awaited(voxhelm) -> None:
    from django.utils import timezone

    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    before = timezone.now()

    assert enrich_item_transcript(item=item) is True

    item.refresh_from_db()
    # The worker uploads and submits, then moves on without asking for the job's state.
    assert voxhelm.requested == [f"{voxhelm.BASE}/uploads", f"{voxhelm.BASE}/jobs"]
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_error == ""
    assert item.transcript_job_id == "job-1"
    assert item.transcript_job_ref.startswith(f"archive-item-{item.pk}-transcript-")
    assert item.transcript_job_submitted_at is not None
    assert item.transcript_job_submitted_at >= before
    assert item.transcript_poll_at is not None
    assert item.transcript_poll_at >= before + timezone.timedelta(seconds=60)
    assert item.processing_started_at is None

    # A running job costs one status request per pass and is never uploaded again.
    _make_poll_due(item)
    voxhelm.state = "running"
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    assert voxhelm.requested[2:] == [f"{voxhelm.BASE}/jobs/job-1"]
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_job_id == "job-1"
    assert item.transcript_poll_at > timezone.now()

    _make_poll_due(item)
    voxhelm.state = "succeeded"
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    assert voxhelm.requested[3:] == [
        f"{voxhelm.BASE}/jobs/job-1",
        f"{voxhelm.BASE}/jobs/job-1/artifacts/transcript.json",
    ]
    assert voxhelm.uploads() == 1
    assert item.transcript == "Async batch transcript"
    assert item.transcript_generated is True
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert item.transcript_error == ""
    assert (item.transcript_job_id, item.transcript_job_ref) == ("", "")
    assert item.transcript_job_submitted_at is None
    assert item.transcript_poll_at is None
    # Summaries generated without the transcript are refreshed from it.
    assert item.summary_status == EnrichmentStatus.PENDING


@pytest.mark.django_db
def test_failed_batch_job_fails_the_step_and_forgets_the_job(voxhelm) -> None:
    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    _make_poll_due(item)
    voxhelm.state = "failed"

    assert enrich_item_transcript(item=item) is False

    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.transcript_error == "Batch transcription job failed"
    assert item.transcript_job_id == ""
    assert item.transcript_poll_at is None
    assert voxhelm.uploads() == 1


@pytest.mark.django_db
def test_incomplete_batch_transcript_fails_after_polling(voxhelm) -> None:
    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    _make_poll_due(item)
    voxhelm.state = "succeeded"
    voxhelm.segments = _segments((0, 600), (2000, 2600))

    assert enrich_item_transcript(item=item) is False

    item.refresh_from_db()
    assert item.transcript == ""
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert "job-1 is incomplete" in item.transcript_error
    assert item.transcript_job_id == ""


@pytest.mark.django_db
@pytest.mark.parametrize(
    "artifact",
    [
        {},
        {"text": None},
        {"text": True},
        {"text": ["Async batch transcript"]},
        {"text": " \t\r\n "},
        {"text": "Complete text of a different recording"},
        {"text": "Async batch\ntranscript"},
        {"text": "Async batch transcript", "job_id": "job-other"},
        {"text": "Async batch transcript", "job_id": None},
        {"text": "Async batch transcript", "job_id": True},
        {"text": "Async batch transcript", "id": "job-other"},
        {"text": "Async batch transcript", "job_id": "job-1", "id": "job-other"},
    ],
)
def test_unbound_batch_artifact_is_immutable_failure(voxhelm, artifact):
    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item) is True
    item.refresh_from_db()
    _make_poll_due(item)
    voxhelm.state = "succeeded"
    voxhelm.artifact = artifact

    assert enrich_item_transcript(item) is False

    item.refresh_from_db()
    assert item.transcript == ""
    assert item.short_summary == "Summary from metadata"
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.transcript_job_id == ""
    assert "artifact" in item.transcript_error
    assert voxhelm.uploads() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "identity", [{}, {"job_id": "job-1"}, {"id": "job-1"}, {"job_id": "job-1", "id": "job-1"}]
)
def test_batch_artifact_binding_uses_publication_normalization(voxhelm, identity):
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item) is True
    item.refresh_from_db()
    _make_poll_due(item)
    voxhelm.state = "succeeded"
    voxhelm.artifact = {"text": " \tAsync  batch\ttranscript\r\n", **identity}

    assert enrich_item_transcript(item) is True

    item.refresh_from_db()
    assert item.transcript == "Async batch transcript"
    assert item.transcript_job_id == ""


@pytest.mark.django_db
@pytest.mark.parametrize("immediately_complete", [False, True])
def test_artifact_transport_error_keeps_job_for_matching_retry(voxhelm, immediately_complete):
    from urllib.error import URLError

    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    submitted_at = None
    if immediately_complete:
        voxhelm.submitted_state = "succeeded"
    else:
        assert enrich_item_transcript(item) is True
        item.refresh_from_db()
        submitted_at = item.transcript_job_submitted_at
        _make_poll_due(item)
    voxhelm.state = "succeeded"
    voxhelm.artifact_error = URLError("synthetic unavailable artifact")

    assert enrich_item_transcript(item) is False

    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_job_id == "job-1"
    assert item.transcript_job_submitted_at is not None
    if submitted_at is not None:
        assert item.transcript_job_submitted_at == submitted_at
    assert item.transcript == ""
    assert item.short_summary == "Summary from metadata"
    _make_poll_due(item)
    voxhelm.artifact_error = None
    assert enrich_item_transcript(item) is True
    item.refresh_from_db()
    assert item.transcript == "Async batch transcript"
    assert item.transcript_job_id == ""
    assert voxhelm.uploads() == 1


@pytest.mark.django_db
def test_unreadable_job_status_keeps_the_job_and_polls_again(voxhelm) -> None:
    from urllib.error import URLError

    from django.utils import timezone

    from archive.models import EnrichmentStatus, ProcessingStepState
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    submitted_at = item.transcript_job_submitted_at
    _make_poll_due(item)
    voxhelm.poll_error = URLError("connection refused")

    assert enrich_item_transcript(item=item) is False

    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert "Batch transcription job status request failed" in item.transcript_error
    assert item.transcript_job_id == "job-1"
    assert item.transcript_job_submitted_at == submitted_at
    assert item.transcript_poll_at > timezone.now()
    assert ProcessingStepState.objects.get(step="transcript").failing_since is not None

    _make_poll_due(item)
    voxhelm.poll_error = None
    voxhelm.state = "succeeded"
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    assert item.transcript == "Async batch transcript"
    assert item.transcript_error == ""
    assert voxhelm.uploads() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("poll_error", [False, True])
def test_batch_job_older_than_the_maximum_age_fails(voxhelm, poll_error) -> None:
    from urllib.error import URLError

    from django.utils import timezone

    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item=item) is True
    Item.objects.filter(pk=item.pk).update(
        transcript_job_submitted_at=timezone.now() - timezone.timedelta(hours=2, seconds=1),
        transcript_poll_at=timezone.now(),
    )
    item.refresh_from_db()
    voxhelm.state = "running"
    if poll_error:
        voxhelm.poll_error = URLError("connection refused")

    assert enrich_item_transcript(item=item) is False

    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.FAILED
    assert item.transcript_error.startswith(
        "Batch transcription job job-1 did not finish within 7200 s"
    )
    assert ("last status request" in item.transcript_error) is poll_error
    assert item.transcript_job_id == ""
    assert item.transcript_job_submitted_at is None
    assert voxhelm.uploads() == 1


@pytest.mark.django_db
def test_finished_job_is_still_used_after_the_maximum_age(voxhelm) -> None:
    from django.utils import timezone

    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item=item) is True
    Item.objects.filter(pk=item.pk).update(
        transcript_job_submitted_at=timezone.now() - timezone.timedelta(hours=3),
        transcript_poll_at=timezone.now(),
    )
    item.refresh_from_db()
    voxhelm.state = "succeeded"

    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    assert item.transcript == "Async batch transcript"


@pytest.mark.django_db
def test_job_for_an_older_source_is_not_resumed(voxhelm) -> None:
    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()
    old_ref = item.transcript_job_ref
    # The archived audio was replaced by a different file after the job was submitted.
    Item.objects.filter(pk=item.pk).update(archived_audio_size_bytes=MAX_TRANSCRIPTION_BYTES + 2)
    storages["archive_media"].delete(item.archived_audio_path)
    storages["archive_media"].save(
        item.archived_audio_path,
        ContentFile(b"replaced-archived-audio".ljust(MAX_TRANSCRIPTION_BYTES + 2, b"y")),
    )
    _make_poll_due(item)

    assert enrich_item_transcript(item=item) is True

    item.refresh_from_db()
    assert voxhelm.uploads() == 2
    assert f"{voxhelm.BASE}/jobs/job-1" not in voxhelm.requested
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_job_id == "job-2"
    assert item.transcript_job_ref != old_ref


@pytest.mark.django_db
def test_worker_claims_a_pending_job_only_when_its_poll_is_due(voxhelm, monkeypatch) -> None:
    from django.utils import timezone

    from archive.models import EnrichmentStatus
    from archive.services import (
        claim_pending_item,
        enrich_item_metadata,
        recover_processing_items,
    )

    item = _claimed_batch_item()
    Item.objects.filter(pk=item.pk).update(transcript_status=EnrichmentStatus.PENDING)
    monkeypatch.setattr(
        "archive.services.generate_item_summaries",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("summary unavailable")),
    )

    claimed = claim_pending_item()
    assert claimed is not None and claimed.pk == item.pk
    assert enrich_item_metadata(item=claimed) is True
    item.refresh_from_db()
    assert item.transcript_job_id == "job-1"
    assert item.processing_started_at is None

    # Not claimable again until the poll is due, so the worker stays idle meanwhile.
    assert claim_pending_item() is None

    # Claimed for another step, the pass leaves the job alone until its poll is due.
    Item.objects.filter(pk=item.pk).update(summary_status=EnrichmentStatus.PENDING)
    claimed = claim_pending_item()
    assert claimed is not None and claimed.transcript_status == EnrichmentStatus.PENDING
    voxhelm.requested.clear()
    assert enrich_item_metadata(item=claimed) is True
    assert voxhelm.requested == []
    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_job_id == "job-1"

    # A worker that died while polling hands the job on; the next pass polls it at once.
    Item.objects.filter(pk=item.pk).update(
        summary_status=EnrichmentStatus.COMPLETE,
        transcript_status=EnrichmentStatus.PROCESSING,
        processing_started_at=timezone.now(),
    )
    assert recover_processing_items() == 1
    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_job_id == "job-1"
    assert item.transcript_poll_at is None
    voxhelm.state = "succeeded"
    claimed = claim_pending_item()
    assert claimed is not None and claimed.transcript_status == EnrichmentStatus.PROCESSING
    # The summary refresh that follows the transcript fails here; the transcript stands.
    enrich_item_metadata(item=claimed)
    item.refresh_from_db()
    assert item.transcript == "Async batch transcript"
    assert item.transcript_status == EnrichmentStatus.COMPLETE
    assert voxhelm.uploads() == 0  # since the first pass: polled, never uploaded again


@pytest.mark.django_db
def test_item_reprocess_keeps_a_running_job_and_polls_it_now(voxhelm) -> None:
    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript, request_item_reprocess

    item = _claimed_batch_item()
    assert enrich_item_transcript(item=item) is True
    item.refresh_from_db()

    request_item_reprocess(item)

    item.refresh_from_db()
    assert item.transcript_status == EnrichmentStatus.PENDING
    assert item.transcript_job_id == "job-1"
    assert item.transcript_poll_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("lock_wait_seconds", [1, 3])
def test_pending_reconciliation_uses_post_lock_time(voxhelm, monkeypatch, lock_wait_seconds):
    from django.utils import timezone

    from archive.models import EnrichmentStatus
    from archive.services import enrich_item_transcript

    item = _claimed_batch_item()
    assert enrich_item_transcript(item) is True
    before_lock = timezone.now()
    after_lock = before_lock + timezone.timedelta(seconds=lock_wait_seconds)
    Item.objects.filter(pk=item.pk).update(
        transcript_job_submitted_at=before_lock - timezone.timedelta(seconds=7198),
        transcript_poll_at=before_lock,
    )
    item.refresh_from_db()
    refresh = item.refresh_from_db
    now = before_lock

    def refresh_with_lock_wait(**kwargs):
        nonlocal now
        refresh(**kwargs)
        if "from_queryset" in kwargs:
            now = after_lock

    monkeypatch.setattr(item, "refresh_from_db", refresh_with_lock_wait)
    monkeypatch.setattr("archive.services.timezone.now", lambda: now)

    assert enrich_item_transcript(item) is (lock_wait_seconds == 1)
    refresh()
    if lock_wait_seconds == 1:
        assert item.transcript_poll_at == after_lock + timezone.timedelta(seconds=60)
    else:
        assert item.transcript_status == EnrichmentStatus.FAILED
        assert item.transcript_job_id == ""


@pytest.mark.django_db
def test_point_segments_cannot_hide_uncovered_batch_audio(voxhelm, settings):
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 25
    voxhelm.state = "succeeded"
    voxhelm.segments = _segments((0, 1), (80, 81)) + [
        {"start": point, "end": point} for point in (20, 40, 60)
    ]

    with pytest.raises(TranscriptionJobFailed, match="0:00:01 to 0:01:20"):
        _transcribe_through_batch_job(_oversized_archived_audio_item())


def test_gap_check_rejects_point_only_evidence():
    with pytest.raises(TranscriptionGenerationError, match="positive span"):
        find_transcript_gaps([{"start": 5, "end": 5}], max_gap_seconds=10)


@pytest.mark.parametrize(
    ("read_payload", "close_error"), [(False, False), (True, False), (True, True)]
)
def test_staging_closes_reader_even_if_failed_transport_keeps_request(
    voxhelm, monkeypatch, read_payload, close_error
):
    from io import BytesIO
    from urllib.error import URLError

    from archive import transcriptions

    class Reader(BytesIO):
        def close(self):
            was_closed = self.closed
            super().close()
            if close_error and not was_closed:
                raise OSError("synthetic close failure")

    source = transcriptions.TranscriptionSource(
        kind="archived_audio", location="audio.mp3", content_type="audio/mpeg", size_bytes=4
    )
    opened = []
    retained = []

    def open_audio(item):
        reader = Reader(b"test")
        opened.append(reader)
        return reader

    def interrupted_transport(request, timeout):
        retained.append(request)
        if read_payload:
            next(request.data)  # Multipart headers.
            assert next(request.data) == b"test"
        raise URLError("synthetic send failure")

    monkeypatch.setattr(transcriptions, "open_archived_audio", open_audio)
    monkeypatch.setattr(transcriptions, "urlopen", interrupted_transport)
    with pytest.raises(TranscriptionGenerationError, match=r"transport error \(URLError\)"):
        transcriptions._stage_archived_audio_upload(item=Item(), source=source, timeout=30)

    assert retained
    assert len(opened) == int(read_payload)
    assert all(reader.closed for reader in opened)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("metadata", "error"),
    [
        ({}, None),
        ({"duration_seconds": 2000}, None),
        ({"media_duration_seconds": 610}, None),
        ({"media_duration_seconds": 611}, "incomplete"),
        ({"media_duration_seconds": "611"}, "invalid measured duration"),
        ({"media_duration_seconds": True}, "invalid measured duration"),
        ({"media_duration_seconds": -1}, "invalid measured duration"),
        ({"media_duration_seconds": None}, "invalid measured duration"),
    ],
)
def test_batch_trailing_gap_uses_measured_media_duration(voxhelm, metadata, error):
    voxhelm.state = "succeeded"
    voxhelm.segments = _segments((0, 10))
    voxhelm.result_metadata = metadata
    voxhelm.artifact["job_id"] = "job-1"
    item = _oversized_archived_audio_item()

    if error:
        with pytest.raises(TranscriptionJobFailed, match=error):
            _transcribe_through_batch_job(item)
    else:
        assert _transcribe_through_batch_job(item) == "Async batch transcript"
