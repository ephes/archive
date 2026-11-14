import json

import pytest
from test_upstream_wire_contract import Response

from archive import article_audio as audio
from archive import transcriptions as tr
from archive.models import Item, ItemKind


@pytest.fixture
def clock(monkeypatch, settings):
    now = [0.0]
    monkeypatch.setattr(tr, "monotonic", lambda: now[0])
    monkeypatch.setattr(tr, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    settings.ARCHIVE_TRANSCRIPTION_POLL_SECONDS = 3
    return now


def test_sync_transcription_receives_only_remaining_budget(clock, monkeypatch):
    item = Item(audio_url="https://example.test/a.mp3")

    def load(**kwargs):
        assert kwargs["timeout"] == 10
        clock[0] = 6
        return tr.DownloadedMedia("a.mp3", "audio/mpeg", b"abc")

    calls = []

    def transcribe(**kwargs):
        calls.append(kwargs["timeout"])
        return "words"

    monkeypatch.setattr(tr, "_load_transcription_media", load)
    monkeypatch.setattr(tr, "_request_transcription", transcribe)
    assert tr.generate_item_transcript(item, timeout=10) == "words"
    assert calls == [4]


@pytest.mark.parametrize("step", ["load", "response"])
def test_expired_sync_pipeline_cannot_submit_or_publish(step, clock, monkeypatch):
    calls = []

    def load(**kwargs):
        clock[0] = 10 if step == "load" else 6
        return tr.DownloadedMedia("a.mp3", "audio/mpeg", b"abc")

    def transcribe(**kwargs):
        calls.append(True)
        clock[0] = 10
        return "late words"

    monkeypatch.setattr(tr, "_load_transcription_media", load)
    monkeypatch.setattr(tr, "_request_transcription", transcribe)
    with pytest.raises(tr.TranscriptionGenerationError, match="[Tt]ime"):
        tr.generate_item_transcript(Item(audio_url="https://example.test/a.mp3"), timeout=10)
    assert calls == ([] if step == "load" else [True])


@pytest.mark.parametrize("complete", [False, True])
def test_batch_pipeline_does_not_restart_timeout(clock, monkeypatch, settings, complete):
    item = Item(
        archived_audio_path="a.mp3", archived_audio_size_bytes=tr.MAX_TRANSCRIPTION_BYTES + 1
    )
    calls = []

    def stage(**kw):
        calls.append(("stage", kw["timeout"]))
        clock[0] = 4
        return "upload-a"

    def submit(**kw):
        calls.append(("submit", kw["timeout"]))
        clock[0] = 8
        return tr.BatchTranscriptionJobUpdate(
            "job-a", "succeeded" if complete else "queued", "words"
        )

    def coverage(**kw):
        calls.append(("coverage", kw["timeout"]))

    monkeypatch.setattr(tr, "_stage_archived_audio_upload", stage)
    monkeypatch.setattr(tr, "_submit_batch_transcription_job", submit)
    monkeypatch.setattr(tr, "_check_batch_transcript_coverage", coverage)
    monkeypatch.setattr(
        tr,
        "_poll_batch_transcription_job",
        lambda **kw: pytest.fail("poll started after the shared budget was consumed by sleep"),
    )
    if complete:
        assert tr.generate_item_transcript(item, timeout=10) == "words"
        assert calls == [("stage", 10), ("submit", 6), ("coverage", 2)]
    else:
        with pytest.raises(tr.TranscriptionGenerationError, match="[Tt]ime"):
            tr.generate_item_transcript(item, timeout=10)
        assert calls == [("stage", 10), ("submit", 6)]


@pytest.mark.parametrize("setting", ["MODEL", "VOICE", "LANGUAGE"])
def test_synthesis_task_identity_tracks_output_configuration(monkeypatch, settings, setting):
    item = Item(pk=1, kind=ItemKind.ARTICLE, long_summary="Synthetic body")
    before = audio._task_ref_for_script(item, "same words")
    assert before == audio._task_ref_for_script(item, "same words")
    setattr(settings, f"ARCHIVE_ARTICLE_AUDIO_{setting}", "changed-synthetic")
    assert audio._task_ref_for_script(item, "same words") != before


def test_pending_synthesis_keeps_recorded_job_under_changed_configuration(monkeypatch, settings):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    settings.ARCHIVE_ARTICLE_AUDIO_VOICE = "changed-synthetic"
    item = Item(pk=1, kind=ItemKind.ARTICLE, article_audio_job_id="recorded-job")
    requests = []

    def request(req, **kw):
        requests.append((req.get_method(), req.full_url))
        return Response({"id": "recorded-job", "state": "running"})

    monkeypatch.setattr(audio, "urlopen", request)
    assert audio.generate_item_article_audio(item).job_id == "recorded-job"
    assert len(requests) == 1 and requests[0][0] == "GET"
    assert requests[0][1].endswith("/jobs/recorded-job")


@pytest.mark.parametrize("failure", [ValueError, OSError])
def test_source_extraction_failure_logs_only_trusted_category(
    monkeypatch, settings, caplog, failure
):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    secret = (
        "https://private-user:private-pass@example.test/private-path?signature=private-query BODY"
    )

    def extract(*a, **kw):
        raise failure(secret)

    monkeypatch.setattr(audio, "extract_summary_source_from_url", extract)
    payloads = []

    def submit(request, **kw):
        payloads.append(json.loads(request.data))
        return Response({"id": "job-a", "state": "queued"})

    monkeypatch.setattr(audio, "urlopen", submit)
    item = Item(
        pk=7,
        kind=ItemKind.ARTICLE,
        original_url="https://example.test/",
        long_summary="Stored fallback body",
    )
    assert audio.generate_item_article_audio(item).is_pending
    assert payloads[0]["input"]["text"] == "Stored fallback body"
    assert all(
        marker not in caplog.text
        for marker in ["private-user", "private-pass", "private-path", "private-query", "BODY"]
    )
    assert "item 7" in caplog.text and "source_extraction_failed" in caplog.text


@pytest.mark.parametrize("expire_at", [None, "uploads", "jobs", "job-a", "segments.json"])
def test_real_batch_boundaries_share_deadline(clock, monkeypatch, settings, expire_at):
    import io

    settings.ARCHIVE_TRANSCRIPTION_POLL_SECONDS = 1
    settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 600
    monkeypatch.setattr(tr, "MAX_TRANSCRIPTION_BYTES", 0)
    monkeypatch.setattr(tr, "open_archived_audio", lambda item: io.BytesIO(b"a"))
    item = Item(
        archived_audio_path="a.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=1,
    )
    seen = []

    def request(req, timeout):
        name = req.full_url.rsplit("/", 1)[-1]
        seen.append((name, timeout))
        if name == "uploads":
            b"".join(req.data)
            clock[0] = 4
            payload = {"id": "upload-a"}
        elif name == "jobs":
            clock[0] = 7
            payload = {"id": "job-a", "state": "queued"}
        elif name == "job-a":
            clock[0] = 9
            payload = {
                "id": "job-a",
                "state": "succeeded",
                "result": {"text": "words", "artifacts": {"json": "/v1/segments.json"}},
            }
        else:
            assert name == "segments.json"
            clock[0] = 9.5
            payload = {"segments": [{"start": 0, "end": 1}]}
        if name == expire_at:
            clock[0] = 10
        return Response(payload)

    monkeypatch.setattr(tr, "urlopen", request)
    expected = [("uploads", 10), ("jobs", 6), ("job-a", 2), ("segments.json", 1)]
    if expire_at is None:
        assert tr.generate_item_transcript(item, timeout=10) == "words"
        assert seen == expected
    else:
        with pytest.raises(tr.TranscriptionGenerationError, match="[Tt]ime"):
            tr.generate_item_transcript(item, timeout=10)
        index = [name for name, _ in expected].index(expire_at)
        assert seen == expected[: index + 1]


def test_storage_staging_deadline_closes_source_without_starting_transport(clock, monkeypatch):
    import io

    from archive import upstream_http

    class SlowSource(io.BytesIO):
        def read(self, size=-1):
            clock[0] += 6
            return super().read(size)

    source = SlowSource(b"abc")
    monkeypatch.setattr(tr, "open_archived_audio", lambda item: source)
    monkeypatch.setattr(tr, "urlopen", upstream_http.open_authenticated_url)
    monkeypatch.setattr(
        upstream_http.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("expired staged body sent to network"),
    )
    with pytest.raises(tr.TranscriptionGenerationError, match="[Tt]ime"):
        tr._stage_archived_audio_upload(
            item=Item(),
            source=tr.TranscriptionSource("archived_audio", "a.mp3", "audio/mpeg", 3),
            timeout=10,
        )
    assert source.closed


def test_synthesis_identity_hashes_the_submitted_payload(monkeypatch, settings):
    import hashlib

    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    captured = []

    def request(req, **kw):
        payload = json.loads(req.data)
        ref = payload.pop("task_ref")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        assert ref == f"archive-item-1-article-audio-v3-{expected}"
        captured.append(ref)
        return Response({"id": "job-a", "state": "queued"})

    monkeypatch.setattr(audio, "urlopen", request)
    item = Item(pk=1)
    audio._submit_synthesis_job(item, "words", timeout=10)
    audio._submit_synthesis_job(item, "words", timeout=10)
    assert captured[0] == captured[1] == audio._task_ref_for_script(item, "words")
