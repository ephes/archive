from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import Request

import pytest

from archive import safe_fetch
from archive.metadata import MetadataExtractionError, extract_metadata_from_url
from archive.models import Item, ItemKind
from archive.safe_fetch import BlockedFetchError, check_resolved_address, open_public_url
from archive.summaries import SummaryGenerationError, extract_summary_source_from_url
from archive.transcriptions import TranscriptionGenerationError, generate_item_transcript


class _Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.hits.append(self.path)
        if self.path.startswith("/redirect-to-ftp"):
            self.send_response(302)
            self.send_header("Location", "ftp://example.com/episode.mp3")
            self.end_headers()
            return
        if self.path.startswith("/loop"):
            self.send_response(302)
            self.send_header("Location", f"/loop{len(self.hits)}")
            self.end_headers()
            return
        if self.path.startswith("/ping") and len(self.hits) <= 6:
            # Alternates between two URLs, then would answer 200 on the 7th request.
            self.send_response(302)
            self.send_header("Location", "/ping-a" if self.path == "/ping-b" else "/ping-b")
            self.end_headers()
            return
        body = b"<html><head><title>Internal admin</title></head><body>secret</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def loopback_server():
    _Handler.hits = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def public_name_for_loopback(monkeypatch, loopback_server):
    """A hostname whose DNS answer is controlled by the test."""
    port = int(loopback_server.rsplit(":", 1)[1])
    answers = {"address": "127.0.0.1"}

    def resolve(host, requested_port):
        assert host == "media.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers["address"], requested_port))]

    monkeypatch.setattr(safe_fetch, "resolve_host", resolve)
    return f"http://media.example.com:{port}", answers


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.178.1",
        "172.16.0.9",
        "100.64.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fd12:3456::1",
        "fe80::1%eth0",
        "::ffff:10.0.0.1",
        "fec0::1",
        "2002:c0a8:0101::1",
        "64:ff9b::a00:1",
    ],
)
def test_non_public_addresses_are_refused(address, monkeypatch):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS", raising=False)
    with pytest.raises(BlockedFetchError):
        check_resolved_address("media.example.com", address)


def test_public_addresses_are_accepted(monkeypatch):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS", raising=False)
    check_resolved_address("media.example.com", "93.184.216.34")
    check_resolved_address("media.example.com", "2606:2800:220:1:248:1893:25c8:1946")
    check_resolved_address("media.example.com", "64:ff9b::5db8:d822")


def test_listed_private_host_may_resolve_privately_but_never_link_local(monkeypatch):
    monkeypatch.setenv("ARCHIVE_FETCH_PRIVATE_HOSTS", "nas.home.example, Media.Example.com")
    check_resolved_address("media.example.com", "192.168.178.20")
    with pytest.raises(BlockedFetchError):
        check_resolved_address("other.example.com", "192.168.178.20")
    with pytest.raises(BlockedFetchError):
        check_resolved_address("media.example.com", "169.254.169.254")


@pytest.mark.parametrize(
    "url", ["file:///etc/hostname", "ftp://example.com/a.mp3", "data:text/plain,hi", "http:///x"]
)
def test_only_http_urls_are_opened(url):
    with pytest.raises(BlockedFetchError):
        open_public_url(Request(url), timeout=1)


def test_loopback_literal_is_not_contacted(loopback_server, monkeypatch):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS", raising=False)
    with pytest.raises(URLError, match="non-public network address"):
        open_public_url(Request(loopback_server + "/"), timeout=2)
    assert _Handler.hits == []


def test_name_resolving_to_loopback_is_not_contacted(public_name_for_loopback, monkeypatch):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS", raising=False)
    url, _answers = public_name_for_loopback
    with pytest.raises(URLError, match="non-public network address"):
        open_public_url(Request(url + "/"), timeout=2)
    assert _Handler.hits == []


def test_connection_uses_the_checked_address(public_name_for_loopback, monkeypatch):
    monkeypatch.setenv("ARCHIVE_FETCH_PRIVATE_HOSTS", "media.example.com")
    url, _answers = public_name_for_loopback
    with open_public_url(Request(url + "/page"), timeout=2) as response:
        assert b"Internal admin" in response.read()
    assert _Handler.hits == ["/page"]


def test_redirect_to_other_scheme_is_refused(loopback_server, monkeypatch):
    monkeypatch.setenv("ARCHIVE_FETCH_PRIVATE_HOSTS", "127.0.0.1")
    with pytest.raises(BlockedFetchError):
        open_public_url(Request(loopback_server + "/redirect-to-ftp"), timeout=2)


@pytest.mark.parametrize("path", ["/loop", "/ping-a"])
def test_redirects_are_capped_even_for_revisited_urls(loopback_server, monkeypatch, path):
    monkeypatch.setenv("ARCHIVE_FETCH_PRIVATE_HOSTS", "127.0.0.1")
    with pytest.raises(BlockedFetchError, match="exceeded 5 redirects"):
        open_public_url(Request(loopback_server + path), timeout=2)
    assert len(_Handler.hits) == safe_fetch.MAX_REDIRECTS + 1


def test_redirects_within_the_limit_are_followed(loopback_server, monkeypatch):
    monkeypatch.setenv("ARCHIVE_FETCH_PRIVATE_HOSTS", "127.0.0.1")
    _Handler.hits = ["earlier"] * 2
    with open_public_url(Request(loopback_server + "/ping-a"), timeout=2) as response:
        assert response.status == 200


def test_metadata_fetch_refuses_internal_page(loopback_server, monkeypatch):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS", raising=False)
    with pytest.raises(MetadataExtractionError, match="non-public network address"):
        extract_metadata_from_url(loopback_server + "/", timeout=2)
    assert _Handler.hits == []


def test_summary_source_fetch_refuses_internal_page(loopback_server, monkeypatch):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS", raising=False)
    with pytest.raises(SummaryGenerationError, match="non-public network address"):
        extract_summary_source_from_url(loopback_server + "/", timeout=2)
    assert _Handler.hits == []


@pytest.mark.django_db
def test_transcription_media_fetch_refuses_internal_source(loopback_server, monkeypatch, settings):
    monkeypatch.delenv("ARCHIVE_FETCH_PRIVATE_HOSTS", raising=False)
    settings.ARCHIVE_TRANSCRIPTION_API_KEY = "key"
    item = Item.objects.create(
        original_url="https://example.com/episode",
        audio_url=loopback_server + "/episode.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )
    with pytest.raises(TranscriptionGenerationError, match="non-public network address"):
        generate_item_transcript(item=item)
    assert _Handler.hits == []
