"""Long recordings: multi-gigabyte media must pass without whole-file buffering."""

from __future__ import annotations

import io
import json
import resource
import shlex
import shutil
import sys
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import storages
from django.urls import reverse
from django.utils import timezone
from storages.backends.s3 import S3Storage

from archive.management.commands.run_metadata_worker import Command as WorkerCommand
from archive.media_archival import (
    MediaArchivalError,
    S3RangeReader,
    archive_item_audio,
    ensure_temp_space,
    open_archived_audio,
)
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.services import _mark_transcript_pending
from archive.transcription_config import (
    size_scaled_seconds,
    transcription_min_bytes_per_second,
)
from archive.transcriptions import (
    BatchTranscriptionJob,
    TranscriptionGenerationError,
    TranscriptionPending,
    _select_transcription_source,
    _stage_archived_audio_upload,
    generate_item_transcript,
    staging_allowance_seconds,
)

GIB = 1024 * 1024 * 1024
PAYLOAD = bytes(range(256)) * 100


class _FakeBody:
    def __init__(self, data: bytes, *, piece: int | None = None) -> None:
        self._data = io.BytesIO(data)
        self._piece = piece
        self.delivered = 0
        self.closed = False

    def read(self, amt: int) -> bytes:
        if self._piece is not None:
            amt = min(amt, self._piece)
        chunk = self._data.read(amt)
        self.delivered += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def _content_range(start: int, size: int) -> str:
    return f"bytes {start}-{size - 1}/{size}"


class _FakeS3Object:
    e_tag = '"etag-1"'

    def __init__(self, data: bytes, *, piece: int | None = None) -> None:
        self._data = data
        self._piece = piece
        self.content_length = len(data)
        self.ranges: list[str] = []
        self.calls: list[dict] = []
        self.bodies: list[_FakeBody] = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        self.ranges.append(kwargs["Range"])
        start = int(kwargs["Range"].removeprefix("bytes=").removesuffix("-"))
        body = _FakeBody(self._data[start:], piece=self._piece)
        self.bodies.append(body)
        return {
            "Body": body,
            "ContentRange": _content_range(start, len(self._data)),
            "ETag": self.e_tag,
        }


class _ZeroBody:
    """A lazily generated object body; nothing is held beyond the requested chunk."""

    def __init__(self, remaining: int) -> None:
        self._remaining = remaining

    def read(self, amt: int) -> bytes:
        count = min(amt, self._remaining)
        self._remaining -= count
        return b"\0" * count

    def close(self) -> None:
        self._remaining = 0


class _ZeroS3Object:
    e_tag = '"zero"'

    def __init__(self, size: int) -> None:
        self.content_length = size

    def get(self, **kwargs):
        start = int(kwargs["Range"].removeprefix("bytes=").removesuffix("-"))
        return {
            "Body": _ZeroBody(self.content_length - start),
            "ContentRange": _content_range(start, self.content_length),
            "ETag": self.e_tag,
        }


@pytest.fixture
def s3_archive_media(settings, monkeypatch):
    """Archive media on an S3Storage whose objects are local fakes (no network)."""
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": "archive-media",
                "endpoint_url": "http://127.0.0.1:9",
                "access_key": "test",
                "secret_key": "test",
                "region_name": "us-east-1",
            },
        },
    }
    objects: dict[str, object] = {}

    def fake_open(self, name, mode="rb"):
        if name not in objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return SimpleNamespace(obj=objects[name], name=name)

    monkeypatch.setattr(S3Storage, "_open", fake_open)
    return objects


def test_s3_range_reader_streams_sequentially_with_one_request():
    obj = _FakeS3Object(PAYLOAD, piece=1000)
    reader = S3RangeReader(obj)

    assert reader.size == len(PAYLOAD)
    collected = bytearray()
    while chunk := reader.read(4096):
        collected += chunk

    assert bytes(collected) == PAYLOAD
    assert obj.ranges == ["bytes=0-"]
    assert reader.read(10) == b""
    reader.close()
    assert obj.bodies[0].closed is True


def test_s3_range_reader_seek_opens_a_new_range_and_never_reads_past_the_end():
    obj = _FakeS3Object(PAYLOAD)
    reader = S3RangeReader(obj)

    assert reader.read(10) == PAYLOAD[:10]
    assert reader.seek(20000) == 20000
    assert obj.bodies[0].closed is True
    assert reader.read(5) == PAYLOAD[20000:20005]
    assert reader.seek(-3, io.SEEK_END) == len(PAYLOAD) - 3
    assert reader.read(100) == PAYLOAD[-3:]
    assert reader.tell() == len(PAYLOAD)
    assert obj.ranges == ["bytes=0-", "bytes=20000-", f"bytes={len(PAYLOAD) - 3}-"]
    # The second body was only asked for the five bytes the caller wanted.
    assert obj.bodies[1].delivered == 5
    with pytest.raises(OSError):
        reader.seek(-1)


@pytest.mark.parametrize("error", [OSError, RuntimeError])
def test_s3_range_reader_discards_a_body_that_advances_before_raising(monkeypatch, error):
    obj = _FakeS3Object(PAYLOAD)
    with S3RangeReader(obj) as reader:
        assert reader.read(7) == PAYLOAD[:7]
        body = obj.bodies[0]
        original_read = body.read
        failure = error("read interrupted")

        def advance_then_fail(wanted):
            original_read(wanted)
            raise failure

        monkeypatch.setattr(body, "read", advance_then_fail)
        with pytest.raises(error) as caught:
            reader.read(3)

        assert caught.value is failure
        assert body.delivered == 10
        assert body.closed
        assert reader.tell() == 7
        assert reader.read(3) == PAYLOAD[7:10]
        assert obj.ranges == ["bytes=0-", "bytes=7-"]


def test_s3_range_reader_reports_a_short_object_and_safe_backend_errors():
    class ShortObject(_FakeS3Object):
        def get(self, **kwargs):
            return {
                "Body": _FakeBody(b"abc"),
                "ContentRange": _content_range(0, 6),
                "ETag": self.e_tag,
            }

    reader = S3RangeReader(ShortObject(b"abcdef"))
    assert reader.read(3) == b"abc"
    with pytest.raises(OSError, match="ended before its recorded length"):
        reader.read(3)

    class FailingObject(_FakeS3Object):
        def get(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "SlowDown", "Message": "CANARY_SECRET_KEY"}}, "GetObject"
            )

    with pytest.raises(OSError) as caught:
        S3RangeReader(FailingObject(b"abc")).read(3)
    assert "CANARY_SECRET_KEY" not in str(caught.value)


def test_s3_range_reader_pins_the_etag_and_passes_download_parameters():
    obj = _FakeS3Object(PAYLOAD)
    reader = S3RangeReader(
        obj, extra_args={"SSECustomerKey": "k", "SSECustomerAlgorithm": "AES256"}
    )
    reader.seek(7)
    assert reader.read(3) == PAYLOAD[7:10]
    assert obj.calls == [
        {
            "SSECustomerKey": "k",
            "SSECustomerAlgorithm": "AES256",
            "Range": "bytes=7-",
            "IfMatch": '"etag-1"',
        }
    ]


def test_s3_range_reader_refuses_an_object_without_etag():
    obj = _FakeS3Object(PAYLOAD)
    obj.e_tag = None
    with pytest.raises(OSError, match="no ETag"):
        S3RangeReader(obj)


def test_s3_range_reader_refuses_a_replaced_object():
    # HEAD said 3 bytes, but the GET answers for a 5-byte replacement.
    class ReplacedObject(_FakeS3Object):
        def get(self, **kwargs):
            body = _FakeBody(b"NEWER")
            self.bodies.append(body)
            return {"Body": body, "ContentRange": _content_range(0, 5), "ETag": self.e_tag}

    obj = ReplacedObject(b"old")
    with pytest.raises(OSError, match="changed while it was read"):
        S3RangeReader(obj).read(3)
    assert obj.bodies[0].closed is True
    assert obj.bodies[0].delivered == 0


@pytest.mark.parametrize("response_etag", ['"another-generation"', None])
def test_s3_range_reader_refuses_an_unverified_generation_with_a_matching_range(response_etag):
    class UnverifiedObject(_FakeS3Object):
        def get(self, **kwargs):
            response = super().get(**kwargs)
            if response_etag is None:
                del response["ETag"]
            else:
                response["ETag"] = response_etag
            return response

    obj = UnverifiedObject(PAYLOAD)
    with S3RangeReader(obj) as reader:
        reader.seek(7)
        with pytest.raises(OSError, match="changed while it was read"):
            reader.read(3)
        assert reader.tell() == 7
        assert obj.bodies[0].closed
        assert obj.bodies[0].delivered == 0


def test_real_s3_storage_open_streams_with_ranged_get_object(settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": "archive-media",
                "endpoint_url": "http://127.0.0.1:9",
                "access_key": "test",
                "secret_key": "test",
                "region_name": "us-east-1",
                "addressing_style": "path",
            },
        },
    }
    storage = storages["archive_media"]
    client = storage.connection.meta.client
    item = Item(archived_audio_path="items/1/audio/source.mp3")
    with Stubber(client) as stubber:
        stubber.add_response(
            "head_object",
            {"ContentLength": len(PAYLOAD), "ContentType": "audio/mpeg", "ETag": '"abc"'},
            {"Bucket": "archive-media", "Key": "items/1/audio/source.mp3"},
        )
        stubber.add_response(
            "get_object",
            {
                "Body": StreamingBody(io.BytesIO(PAYLOAD[100:]), len(PAYLOAD) - 100),
                "ContentRange": _content_range(100, len(PAYLOAD)),
                "ETag": '"abc"',
            },
            {
                "Bucket": "archive-media",
                "Key": "items/1/audio/source.mp3",
                "Range": "bytes=100-",
                "IfMatch": '"abc"',
            },
        )
        reader = open_archived_audio(item)
        assert isinstance(reader, S3RangeReader)
        assert reader.size == len(PAYLOAD)
        reader.seek(100)
        assert reader.read(50) == PAYLOAD[100:150]
        reader.close()
        stubber.assert_no_pending_responses()


@pytest.mark.django_db
def test_missing_s3_object_is_a_media_archival_error(s3_archive_media):
    with pytest.raises(MediaArchivalError, match="Archived audio open failed"):
        open_archived_audio(Item(archived_audio_path="items/1/audio/missing.mp3"))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "method,header,status,expected,ranges",
    [
        ("GET", "bytes=4000-9000", 206, PAYLOAD[4000:9001], ["bytes=4000-"]),
        ("GET", None, 200, PAYLOAD, ["bytes=0-"]),
        ("HEAD", None, 200, b"", []),
    ],
)
def test_public_archived_audio_from_s3_reads_only_the_served_bytes(
    client, s3_archive_media, method, header, status, expected, ranges
):
    obj = _FakeS3Object(PAYLOAD, piece=3000)
    s3_archive_media["items/1/audio/source.mp3"] = obj
    item = Item.objects.create(
        original_url="https://example.com/long",
        kind=ItemKind.PODCAST_EPISODE,
        is_public=True,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_size_bytes=len(PAYLOAD),
        archived_audio_content_type="audio/mpeg",
    )
    headers = {"HTTP_RANGE": header} if header else {}
    url = reverse("archive:item-archived-audio", kwargs={"pk": item.pk})
    response = getattr(client, method.lower())(url, **headers)

    assert response.status_code == status
    body = b"".join(response.streaming_content) if method == "GET" else b""
    response.close()
    assert body == expected
    assert obj.ranges == ranges
    assert sum(b.delivered for b in obj.bodies) == len(expected)
    assert all(b.closed for b in obj.bodies)


def test_ensure_temp_space_keeps_the_reserve(settings, monkeypatch):
    settings.ARCHIVE_MEDIA_TEMP_RESERVE_BYTES = 100
    monkeypatch.setattr("archive.media_archival._temp_free_bytes", lambda: 1_000)
    ensure_temp_space(900, purpose="Test")
    with pytest.raises(MediaArchivalError, match="needs 1001 bytes of free temporary disk space"):
        ensure_temp_space(901, purpose="Test")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fields,needed",
    [
        # The test storage is a local directory on the temporary volume, so the
        # stored copies count too: 1 + 1 for audio, 2 + 2 for video.
        ({"audio_url": "https://cdn.example.com/long.mp3"}, 2),
        ({"media_url": "https://cdn.example.com/long.mp4", "kind": ItemKind.VIDEO}, 4),
        # A YouTube page keeps two temporary files but stores only the audio.
        ({"media_url": "https://www.youtube.com/watch?v=abc", "kind": ItemKind.VIDEO}, 3),
    ],
)
def test_archival_refuses_before_downloading_without_temp_space(
    settings, monkeypatch, fields, needed
):
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 1000
    settings.ARCHIVE_MEDIA_TEMP_RESERVE_BYTES = 0
    item = Item.objects.create(
        original_url="https://example.com/long",
        **{"kind": ItemKind.PODCAST_EPISODE, **fields},
    )
    monkeypatch.setattr("archive.media_archival._temp_free_bytes", lambda: needed * 1000 - 1)
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda *a, **k: pytest.fail("download started without temporary space"),
    )
    monkeypatch.setattr(
        "archive.media_archival.run_media_command",
        lambda *a, **k: pytest.fail("download started without temporary space"),
    )

    with pytest.raises(MediaArchivalError, match="free temporary disk space"):
        archive_item_audio(item=item)

    # One byte more and the same item is admitted (the download then fails here).
    monkeypatch.setattr("archive.media_archival._temp_free_bytes", lambda: needed * 1000)
    with pytest.raises(BaseException) as admitted:
        archive_item_audio(item=item)
    assert "download started without temporary space" in str(admitted.value)


def _long_item(size: int, **fields) -> Item:
    return Item.objects.create(
        original_url="https://example.com/long",
        kind=ItemKind.PODCAST_EPISODE,
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=size,
        **fields,
    )


@pytest.mark.django_db
def test_staging_refuses_without_temp_space_before_any_request(settings, monkeypatch):
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    settings.ARCHIVE_MEDIA_TEMP_RESERVE_BYTES = 0
    item = _long_item(5000)
    monkeypatch.setattr("archive.media_archival._temp_free_bytes", lambda: 4999)
    monkeypatch.setattr(
        "archive.transcriptions.urlopen", lambda *a, **k: pytest.fail("request was sent")
    )

    with pytest.raises(TranscriptionGenerationError, match="free temporary disk space"):
        _stage_archived_audio_upload(
            item=item, source=_select_transcription_source(item), timeout=30
        )


@pytest.mark.django_db
def test_staged_transcription_deadline_grows_with_the_archived_size(settings, monkeypatch):
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_UPLOAD_MIN_BYTES_PER_SECOND = 1024 * 1024
    item = _long_item(3 * GIB)
    seen = {}

    def capture(*, item, source, timeout, deadline):
        seen["timeout"] = timeout
        raise TranscriptionPending(BatchTranscriptionJob(job_id="job-1", job_ref="ref"))

    monkeypatch.setattr(
        "archive.transcriptions._request_staged_archived_audio_transcription", capture
    )
    with pytest.raises(TranscriptionPending):
        generate_item_transcript(item, timeout=60)

    # 3 GiB at 1 MiB/s: the staging step may take 3072 s, not just the 60 s timeout.
    assert 3000 < seen["timeout"] <= 3072
    assert staging_allowance_seconds(3 * GIB) == 3072


def test_object_storage_adds_no_stored_copies_to_the_temp_budget(s3_archive_media):
    from archive.media_archival import _stored_copies_on_temp_volume

    assert _stored_copies_on_temp_volume(2) == 0


def test_local_storage_on_another_volume_adds_no_stored_copies(settings, monkeypatch):
    from archive import media_archival

    real_stat = media_archival.Path.stat

    def fake_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if str(self) == media_archival.gettempdir():
            return SimpleNamespace(st_dev=result.st_dev + 1)
        return result

    monkeypatch.setattr(media_archival.Path, "stat", fake_stat)
    assert media_archival._stored_copies_on_temp_volume(2) == 0
    monkeypatch.setattr(media_archival.Path, "stat", real_stat)
    assert media_archival._stored_copies_on_temp_volume(2) == 2


def test_media_command_is_killed_when_its_directory_outgrows_the_budget(tmp_path):
    from archive.media_process import MediaProcessError, run_media_command

    writer = (
        "import sys, time\n"
        "with open(sys.argv[1], 'wb') as f:\n"
        "    for _ in range(400):\n"
        "        f.write(b'x' * 4096); f.flush(); time.sleep(0.01)\n"
    )
    with pytest.raises(MediaProcessError, match="output limit"):
        run_media_command(
            [sys.executable, "-c", writer, str(tmp_path / "source.webm")],
            timeout=30,
            output_dir=tmp_path,
            max_output_dir_bytes=64 * 1024,
        )
    # Killed early: far less than the 1.6 MB the command would have written.
    assert (tmp_path / "source.webm").stat().st_size < 1024 * 1024


def test_media_command_is_killed_when_one_file_outgrows_the_file_budget(tmp_path):
    from archive.media_process import MediaProcessError, run_media_command

    writer = (
        "import sys, time\n"
        "with open(sys.argv[1], 'wb') as f:\n"
        "    for _ in range(400):\n"
        "        f.write(b'x' * 4096); f.flush(); time.sleep(0.01)\n"
    )
    with pytest.raises(MediaProcessError, match="output limit"):
        run_media_command(
            [sys.executable, "-c", writer, str(tmp_path / "source.webm")],
            timeout=30,
            output_dir=tmp_path,
            max_output_dir_bytes=10 * 1024 * 1024,
            max_output_dir_file_bytes=64 * 1024,
        )
    assert (tmp_path / "source.webm").stat().st_size < 1024 * 1024


def test_temp_space_admission_refuses_a_lock_acquired_after_the_deadline(monkeypatch):
    from archive import media_archival

    calls = {"n": 0}
    real_flock = media_archival.fcntl.flock
    clock = {"now": 100.0}

    def flock(fd, op):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BlockingIOError
        return real_flock(fd, op)

    def sleep(seconds):
        clock["now"] = 200.0  # the holder releases only after the deadline (150)

    monkeypatch.setattr(media_archival.fcntl, "flock", flock)
    monkeypatch.setattr(media_archival, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(media_archival, "sleep", sleep)
    with pytest.raises(MediaArchivalError, match="timed out waiting"):
        with media_archival.temp_space_admission(1, purpose="Test", timeout=50.0):
            pytest.fail("admitted after the deadline")
    assert calls["n"] == 2


def test_temp_space_admission_serializes_media_work_across_processes(tmp_path, monkeypatch):
    import subprocess
    import time

    from django.conf import settings

    from archive import media_archival

    # Not the temporary directory: workers with private /tmp directories share it.
    lock_path = tmp_path / "shared" / "db.sqlite3.media-temp-space.lock"
    lock_path.parent.mkdir()
    settings.ARCHIVE_MEDIA_TEMP_LOCK_PATH = str(lock_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "private-tmp"))
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(30)\n",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(MediaArchivalError, match="waiting for other media work"):
            with media_archival.temp_space_admission(1, purpose="Test", timeout=0.6):
                pytest.fail("admitted while another process held the lock")
        assert time.monotonic() - started >= 0.5
    finally:
        holder.kill()
        holder.wait(timeout=10)
    # Released by the holder's exit: the next admission proceeds and releases again.
    assert media_archival.settings.ARCHIVE_MEDIA_TEMP_LOCK_PATH == str(lock_path)
    with media_archival.temp_space_admission(1, purpose="Test", timeout=5):
        pass
    with media_archival.temp_space_admission(1, purpose="Test", timeout=5):
        pass


@pytest.mark.django_db
@pytest.mark.parametrize("original_size,accepted", [(1500, False), (900, True)])
def test_video_page_original_over_the_limit_fails_even_after_conversion(
    settings, monkeypatch, tmp_path, original_size, accepted
):
    from archive import media_archival

    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 1000
    # A real child: yt-dlp would keep (keepvideo) the original next to its MP3.
    writer = (
        "import json, sys\n"
        "options = json.load(sys.stdin)['options']\n"
        "assert options['keepvideo'] is True\n"
        "directory = sys.argv[1]\n"
        f"open(directory + '/source.m4a', 'wb').write(b'o' * {original_size})\n"
        "open(directory + '/source.mp3', 'wb').write(b'm' * 400)\n"
    )
    monkeypatch.setattr(
        media_archival,
        "_video_page_download_command",
        lambda: [sys.executable, "-c", writer, str(tmp_path)],
    )
    if accepted:
        path, content_type, output = media_archival._download_supported_video_page_audio_source(
            source_url="https://www.youtube.com/watch?v=abc", temp_dir=tmp_path, timeout=30
        )
        assert (path.name, content_type, output.st_size) == ("source.mp3", "audio/mpeg", 400)
    else:
        with pytest.raises(MediaArchivalError, match="1000-byte archive limit"):
            media_archival._download_supported_video_page_audio_source(
                source_url="https://www.youtube.com/watch?v=abc", temp_dir=tmp_path, timeout=30
            )


@pytest.mark.django_db
def test_video_page_download_over_the_directory_budget_is_an_archive_limit(
    settings, monkeypatch, tmp_path
):
    from archive import media_archival
    from archive.media_process import MediaProcessError

    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 1000
    seen = {}

    def over_budget(command, **kwargs):
        seen.update(kwargs)
        raise MediaProcessError("output limit")

    monkeypatch.setattr(media_archival, "run_media_command", over_budget)
    with pytest.raises(MediaArchivalError, match="1000-byte archive limit"):
        media_archival._download_supported_video_page_audio_source(
            source_url="https://www.youtube.com/watch?v=abc", temp_dir=tmp_path, timeout=30
        )
    assert seen["output_dir"] == tmp_path
    assert seen["max_output_dir_bytes"] == 2000
    assert seen["max_output_dir_file_bytes"] == 1000


@pytest.mark.django_db
def test_sync_sized_archived_audio_keeps_the_stage_timeout(settings, monkeypatch):
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_UPLOAD_MIN_BYTES_PER_SECOND = 1024
    item = _long_item(20 * 1024 * 1024)
    seen = {}

    def capture(*, media, prompt, timeout, deadline):
        seen["timeout"] = timeout
        return "short transcript"

    monkeypatch.setattr(
        "archive.transcriptions._read_archived_media",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr("archive.transcriptions._request_transcription", capture)
    assert generate_item_transcript(item, timeout=60) == "short transcript"
    # 20 MiB at 1 KiB/s would be 20480 s; the synchronous request keeps 60 s.
    assert 50 < seen["timeout"] <= 60


@pytest.mark.django_db
def test_small_archived_audio_keeps_the_stage_timeout(settings, monkeypatch):
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    item = _long_item(30 * 1024 * 1024)
    seen = {}

    def capture(*, item, source, timeout, deadline):
        seen["timeout"] = timeout
        raise TranscriptionPending(BatchTranscriptionJob(job_id="job-1", job_ref="ref"))

    monkeypatch.setattr(
        "archive.transcriptions._request_staged_archived_audio_transcription", capture
    )
    with pytest.raises(TranscriptionPending):
        generate_item_transcript(item, timeout=600)
    assert 590 < seen["timeout"] <= 600


@pytest.mark.django_db
@pytest.mark.parametrize("age,still_pending", [(7200 + 60, True), (12 * 3600 + 60, False)])
def test_batch_job_age_limit_grows_with_the_recording_size(settings, age, still_pending):
    settings.ARCHIVE_TRANSCRIPTION_MAX_JOB_AGE_SECONDS = 7200
    settings.ARCHIVE_TRANSCRIPTION_JOB_MIN_BYTES_PER_SECOND = 16 * 1024
    # 16 KiB/s over 12 hours: the job may run that long before it is given up.
    size = 16 * 1024 * 12 * 3600
    item = _long_item(
        size,
        transcript_status=EnrichmentStatus.PROCESSING,
        transcript_job_id="job-1",
        transcript_job_ref="ref-1",
        transcript_job_submitted_at=timezone.now() - timedelta(seconds=age),
    )
    from archive.services import TRANSCRIPT_JOB_FIELDS, TRANSCRIPT_SOURCE_FIELDS, _result_state

    result_state = _result_state(
        item, "transcript", (*TRANSCRIPT_SOURCE_FIELDS, *TRANSCRIPT_JOB_FIELDS)
    )
    _mark_transcript_pending(
        item,
        TranscriptionPending(BatchTranscriptionJob(job_id="job-1", job_ref="ref-1")),
        expected=result_state,
    )
    item.refresh_from_db()
    if still_pending:
        assert item.transcript_status == EnrichmentStatus.PENDING
        assert item.transcript_job_id == "job-1"
    else:
        assert item.transcript_status == EnrichmentStatus.FAILED
        assert "did not finish within 43200 s" in item.transcript_error


def test_worker_defaults_cover_long_media(settings):
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 16 * GIB
    settings.ARCHIVE_TRANSCRIPTION_UPLOAD_MIN_BYTES_PER_SECOND = 4 * 1024 * 1024
    command = WorkerCommand()
    parser = command.create_parser("manage.py", "run_metadata_worker")
    options = vars(parser.parse_args([]))

    assert options["media_archive_timeout"] == 3600
    assert options["transcription_timeout"] == 1800
    # Staging 16 GiB at 4 MiB/s may take 4096 s, longer than the 1800 s timeout.
    assert command._stale_processing_after_seconds(options) == 2 * (
        15 + 60 + 4096 + 3600 + WorkerCommand.DEFAULT_ARTICLE_AUDIO_TIMEOUT
    )


def test_default_limits_match_voxhelm_staged_uploads():
    from config.settings import base

    assert base.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES == 16 * GIB
    assert base.ARCHIVE_TRANSCRIPTION_UPLOAD_MIN_BYTES_PER_SECOND == 4 * 1024 * 1024
    assert base.ARCHIVE_TRANSCRIPTION_JOB_MIN_BYTES_PER_SECOND == 16 * 1024


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", True, 2.0])
def test_minimum_rates_must_be_positive_integers(value):
    with pytest.raises(ImproperlyConfigured):
        transcription_min_bytes_per_second(value, name="RATE")


def test_size_scaled_seconds_rounds_up():
    assert size_scaled_seconds(0, 10) == 0
    assert size_scaled_seconds(1, 10) == 1
    assert size_scaled_seconds(20, 10) == 2
    assert size_scaled_seconds(21, 10) == 3


@pytest.fixture
def counting_voxhelm():
    received = SimpleNamespace(length=0, declared=0, head=b"", tail=b"")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            declared = int(self.headers["Content-Length"])
            received.declared = declared
            remaining = declared
            tail = b""
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                if not received.head:
                    received.head = chunk[:200]
                tail = (tail + chunk)[-200:]
                received.length += len(chunk)
                remaining -= len(chunk)
            received.tail = tail
            payload = json.dumps({"id": "upload-long"}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    received.url = f"http://127.0.0.1:{server.server_port}/v1"
    yield received
    server.shutdown()
    server.server_close()
    thread.join()


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


@pytest.mark.django_db
def test_multi_gigabyte_archived_audio_stages_end_to_end_without_buffering(
    settings, monkeypatch, tmp_path, s3_archive_media, counting_voxhelm
):
    # Past both the old 250 MiB limit and the signed 32-bit byte boundary.
    size = 2 * GIB + 3
    if shutil.disk_usage(tmp_path).free < size + 4 * GIB:
        pytest.skip("not enough local disk for the synthetic multi-gigabyte upload")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = counting_voxhelm.url
    s3_archive_media["items/1/audio/source.mp3"] = _ZeroS3Object(size)
    item = _long_item(size)
    before = _peak_rss_bytes()

    upload_id = _stage_archived_audio_upload(
        item=item, source=_select_transcription_source(item), timeout=900
    )

    assert upload_id == "upload-long"
    assert counting_voxhelm.length == counting_voxhelm.declared
    assert counting_voxhelm.declared > size
    assert b'name="file"; filename="source.mp3"' in counting_voxhelm.head
    assert counting_voxhelm.tail.endswith(b"--\r\n")
    # The whole object was never held in memory.
    assert _peak_rss_bytes() - before < 512 * 1024 * 1024
    # The spooled request body was removed after the exchange.
    assert not list(tmp_path.glob("archive-upstream-*"))


@pytest.fixture
def unsized_audio_server():
    """Serves N bytes of audio without a Content-Length (close-delimited)."""
    sent: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def _size(self) -> int:
            return int(self.path.split("/")[-1].split(".")[0])

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            remaining = self._size()
            written = 0
            try:
                while remaining:
                    chunk = min(remaining, 64 * 1024)
                    self.wfile.write(b"\xff" * chunk)
                    self.wfile.flush()
                    written += chunk
                    remaining -= chunk
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                sent.append(written)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", sent=sent)
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("size,exit_code", [(64 * 1024 * 1024, 3), (100_000, 0)])
def test_real_ytdlp_child_stops_an_unsized_source_over_the_limit(
    unsized_audio_server, tmp_path, size, exit_code
):
    import subprocess
    from pathlib import Path

    from archive import media_archival, video_page_download

    out = tmp_path / "out"
    out.mkdir()
    request = {
        "url": f"{unsized_audio_server.url}/{size}.mp3",
        "options": {
            "outtmpl": str(out / "source.%(ext)s"),
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "nopart": True,
            # yt-dlp's own limit cannot act without a Content-Length.
            "max_filesize": 200_000,
        },
        "max_source_bytes": 200_000,
    }
    result = subprocess.run(
        [sys.executable, "-I", str(Path(video_page_download.__file__))],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert result.returncode == exit_code, result.stderr[-2000:]
    assert media_archival.VIDEO_PAGE_SOURCE_LIMIT_EXIT_CODE == (
        video_page_download.SOURCE_LIMIT_EXIT_CODE
    )
    if exit_code == 0:
        assert (out / "source.mp3").stat().st_size == size
    else:
        # Stopped while downloading: the server never got to send the whole body,
        # and no file on disk holds it.
        for _ in range(100):
            if unsized_audio_server.sent:
                break
            time.sleep(0.05)
        assert unsized_audio_server.sent and max(unsized_audio_server.sent) < size
        assert all(path.stat().st_size < size for path in out.iterdir())


def test_postprocessor_hook_refuses_an_oversized_file_before_any_postprocessor(tmp_path):
    from archive.video_page_download import SourceLimitError, output_limit_postprocessor_hook

    (tmp_path / "source.m4a").write_bytes(b"x" * 11)
    exceeded: list[bool] = []
    hook = output_limit_postprocessor_hook(str(tmp_path), 10, exceeded)
    hook({"status": "finished", "postprocessor": "FixupM4a"})
    assert exceeded == []
    with pytest.raises(SourceLimitError):
        hook({"status": "started", "postprocessor": "FixupM4a"})
    assert exceeded == [True]


def test_source_limit_hook_measures_the_finished_file_on_disk(tmp_path):
    from archive.video_page_download import SourceLimitError, source_limit_hook

    downloaded = tmp_path / "source.m4a"
    downloaded.write_bytes(b"x" * 11)
    exceeded: list[bool] = []
    hook = source_limit_hook(10, exceeded)
    hook({"status": "downloading", "downloaded_bytes": 10})
    assert exceeded == []
    with pytest.raises(SourceLimitError):
        # The progress counter may lag; the finished file on disk decides.
        hook({"status": "finished", "downloaded_bytes": 5, "filename": str(downloaded)})
    assert exceeded == [True]


@pytest.mark.django_db
def test_video_page_child_limit_status_is_reported_as_the_archive_limit(
    settings, monkeypatch, tmp_path
):
    from archive import media_archival

    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 1000
    monkeypatch.setattr(
        media_archival,
        "_video_page_download_command",
        lambda: [
            sys.executable,
            "-c",
            "import json, sys\n"
            "assert json.load(sys.stdin)['max_source_bytes'] == 1000\n"
            "sys.exit(3)\n",
        ],
    )
    with pytest.raises(MediaArchivalError, match="1000-byte archive limit"):
        media_archival._download_supported_video_page_audio_source(
            source_url="https://www.youtube.com/watch?v=abc", temp_dir=tmp_path, timeout=30
        )


def test_real_ytdlp_stops_before_the_first_postprocessor_without_progress_callbacks(
    unsized_audio_server, tmp_path, monkeypatch
):
    from yt_dlp.downloader.common import FileDownloader

    from archive import video_page_download

    # A downloader that reports no progress (as FFmpeg-delegated HLS does).
    monkeypatch.setattr(FileDownloader, "_hook_progress", lambda self, *args, **kwargs: None)
    out = tmp_path / "out"
    out.mkdir()
    marker = tmp_path / "postprocessor-ran"
    request = {
        "url": f"{unsized_audio_server.url}/300000.mp3",
        "options": {
            "outtmpl": str(out / "source.%(ext)s"),
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "nopart": True,
            "postprocessors": [
                {
                    "key": "Exec",
                    "exec_cmd": [
                        shlex.join(
                            [
                                sys.executable,
                                "-c",
                                "import sys; open(sys.argv[1], 'w').close()",
                                str(marker),
                            ]
                        )
                    ],
                    "when": "post_process",
                }
            ],
        },
        "max_source_bytes": 200_000,
    }
    exceeded: list[bool] = []
    with pytest.raises(Exception):  # noqa: B017 - yt-dlp wraps the hook's error
        video_page_download.download(request, exceeded)

    assert exceeded == [True]
    assert not marker.exists()

    # Within the limit the same postprocessor runs, so the hook is what stopped it.
    request["url"] = f"{unsized_audio_server.url}/100000.mp3"
    request["options"]["outtmpl"] = str(tmp_path / "ok" / "source.%(ext)s")
    (tmp_path / "ok").mkdir()
    video_page_download.download(request, exceeded := [])
    assert exceeded == []
    assert marker.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("admission_seconds", [9, 11])
def test_staging_budget_is_recomputed_after_disk_admission(
    settings, monkeypatch, admission_seconds
):
    from archive import media_archival, transcriptions

    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    settings.ARCHIVE_TRANSCRIPTION_API_BASE = "https://voxhelm.example.test/v1"
    item = _long_item(4)
    clock = 100.0
    sends = []

    def disk_space():
        nonlocal clock
        clock += admission_seconds
        return 10**15

    def send(*, item, source, suffix, timeout, deadline):
        sends.append((timeout, deadline))
        return "upload-1"

    monkeypatch.setattr(media_archival, "monotonic", lambda: clock)
    monkeypatch.setattr(transcriptions, "monotonic", lambda: clock)
    monkeypatch.setattr(media_archival, "_temp_free_bytes", disk_space)
    monkeypatch.setattr(transcriptions, "_send_staged_upload", send)

    if admission_seconds > 10:
        with pytest.raises(TranscriptionGenerationError, match="pipeline timeout"):
            _stage_archived_audio_upload(
                item=item, source=_select_transcription_source(item), timeout=10
            )
        assert sends == []
    else:
        assert (
            _stage_archived_audio_upload(
                item=item, source=_select_transcription_source(item), timeout=10
            )
            == "upload-1"
        )
        assert sends == [(1, 110)]
