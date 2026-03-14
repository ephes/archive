from __future__ import annotations

import pytest

from archive.media_archival import MediaArchivalError, archive_item_audio
from archive.models import Item, ItemKind


class _FakeHeaders:
    def get_content_type(self) -> str:
        return "audio/mpeg"


class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.headers = _FakeHeaders()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def geturl(self) -> str:
        return "https://cdn.example.com/audio.mp3"

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
        "archive.media_archival.urlopen",
        lambda request, timeout: _FakeResponse([b"1234", b"5678"]),
    )
    monkeypatch.setattr("archive.media_archival.SpooledTemporaryFile", _TrackingSpool)

    with pytest.raises(MediaArchivalError, match="archive limit"):
        archive_item_audio(item=item)

    assert len(_TrackingSpool.instances) == 1
    assert _TrackingSpool.instances[0].closed is True
