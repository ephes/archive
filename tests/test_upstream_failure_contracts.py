import io
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from django.core.files.storage import storages

from archive import article_audio, services, summaries, transcriptions
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.processing_health import processing_health_snapshot

SECRET = "synthetic-private-marker"


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"


@pytest.fixture
def worker_setup(monkeypatch, settings):
    Path(storages["archive_media"].location).mkdir(parents=True, exist_ok=True)
    for stage in ("SUMMARY", "TRANSCRIPTION", "ARTICLE_AUDIO"):
        setattr(settings, f"ARCHIVE_{stage}_API_KEY", "synthetic")
    monkeypatch.setattr(
        summaries, "extract_summary_source_from_url", lambda *a, **kw: summaries.SummarySource()
    )
    monkeypatch.setattr(
        article_audio,
        "_best_effort_article_audio_source_text",
        lambda *a, **kw: "Synthetic article body",
    )
    monkeypatch.setattr(
        transcriptions,
        "_load_transcription_media",
        lambda **kw: transcriptions.DownloadedMedia("a.mp3", "audio/mpeg", b"audio"),
    )


def run_worker(stage):
    item = Item.objects.create(
        original_url="https://example.test/item",
        kind=ItemKind.ARTICLE,
        audio_url="https://example.test/a.mp3",
        long_summary="Operator summary",
        summary_status=EnrichmentStatus.COMPLETE if stage == "article_audio" else "pending",
    )
    worker = {
        "summary": services.enrich_item_summaries,
        "transcript": services.enrich_item_transcript,
        "article_audio": services.enrich_item_article_audio,
    }[stage]
    assert not worker(item)
    item.refresh_from_db()
    assert getattr(item, f"{stage}_status") == EnrichmentStatus.FAILED
    assert item.long_summary == "Operator summary"
    assert item.short_summary == item.transcript == item.tags == ""
    assert not item.article_audio_file and not item.article_audio_artifact_path
    assert storages["archive_media"].listdir("") == ([], [])
    if stage == "summary":
        assert item.summary_retry_count == 1 and item.summary_retry_at is not None
    if stage == "article_audio":
        assert item.article_audio_poll_at is None
    assert processing_health_snapshot()["steps"][stage]["failures"] == 1
    return getattr(item, f"{stage}_error")


def hostile_json(kind):
    if kind == "utf8":
        return b'{"' + SECRET.encode() + b'":"\xff"}'
    if kind == "deep":
        return (
            '{"'
            + SECRET
            + '":'
            + "[" * 100_000
            + "0"
            + "]" * 100_000
            + "}"
        ).encode()
    # Make this independent of ambient interpreter integer-limit settings.
    sys.set_int_max_str_digits(4300)
    return ('{"' + SECRET + '":' + "9" * 5000 + "}").encode()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "stage,module",
    [("summary", summaries), ("transcript", transcriptions), ("article_audio", article_audio)],
)
@pytest.mark.parametrize("kind", ["utf8", "deep", "integer"])
def test_invalid_json_resource_failures_are_controlled(
    stage, module, kind, monkeypatch, worker_setup, caplog
):
    previous = sys.get_int_max_str_digits()
    try:
        response = Response(hostile_json(kind))
        monkeypatch.setattr(module, "urlopen", lambda *a, **kw: response)
        error = run_worker(stage)
        assert "invalid JSON" in error and len(error) < 150
        assert SECRET not in error + caplog.text
        assert response.closed
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize(
    "module,error",
    [
        (transcriptions, transcriptions.TranscriptionGenerationError),
        (article_audio, article_audio.ArticleAudioGenerationError),
    ],
)
@pytest.mark.parametrize("kind", ["utf8", "deep", "integer"])
def test_job_json_helpers_normalize_resource_failures(module, error, kind, monkeypatch):
    previous = sys.get_int_max_str_digits()
    try:
        response = Response(hostile_json(kind))
        monkeypatch.setattr(module, "urlopen", lambda *a, **kw: response)
        with pytest.raises(error, match="invalid JSON"):
            module._request_json(Request("https://example.test/api"), 1, "Synthetic")
        assert response.closed
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "stage,module",
    [("summary", summaries), ("transcript", transcriptions), ("article_audio", article_audio)],
)
@pytest.mark.parametrize("failure_kind", ["400", "503", "url", "os"])
def test_upstream_errors_never_retain_private_details(
    stage, module, failure_kind, monkeypatch, worker_setup, caplog, client
):
    response = Response(SECRET.encode())
    if failure_kind.isdigit():
        failure = HTTPError(
            "https://example.test/" + SECRET, int(failure_kind), SECRET, response.headers, response
        )
    elif failure_kind == "url":
        failure = URLError(SECRET)
    else:
        failure = OSError(SECRET)

    def fail(*a, **kw):
        raise failure

    monkeypatch.setattr(module, "urlopen", fail)
    error = run_worker(stage)
    assert SECRET not in error + caplog.text + client.get("/api/health/").content.decode()
    assert ("HTTP " + failure_kind if failure_kind.isdigit() else "transport") in error
    if failure_kind.isdigit():
        assert response.closed


@pytest.mark.parametrize(
    "module,parse",
    [
        (article_audio, article_audio._parse_job_update),
        (transcriptions, transcriptions._parse_batch_transcription_job_update),
    ],
)
def test_failed_job_messages_are_private(module, parse):
    update = parse({"id": "synthetic", "state": "failed", "error": {"message": SECRET}})
    assert SECRET not in update.error_message
    assert "failed" in update.error_message


def test_nested_generated_summary_decoder_is_controlled():
    with pytest.raises(summaries.SummaryGenerationError, match="JSON"):
        summaries._parse_generated_summary(
            "[" * 100_000 + "0" + "]" * 100_000
        )
