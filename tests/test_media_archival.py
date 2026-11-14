from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import storages

from archive.media_archival import (
    MediaArchivalError,
    _looks_like_supported_video_page_url,
    archive_item_audio,
    can_archive_audio,
)
from archive.media_storage import iter_archive_media_object_names
from archive.models import Item, ItemKind, PendingArchiveMediaUpload


@pytest.fixture(autouse=True)
def isolated_media_storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }


class _FakeHeaders:
    def __init__(self, content_type: str) -> None:
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        content_type: str = "audio/mpeg",
        final_url: str = "https://cdn.example.com/audio.mp3",
    ) -> None:
        self._chunks = list(chunks)
        self.headers = _FakeHeaders(content_type)
        self._final_url = final_url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _TrackingSpool:
    instances: list[_TrackingSpool] = []

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        _TrackingSpool.instances.append(self)

    def write(self, _chunk: bytes) -> None:
        return None

    def seek(self, _position: int) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeYoutubeDL:
    instances: list[_FakeYoutubeDL] = []
    payload = b"youtube-page-video"
    suffix = ".mp4"
    error: Exception | None = None

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        type(self).instances.append(self)

    def __enter__(self) -> _FakeYoutubeDL:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def extract_info(self, source_url: str, download: bool):
        assert download is True
        if self.error is not None:
            raise self.error
        output_template = str(self.options["outtmpl"])
        output_path = Path(output_template.replace("%(ext)s", self.suffix.lstrip(".")))
        output_path.write_bytes(self.payload)
        return {"webpage_url": source_url}


def _fake_ffmpeg_run(command, **kwargs) -> None:
    output_path = command[-1]
    with open(output_path, "wb") as output_file:
        output_file.write(b"ID3-derived-audio")


def _failing_ffmpeg_run(command, **kwargs) -> None:
    from archive.media_process import MediaProcessError

    raise MediaProcessError("failed")


def _delete_storage_objects(*paths: str) -> None:
    for path in paths:
        if storages["archive_media"].exists(path):
            storages["archive_media"].delete(path)


@pytest.mark.django_db
def test_can_archive_audio_accepts_direct_video_media_url() -> None:
    item = Item(
        original_url="https://example.com/watch/demo",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )

    assert can_archive_audio(item) is True


@pytest.mark.django_db
def test_can_archive_audio_accepts_supported_youtube_page_url() -> None:
    item = Item(
        original_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
    )

    assert can_archive_audio(item) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=demo123",
        "https://m.youtube.com/watch?v=demo123",
        "http://www.youtube.com/watch?v=demo123",
        "https://www.youtube.com/shorts/demo123",
        "https://www.youtube.com/live/demo123",
        "https://www.youtube.com/embed/demo123",
        "https://youtu.be/demo123",
    ],
)
def test_supported_video_page_url_matcher_accepts_documented_youtube_shapes(url: str) -> None:
    assert _looks_like_supported_video_page_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@archive/videos",
        "https://youtu.be/",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch/",
        "https://www.youtube.com/",
    ],
)
def test_supported_video_page_url_matcher_rejects_non_video_youtube_pages(url: str) -> None:
    assert _looks_like_supported_video_page_url(url) is False


@pytest.mark.django_db
def test_can_archive_audio_rejects_non_video_youtube_pages() -> None:
    item = Item(
        original_url="https://www.youtube.com/@archive/videos",
        kind=ItemKind.VIDEO,
    )

    assert can_archive_audio(item) is False


@pytest.mark.django_db
def test_can_archive_audio_accepts_explicit_audio_webm_url() -> None:
    item = Item(
        original_url="https://example.com/listen",
        audio_url="https://cdn.example.com/audio.webm",
        kind=ItemKind.PODCAST_EPISODE,
    )

    assert can_archive_audio(item) is True


@pytest.mark.django_db
def test_archive_item_audio_closes_spool_when_download_exceeds_size_limit(
    monkeypatch,
    settings,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 6
    _TrackingSpool.instances.clear()
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse([b"1234", b"5678"]),
    )
    monkeypatch.setattr("archive.media_archival.SpooledTemporaryFile", _TrackingSpool)

    with pytest.raises(MediaArchivalError, match="archive limit"):
        archive_item_audio(item=item)

    assert len(_TrackingSpool.instances) == 1
    assert _TrackingSpool.instances[0].closed is True


@pytest.mark.django_db
def test_archive_item_audio_rejects_video_downloads_over_size_limit(
    monkeypatch,
    settings,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.mp4",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 6
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"1234", b"5678"],
            content_type="video/mp4",
            final_url="https://cdn.example.com/video.mp4",
        ),
    )

    with pytest.raises(MediaArchivalError, match="Video source exceeded"):
        archive_item_audio(item=item)

    assert not list(iter_archive_media_object_names())
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.mark.django_db
def test_archive_item_audio_extracts_audio_from_direct_video_and_tracks_source(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.mp4",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"video-bytes-1", b"video-bytes-2"],
            content_type="video/mp4",
            final_url="https://cdn.example.com/video.mp4",
        ),
    )
    monkeypatch.setattr("archive.media_archival.run_media_command", _fake_ffmpeg_run)

    archived_audio = archive_item_audio(item=item)

    assert archived_audio.object_name.startswith(f"items/{item.pk}/audio/")
    assert archived_audio.object_name.endswith("/extracted.mp3")
    assert archived_audio.content_type == "audio/mpeg"
    assert archived_audio.size_bytes == len(b"ID3-derived-audio")
    assert archived_audio.source_object_name.startswith(f"items/{item.pk}/video/")
    assert archived_audio.source_object_name.endswith("/source.mp4")
    assert archived_audio.source_content_type == "video/mp4"
    assert archived_audio.source_size_bytes == len(b"video-bytes-1video-bytes-2")
    assert storages["archive_media"].exists(archived_audio.object_name) is True
    assert storages["archive_media"].exists(archived_audio.source_object_name) is True


@pytest.mark.django_db
def test_archive_item_audio_prefers_video_path_for_webm_video_urls(monkeypatch) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.webm",
        media_url="https://cdn.example.com/video.webm",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.webm",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"video-webm"],
            content_type="video/webm",
            final_url="https://cdn.example.com/video.webm",
        ),
    )
    monkeypatch.setattr("archive.media_archival.run_media_command", _fake_ffmpeg_run)

    archived_audio = archive_item_audio(item=item)

    assert archived_audio.object_name.startswith(f"items/{item.pk}/audio/")
    assert archived_audio.object_name.endswith("/extracted.mp3")
    assert archived_audio.source_object_name.startswith(f"items/{item.pk}/video/")
    assert archived_audio.source_object_name.endswith("/source.webm")


@pytest.fixture
def fake_ytdlp(monkeypatch):
    import json

    from archive import video_page_download
    from archive.media_process import MediaProcessError

    monkeypatch.setattr(video_page_download, "yt_dlp", SimpleNamespace(YoutubeDL=_FakeYoutubeDL))

    def run(command, *, input_data, timeout):
        try:
            video_page_download.download(json.loads(input_data))
        except Exception:
            raise MediaProcessError("failed") from None

    monkeypatch.setattr("archive.media_archival.run_media_command", run)


@pytest.mark.django_db
def test_archive_item_audio_downloads_supported_youtube_page_audio_directly(
    fake_ytdlp,
    monkeypatch,
    settings,
) -> None:
    item = Item.objects.create(
        original_url="https://www.youtube.com/watch?v=demo123",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.mp4",
        f"items/{item.pk}/audio/source.mp3",
    )
    _FakeYoutubeDL.instances.clear()
    _FakeYoutubeDL.payload = b"youtube-page-audio"
    _FakeYoutubeDL.suffix = ".mp3"
    _FakeYoutubeDL.error = None
    monkeypatch.setattr("archive.media_archival.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.ARCHIVE_MEDIA_YTDLP_JS_RUNTIMES = ["node", "deno"]

    archived_audio = archive_item_audio(item=item)

    assert archived_audio.object_name.startswith(f"items/{item.pk}/audio/")
    assert archived_audio.object_name.endswith("/source.mp3")
    assert archived_audio.content_type == "audio/mpeg"
    assert archived_audio.size_bytes == len(b"youtube-page-audio")
    assert archived_audio.source_object_name == ""
    assert archived_audio.source_content_type == ""
    assert archived_audio.source_size_bytes == 0
    assert (
        _FakeYoutubeDL.instances[0].options["format"]
        == "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio[ext=mp4]/bestaudio"
    )
    assert _FakeYoutubeDL.instances[0].options["ffmpeg_location"] == "/usr/bin"
    assert _FakeYoutubeDL.instances[0].options["postprocessors"] == [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "2",
        }
    ]
    assert _FakeYoutubeDL.instances[0].options["postprocessor_args"] == {
        "extractaudio+ffmpeg_o": ["-fs", str(settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES)]
    }
    assert _FakeYoutubeDL.instances[0].options["noprogress"] is True
    assert _FakeYoutubeDL.instances[0].options["quiet"] is True
    assert _FakeYoutubeDL.instances[0].options["js_runtimes"] == {
        "node": {},
        "deno": {},
    }
    assert storages["archive_media"].exists(archived_audio.object_name) is True
    assert storages["archive_media"].exists(f"items/{item.pk}/video/source.mp4") is False


@pytest.mark.django_db
def test_archive_item_audio_reports_supported_youtube_page_download_failures(fake_ytdlp) -> None:
    item = Item.objects.create(
        original_url="https://youtu.be/demo123",
        kind=ItemKind.VIDEO,
    )
    _FakeYoutubeDL.instances.clear()
    _FakeYoutubeDL.error = RuntimeError("resolver exploded")

    with pytest.raises(MediaArchivalError, match="Video page download failed"):
        archive_item_audio(item=item)


@pytest.mark.django_db
def test_archive_item_audio_preserves_stale_video_until_publication(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode.mp3",
        audio_url="https://cdn.example.com/audio.mp3",
        kind=ItemKind.PODCAST_EPISODE,
        archived_video_path="items/1/video/source.mp4",
    )
    storages["archive_media"].save(item.archived_video_path, ContentFile(b"old-video"))
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"fresh-audio"],
            content_type="audio/mpeg",
            final_url="https://cdn.example.com/audio.mp3",
        ),
    )

    archived_audio = archive_item_audio(item=item)

    assert archived_audio.object_name.startswith(f"items/{item.pk}/audio/")
    assert archived_audio.object_name.endswith("/source.mp3")
    assert storages["archive_media"].exists(item.archived_video_path) is True


@pytest.mark.django_db
def test_archive_item_audio_cleans_up_failed_video_archival_before_persisting_objects(
    monkeypatch,
) -> None:
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        media_url="https://cdn.example.com/video.mp4",
        kind=ItemKind.VIDEO,
    )
    _delete_storage_objects(
        f"items/{item.pk}/video/source.mp4",
        f"items/{item.pk}/audio/extracted.mp3",
    )
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"video-bytes"],
            content_type="video/mp4",
            final_url="https://cdn.example.com/video.mp4",
        ),
    )
    monkeypatch.setattr("archive.media_archival.run_media_command", _failing_ffmpeg_run)

    with pytest.raises(MediaArchivalError, match="Video audio extraction failed"):
        archive_item_audio(item=item)

    assert not list(iter_archive_media_object_names())
    assert not PendingArchiveMediaUpload.objects.exists()


@pytest.fixture
def ffmpeg_script(settings, tmp_path):
    import sys

    def install(body):
        script = tmp_path / "synthetic-ffmpeg"
        script.write_text(f"#!{sys.executable}\n" + body)
        script.chmod(0o700)
        settings.ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN = str(script)
        return script

    return install


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["oversize", "boundary", "stdout", "stderr", "failure"])
def test_extraction_limits_preserve_previous_media(
    mode, monkeypatch, settings, ffmpeg_script, tmp_path
):
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 1024
    item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        kind=ItemKind.VIDEO,
        archived_audio_path="old/audio.mp3",
        archived_video_path="old/video.mp4",
    )
    storage = storages["archive_media"]
    for path in (item.archived_audio_path, item.archived_video_path):
        storage.save(path, ContentFile(b"previous"))
    output_record = tmp_path / "output-path"
    body = (
        "import sys\nfrom pathlib import Path\n"
        f"Path({str(output_record)!r}).write_text(sys.argv[-1])\n"
        "Path(sys.argv[-1]).write_bytes(b'x' * "
        + ("2048" if mode == "oversize" else "1024" if mode == "boundary" else "16")
        + ")\n"
    )
    if mode in {"stdout", "stderr"}:
        body += f"sys.{mode}.write('https://private.invalid/secret\\n' * 100000)\n"
    if mode == "failure":
        body += "sys.stderr.write('https://private.invalid/secret')\nsys.exit(1)\n"
    ffmpeg_script(body)
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"video"], content_type="video/mp4", final_url=item.original_url
        ),
    )
    expected = {
        "oversize": "Audio extraction exceeded the configured output limit",
        "boundary": "Audio extraction exceeded the configured output limit",
        "stdout": "Video audio extraction exceeded the diagnostic limit",
        "stderr": "Video audio extraction exceeded the diagnostic limit",
        "failure": "Video audio extraction failed",
    }[mode]
    with pytest.raises(MediaArchivalError) as caught:
        archive_item_audio(item=item)
    assert str(caught.value) == expected
    assert caught.value.__cause__ is None
    item.refresh_from_db()
    assert item.archived_audio_path == "old/audio.mp3"
    assert item.archived_video_path == "old/video.mp4"
    assert sorted(iter_archive_media_object_names()) == ["old/audio.mp3", "old/video.mp4"]
    with storage.open("old/audio.mp3") as previous:
        assert previous.read() == b"previous"
    assert not PendingArchiveMediaUpload.objects.exists()
    assert not Path(output_record.read_text()).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("phase", ["download", "postprocess"])
def test_video_page_download_enforces_elapsed_deadline(phase, monkeypatch, tmp_path):
    import sys
    from time import monotonic

    from archive.services import enrich_item_media_archive

    script = tmp_path / "slow-downloader.py"
    script.write_text(
        "import json,sys,time\nfrom pathlib import Path\n"
        "request=json.load(sys.stdin)\n"
        "path=Path(request['options']['outtmpl'].replace('%(ext)s', 'mp3'))\n"
        + ("path.write_bytes(b'ID3')\n" if phase == "postprocess" else "")
        + "while True:\n time.sleep(.01)\n sys.stdout.write('progress\\n')\n sys.stdout.flush()\n"
    )
    monkeypatch.setattr(
        "archive.media_archival._video_page_download_command",
        lambda: [sys.executable, "-I", str(script)],
    )
    item = Item.objects.create(original_url="https://youtu.be/demo123", kind=ItemKind.VIDEO)
    started = monotonic()
    assert not enrich_item_media_archive(item=item, timeout=0.2)
    assert monotonic() - started < 1
    assert not list(iter_archive_media_object_names())
    assert not PendingArchiveMediaUpload.objects.exists()
    item.refresh_from_db()
    assert not item.archived_audio_path
    assert item.media_archive_error == "Video page download timed out"
    assert item.media_archive_retry_count == 1 and item.media_archive_retry_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_noisy_ffmpeg_persists_only_fixed_failure(stream, monkeypatch, ffmpeg_script, caplog):
    from archive.services import enrich_item_media_archive

    ffmpeg_script(f"import sys\nsys.{stream}.write('https://private.invalid/secret\\n' * 100000)\n")
    item = Item.objects.create(original_url="https://example.test/a.mp4", kind=ItemKind.VIDEO)
    monkeypatch.setattr(
        "archive.media_archival._open_direct_media",
        lambda request, timeout: _FakeResponse(
            [b"video"], content_type="video/mp4", final_url=item.original_url
        ),
    )
    assert not enrich_item_media_archive(item)
    item.refresh_from_db()
    assert item.media_archive_error == "Video audio extraction exceeded the diagnostic limit"
    assert "private.invalid" not in caplog.text
    assert not list(iter_archive_media_object_names())
    assert not item.archived_audio_path and not item.archived_video_path
    assert item.media_archive_retry_count == 1 and item.media_archive_retry_at is not None


@pytest.mark.parametrize("limited", [False, True])
def test_real_ffmpeg_synthetic_audio_respects_output_ceiling(limited, settings, tmp_path):
    import shutil
    import wave

    from archive.media_archival import _extract_audio_with_ffmpeg

    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("ffmpeg is required for the real local encoding oracle")
    settings.ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN = binary
    settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = 4096 if limited else 1024 * 1024
    source = tmp_path / "synthetic.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * (16000 * 30))
    output = tmp_path / "out.mp3"
    if limited:
        with pytest.raises(MediaArchivalError, match="configured output limit"):
            _extract_audio_with_ffmpeg(source_path=source, output_path=output, timeout=5)
        assert 0 < output.stat().st_size < 64 * 1024
    else:
        _extract_audio_with_ffmpeg(source_path=source, output_path=output, timeout=5)
        assert 0 < output.stat().st_size < settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES
