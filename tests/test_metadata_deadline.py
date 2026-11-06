from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from archive import metadata


@pytest.mark.parametrize("stage", ["dns", "body"])
def test_metadata_whole_transfer_deadline_kills_stalled_fetch(monkeypatch, tmp_path, stage):
    marker = tmp_path / "entered"
    helper = Path(metadata.__file__).with_name("direct_media_download.py")
    # Run the actual downloader and guarded DNS connector in a disposable child,
    # substituting only a stalled resolver or an endless synthetic HTML body.
    script = """
import runpy, sys, time
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
namespace = runpy.run_path(sys.argv[1])
load = namespace['_load_safe_fetch']
guard = load()
marker = Path(sys.argv[2])
if sys.argv[3] == 'dns':
    def resolve(*args):
        marker.write_text('dns')
        time.sleep(5)
        return []
    guard.resolve_host = resolve
else:
    class Response:
        headers = Message()
        headers['Content-Type'] = 'text/html; charset=utf-8'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return 'https://example.test/page'
        def read(self, n):
            if not marker.exists(): marker.write_text('body')
            time.sleep(0.02)
            return b'x'
    guard.open_public_url = lambda *args, **kwargs: Response()
namespace['main'].__globals__['_load_safe_fetch'] = lambda: guard
raise SystemExit(namespace['main']())
"""

    def run_download(command, **kwargs):
        assert command[-1] == str(helper)
        return subprocess.run(
            [sys.executable, "-I", "-c", script, str(helper), str(marker), stage], **kwargs
        )

    # The pre-fix implementation blocks here and never enforces a total deadline.
    def old_open(*args, **kwargs):
        marker.write_text(stage)
        time.sleep(1.2)
        raise OSError("synthetic late failure")

    monkeypatch.setattr(metadata, "run_download", run_download, raising=False)
    monkeypatch.setattr(metadata, "open_public_url", old_open, raising=False)
    started = time.monotonic()
    with pytest.raises(metadata.MetadataExtractionError, match="total timeout"):
        metadata.extract_metadata_from_url("https://example.test/page", timeout=1)
    assert time.monotonic() - started < 3
    assert marker.read_text() == stage


def test_metadata_download_preserves_charset_and_cap(monkeypatch, tmp_path):
    import json
    import runpy
    from email.message import Message
    from io import StringIO

    namespace = runpy.run_path(str(Path(metadata.__file__).with_name("direct_media_download.py")))
    body = '<head><title>Grüße</title></head><audio src="/episode.mp3">'.encode("iso-8859-1")
    reads = []

    class Response:
        headers = Message()
        headers["Content-Type"] = "text/html; charset=iso-8859-1"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return "https://example.test/page"

        def read(self, n):
            reads.append(n)
            return body[:n] if len(reads) == 1 else b""

    from types import SimpleNamespace

    namespace["main"].__globals__["_load_safe_fetch"] = lambda: SimpleNamespace(
        open_public_url=lambda *args, **kwargs: Response()
    )
    path = tmp_path / "source"
    options = dict(
        url="https://example.test/page",
        headers={},
        timeout=1,
        path=str(path),
        max_bytes=len(body),
        metadata=True,
    )
    output = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(options)))
    monkeypatch.setattr(sys, "stdout", output)
    assert namespace["main"]() == 0
    assert reads == [len(body)]
    assert path.read_bytes() == body
    assert json.loads(output.getvalue())["charset"] == "iso-8859-1"


@pytest.mark.parametrize("outcome", ["success", "http", "url"])
def test_metadata_subprocess_preserves_content_and_error_contract(monkeypatch, outcome):
    helper = Path(metadata.__file__).with_name("direct_media_download.py")
    script = """
import runpy, sys
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
namespace = runpy.run_path(sys.argv[1])
class Response(BytesIO):
    headers = Message()
    headers['Content-Type'] = 'text/html; charset=iso-8859-1'
    def geturl(self): return 'https://example.test/page'
def open_url(request, timeout):
    assert request.full_url == 'https://example.test/page'
    assert timeout > 0
    if sys.argv[2] == 'http':
        raise HTTPError(request.full_url, 404, 'Not Found', Message(), None)
    if sys.argv[2] == 'url':
        raise URLError('synthetic DNS failure')
    return Response(('<head><title>Grüße</title></head>'
                     '<body><audio src="/episode.mp3"></audio></body>').encode('iso-8859-1'))
namespace['main'].__globals__['_load_safe_fetch'] = lambda: SimpleNamespace(
    open_public_url=open_url)
raise SystemExit(namespace['main']())
"""
    seen = []

    def run_download(command, **kwargs):
        assert command[-1] == str(helper)
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(helper), outcome], **kwargs
        )
        seen.append(result.returncode)
        return result

    monkeypatch.setattr(metadata, "run_download", run_download)
    if outcome == "success":
        result = metadata.extract_metadata_from_url("https://example.test/page", timeout=3)
        assert result.title == "Grüße"
        assert result.audio_url == "https://example.test/episode.mp3"
        assert seen == [0]
    else:
        message = (
            "HTTP 404 while fetching metadata"
            if outcome == "http"
            else "Could not fetch metadata: synthetic DNS failure"
        )
        with pytest.raises(metadata.MetadataExtractionError) as exc:
            metadata.extract_metadata_from_url("https://example.test/page", timeout=3)
        assert str(exc.value) == message
        assert seen == [1]
