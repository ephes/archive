import io
import json
from email.message import Message

import pytest

from archive import article_audio as audio
from archive import transcriptions as tr
from archive.models import Item, ItemKind


class Response(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"


@pytest.mark.parametrize(
    "module,parse,error",
    [
        (tr, tr._parse_batch_transcription_job_update, tr.TranscriptionGenerationError),
        (audio, audio._parse_job_update, audio.ArticleAudioGenerationError),
    ],
)
@pytest.mark.parametrize(
    "value", [None, {}, [], True, "", "x" * 65, 10**64, "../other", "bad\r\nid"]
)
def test_job_ids_are_bounded_scalars(module, parse, error, value):
    with pytest.raises(error):
        parse({"id": value, "state": "queued"})


@pytest.mark.parametrize(
    "parse",
    [tr._parse_batch_transcription_job_update, audio._parse_job_update],
)
def test_job_ids_up_to_the_stored_width_are_admitted(parse):
    assert parse({"id": "x" * 64, "state": "queued"}).job_id == "x" * 64
    assert parse({"id": 10**64 - 1, "state": "queued"}).job_id == "9" * 64


@pytest.mark.django_db
def test_longest_admitted_synthesis_job_id_fits_the_item_field():
    field = Item._meta.get_field("article_audio_job_id")
    update = audio._parse_job_update({"id": "x" * 64, "state": "queued"})
    assert field.max_length >= len(update.job_id)


@pytest.mark.parametrize(
    "parse,error",
    [
        (tr._parse_batch_transcription_job_update, tr.TranscriptionGenerationError),
        (audio._parse_job_update, audio.ArticleAudioGenerationError),
    ],
)
@pytest.mark.parametrize("state", [None, {}, "unknown"])
def test_only_supported_states_are_admitted(parse, error, state):
    with pytest.raises(error):
        parse({"id": "job-a", "state": state})


@pytest.mark.parametrize("value", [None, {}, [], 7, True, "x" * 2049, "/bad\npath"])
@pytest.mark.parametrize("kind", ["audio", "transcript"])
def test_artifacts_require_bounded_string_paths(kind, value):
    if kind == "transcript" and value is None:
        pytest.skip("an explicit null JSON artifact means no segment evidence")
    parse, error, result = (
        (audio._parse_job_update, audio.ArticleAudioGenerationError, {"artifacts": {"mp3": value}})
        if kind == "audio"
        else (
            tr._parse_batch_transcription_job_update,
            tr.TranscriptionGenerationError,
            {"text": "words", "artifacts": {"json": value}},
        )
    )
    with pytest.raises(error):
        parse({"id": "job-a", "state": "succeeded", "result": result})


@pytest.mark.parametrize(
    "module,parse",
    [
        (tr, tr._parse_batch_transcription_job_update),
        (audio, audio._parse_job_update),
    ],
)
def test_valid_pending_and_success_envelopes(module, parse):
    assert parse({"id": 123, "state": "queued"}).job_id == "123"
    assert parse({"id": "job-a", "state": "running"}).is_pending
    result = {"text": "words", "artifacts": {"json": "/v1/a.json", "mp3": "/v1/a.mp3"}}
    assert parse({"id": "job-a", "state": "succeeded", "result": result}).is_complete


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["audio", "transcript"])
def test_wrong_poll_identity_preserves_previous_results(kind, monkeypatch, settings):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        long_summary="Existing text",
        transcript="Previous transcript",
        audio_url="https://example.test/a.mp3",
        article_audio_job_id="job-a",
        article_audio_artifact_path="/v1/previous.mp3",
    )
    result = {"text": "Wrong transcript", "artifacts": {"mp3": "/v1/wrong.mp3"}}
    module = audio if kind == "audio" else tr
    monkeypatch.setattr(
        module,
        "urlopen",
        lambda *a, **kw: Response({"id": "job-b", "state": "succeeded", "result": result}),
    )
    if kind == "transcript":
        monkeypatch.setattr(
            tr,
            "_load_transcription_media",
            lambda **kw: (_ for _ in ()).throw(tr._ArchivedAudioRequiresBatchUpload()),
        )
        monkeypatch.setattr(tr, "_stage_archived_audio_upload", lambda **kw: "upload-a")
        monkeypatch.setattr(
            tr,
            "_submit_batch_transcription_job",
            lambda **kw: tr.BatchTranscriptionJobUpdate("job-a", "queued"),
        )
        settings.ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = 0
        with pytest.raises(tr.TranscriptionPending) as submitted:
            tr.generate_item_transcript(item)
        assert submitted.value.job.job_id == "job-a"
        # The later poll pass refuses the wrong job's result and keeps polling job-a.
        with pytest.raises(tr.TranscriptionPending) as polled:
            tr.generate_item_transcript(item, batch_job=submitted.value.job)
        assert polled.value.job == submitted.value.job
        assert "identity mismatch" in polled.value.poll_error
    else:
        with pytest.raises(audio.ArticleAudioGenerationError, match="identity mismatch"):
            audio.generate_item_article_audio(item)
    item.refresh_from_db()
    assert item.transcript == "Previous transcript"
    assert item.article_audio_artifact_path == "/v1/previous.mp3"


@pytest.mark.parametrize(
    "filename,content_type",
    [
        ('bad"name.mp3', "audio/mpeg"),
        ("bad\r\nInjected: x.mp3", "audio/mpeg"),
        ("safe.mp3", "audio/mpeg\r\nInjected: x"),
        ("safe.mp3", 'audio/mpeg; bad="x"'),
    ],
)
def test_multipart_header_values_refused_before_open(filename, content_type):
    with pytest.raises(tr.TranscriptionGenerationError, match="multipart"):
        body = tr._iter_file_multipart_formdata(
            boundary="synthetic",
            field_name="file",
            filename=filename,
            content_type=content_type,
            file_opener=lambda: pytest.fail("source opened before header validation"),
        )
        next(body)


@pytest.mark.parametrize("filename", ["safe.mp3", "Grüße.mp3"])
def test_multipart_parser_and_length_share_exact_encoding(filename):
    from email.parser import BytesParser
    from email.policy import default

    chunks = tr._iter_file_multipart_formdata(
        boundary="synthetic",
        field_name="file",
        filename=filename,
        content_type="audio/mpeg",
        file_opener=lambda: io.BytesIO(b"synthetic media"),
    )
    body = b"".join(chunks)
    length = tr._multipart_content_length_header(
        boundary="synthetic",
        field_name="file",
        filename=filename,
        content_type="audio/mpeg",
        payload_size=len(b"synthetic media"),
    )
    assert int(length["Content-Length"]) == len(body)
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: multipart/form-data; boundary=synthetic\r\n\r\n" + body
    )
    parts = list(message.iter_parts())
    assert len(parts) == 1
    assert parts[0].get_filename() == filename
    assert parts[0].get_payload(decode=True) == b"synthetic media"


@pytest.mark.parametrize("actual", [b"ab", b"abcd", b"abc"])
def test_staged_length_checked_before_transport(actual, monkeypatch, settings):
    from types import SimpleNamespace

    from archive import upstream_http

    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    monkeypatch.setattr(tr, "open_archived_audio", lambda item: io.BytesIO(actual))
    monkeypatch.setattr(tr, "urlopen", upstream_http.open_authenticated_url)
    calls = []

    def run(command, **kwargs):
        from pathlib import Path

        options = json.loads(kwargs["input"])
        staged = Path(options["data_path"]).read_bytes()
        calls.append(staged)
        assert int(options["headers"]["Content-length"]) == len(staged)
        Path(options["path"]).write_text('{"id": "upload-a"}')
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"headers": [["Content-Type", "application/json"]], "is_error": False}
            ),
        )

    monkeypatch.setattr(upstream_http.subprocess, "run", run)
    source = tr.TranscriptionSource("archived_audio", "source.mp3", "audio/mpeg", 3)
    if len(actual) == 3:
        assert tr._stage_archived_audio_upload(item=Item(), source=source, timeout=5) == "upload-a"
        assert len(calls) == 1
    else:
        with pytest.raises(tr.TranscriptionGenerationError, match="length"):
            tr._stage_archived_audio_upload(item=Item(), source=source, timeout=5)
        assert not calls


@pytest.mark.parametrize("value", [None, {}, True, "x" * 201])
def test_upload_ids_are_validated(value, monkeypatch, settings):
    monkeypatch.setattr(tr, "open_archived_audio", lambda item: io.BytesIO(b"abc"))

    def upload(request, timeout):
        b"".join(request.data)
        return Response({"id": value})

    monkeypatch.setattr(tr, "urlopen", upload)
    with pytest.raises(tr.TranscriptionGenerationError, match="upload identifier"):
        tr._stage_archived_audio_upload(
            item=Item(),
            source=tr.TranscriptionSource("archived_audio", "a.mp3", "audio/mpeg", 3),
            timeout=5,
        )


@pytest.mark.parametrize("mode", ["grows", "read-error", "over-limit"])
def test_staging_failure_closes_source_without_network(mode, monkeypatch, settings):
    from archive import upstream_http

    class Source(io.BytesIO):
        calls = 0

        def read(self, size=-1):
            self.calls += 1
            if self.calls == 1:
                return b"abc"
            if mode == "read-error":
                raise OSError("synthetic private detail")
            return b"x"  # Keeps growing, but only one excess byte may be read.

    source = Source()
    monkeypatch.setattr(tr, "open_archived_audio", lambda item: source)
    monkeypatch.setattr(tr, "urlopen", upstream_http.open_authenticated_url)
    monkeypatch.setattr(
        upstream_http.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("network process started for rejected source"),
    )
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 2 if mode == "over-limit" else 100
    with pytest.raises(tr.TranscriptionGenerationError):
        tr._stage_archived_audio_upload(
            item=Item(),
            source=tr.TranscriptionSource("archived_audio", "a.mp3", "audio/mpeg", 3),
            timeout=5,
        )
    assert source.closed or mode == "over-limit"
    assert source.calls <= 2


@pytest.mark.parametrize("result_artifacts", [{"artifacts": None}, {"artifacts": {"json": None}}])
def test_null_transcription_artifacts_mean_no_segment_evidence(result_artifacts):
    update = tr._parse_batch_transcription_job_update(
        {
            "id": "job-a",
            "state": "succeeded",
            "result": {"text": "words", **result_artifacts},
        }
    )
    assert update.transcript == "words"
    assert update.json_artifact_path == ""


@pytest.mark.parametrize("container", [[], "", 7, True])
def test_transcription_artifacts_container_must_be_an_object(container):
    with pytest.raises(tr.TranscriptionGenerationError, match="artifact"):
        tr._parse_batch_transcription_job_update(
            {
                "id": "job-a",
                "state": "succeeded",
                "result": {"text": "words", "artifacts": container},
            }
        )


def test_staging_diagnostic_never_echoes_source_url():
    private = "https://user:password@example.test/private.bin?signature=secret"
    with pytest.raises(tr.TranscriptionGenerationError) as caught:
        tr._stage_archived_audio_upload(
            item=Item(), source=tr.TranscriptionSource("archived_audio", private, "", 3), timeout=5
        )
    assert str(caught.value) == "Unsupported archived audio type for transcription"


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["audio", "transcript"])
@pytest.mark.parametrize("member", ["id", "state", "result", "artifacts", "path", "ignored"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ambiguous_poll_json_preserves_item(kind, member, reverse, monkeypatch, settings):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = settings.ARCHIVE_TRANSCRIPTION_API_KEY = "synthetic"
    item = Item.objects.create(
        original_url="https://example.test/article",
        kind=ItemKind.ARTICLE,
        transcript="Previous transcript",
        article_audio_job_id="job-a",
        article_audio_artifact_path="/v1/previous.mp3",
    )
    module = audio if kind == "audio" else tr
    path_key = "mp3" if kind == "audio" else "json"
    artifacts = {path_key: "/v1/new-file"}
    result = {"text": "Untrusted new transcript", "artifacts": artifacts}

    def duplicate(key, first, second):
        values = [first, second]
        if reverse:
            values.reverse()
        return ",".join(json.dumps(key) + ":" + json.dumps(value) for value in values)

    parts = {
        "id": '"id":"job-a"',
        "state": '"state":"succeeded"',
        "result": '"result":' + json.dumps(result),
    }
    if member == "id":
        parts["id"] = duplicate("id", "job-a", "job-b")
    elif member == "state":
        parts["state"] = duplicate("state", "queued", "succeeded")
    elif member == "result":
        parts["result"] = duplicate("result", result, {**result, "text": "Other text"})
    elif member == "artifacts":
        parts["result"] = (
            '"result":{"text":"New text",'
            + duplicate("artifacts", artifacts, {path_key: "/v1/other-file"})
            + "}"
        )
    elif member == "path":
        parts["result"] = (
            '"result":{"text":"New text","artifacts":{'
            + duplicate(path_key, "/v1/new-file", "/v1/other-file")
            + "}}"
        )
    else:
        parts["ignored"] = '"ignored":[{' + duplicate("identity", "a", "b") + "}]"
    payload = ("{" + ",".join(parts.values()) + "}").encode()
    response = Response({})
    response.seek(0)
    response.truncate()
    response.write(payload)
    response.seek(0)
    monkeypatch.setattr(module, "urlopen", lambda *a, **kw: response)
    poll, error = (
        (audio.generate_item_article_audio, audio.ArticleAudioGenerationError)
        if kind == "audio"
        else (
            lambda item: tr._poll_batch_transcription_job("job-a", 1),
            tr.TranscriptionGenerationError,
        )
    )
    with pytest.raises(error, match="invalid JSON"):
        poll(item)
    assert response.closed
    item.refresh_from_db()
    assert item.transcript == "Previous transcript"
    assert item.article_audio_job_id == "job-a"
    assert item.article_audio_artifact_path == "/v1/previous.mp3"


@pytest.mark.parametrize(
    "number",
    [
        pytest.param(float("nan"), id="NaN"),
        pytest.param(float("inf"), id="Infinity"),
        pytest.param(float("-inf"), id="-Infinity"),
    ],
)
def test_audio_poll_rejects_nonstandard_numbers_before_job_parsing(monkeypatch, settings, number):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    response = Response({"id": "job-a", "state": "queued", "ignored": [{"number": number}]})
    monkeypatch.setattr(audio, "urlopen", lambda *args, **kwargs: response)

    def unexpected_parse(payload):
        pytest.fail("invalid upstream JSON reached job parsing")

    monkeypatch.setattr(audio, "_parse_job_update", unexpected_parse)
    with pytest.raises(
        audio.ArticleAudioGenerationError,
        match="^Article audio job status request failed: invalid JSON response$",
    ):
        audio.generate_item_article_audio(Item(article_audio_job_id="job-a"))
    assert response.closed


def test_audio_poll_accepts_finite_json_numbers(monkeypatch, settings):
    settings.ARCHIVE_ARTICLE_AUDIO_API_KEY = "synthetic"
    response = Response({"id": "job-a", "state": "queued", "ignored": [{"number": -1.25e20}]})
    monkeypatch.setattr(audio, "urlopen", lambda *args, **kwargs: response)

    update = audio.generate_item_article_audio(Item(article_audio_job_id="job-a"))

    assert (update.job_id, update.state) == ("job-a", "queued")
    assert response.closed


@pytest.mark.parametrize("declared", ["2", "4", "+3", "3, 3", "3", "03", None])
def test_authenticated_body_uses_admitted_staged_length(declared, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from urllib.request import Request

    from archive import upstream_http

    calls = []

    def run(command, **kwargs):
        options = json.loads(kwargs["input"])
        calls.append(options["headers"]["Content-length"])
        assert options["headers"]["Authorization"] == "Bearer SYNTHETIC"
        assert Path(options["data_path"]).read_bytes() == b"abc"
        Path(options["path"]).write_bytes(b"ok")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"headers": [], "is_error": False}))

    monkeypatch.setattr(upstream_http.subprocess, "run", run)
    request = Request(
        "https://api.test/",
        data=iter([b"a", b"bc"]),
        headers={"Authorization": "Bearer SYNTHETIC"},
    )
    if declared is not None:
        request.add_header("Content-Length", declared)
    if declared in ("3", "03", None):
        with upstream_http.open_authenticated_url(request, timeout=5) as response:
            assert response.read() == b"ok"
        assert calls == ["3"]
    else:
        with pytest.raises(OSError, match="Content-Length"):
            with upstream_http.open_authenticated_url(request, timeout=5):
                pytest.fail("invalid framing accepted")
        assert not calls
