"""The production server command must not let one slow response stall or truncate others.

Production runs ``gunicorn config.wsgi:application --bind ... --workers 1`` with
``src/django`` as its working directory, so gunicorn reads ``src/django/gunicorn.conf.py``.
With gunicorn's default sync worker a response that streams longer than ``--timeout``
is killed mid-body (WORKER TIMEOUT) and blocks every other request until then.
"""

import http.client
import io
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DJANGO_DIR = ROOT / "src" / "django"
TESTS_DIR = Path(__file__).resolve().parent
WORKER_TIMEOUT_S = 2


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port: int, path: str, timeout: float) -> tuple[int, bytes, float]:
    started = time.monotonic()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        try:
            data = response.read()
        except http.client.IncompleteRead as exc:
            data = exc.partial
        return response.status, data, time.monotonic() - started
    finally:
        connection.close()


@contextmanager
def _gunicorn(*extra_args: str, env: dict[str, str] | None = None):
    port = _free_port()
    log = tempfile.TemporaryFile()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "--pythonpath",
            str(TESTS_DIR),
            "--bind",
            f"127.0.0.1:{port}",
            "--workers",
            "1",
            "--timeout",
            str(WORKER_TIMEOUT_S),
            *extra_args,
            "gunicorn_probe_app:app",
        ],
        cwd=DJANGO_DIR,
        env={**os.environ, **(env or {})},
        stdout=log,
        stderr=log,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if _get(port, "/fast", timeout=1)[0] == 200:
                    break
            except OSError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                log.seek(0)
                output = log.read().decode(errors="replace")[-4000:]
                raise RuntimeError(f"gunicorn did not start:\n{output}")
            time.sleep(0.1)
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()


@pytest.fixture
def server():
    with _gunicorn() as port:
        yield port


def test_slow_response_neither_blocks_other_requests_nor_is_cut_at_worker_timeout(server):
    port = server
    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(_get, port, "/slow", 30)
        time.sleep(0.5)
        status, data, elapsed = _get(port, "/fast", timeout=30)
        assert (status, data) == (200, b"ok")
        assert elapsed < 2.0, f"a concurrent request waited {elapsed:.1f}s for the slow one"
        slow_status, slow_body, slow_elapsed = slow.result(timeout=30)
    assert slow_status == 200
    # Streams for about 5 s, longer than the 2 s worker timeout, and arrives complete.
    assert slow_elapsed > WORKER_TIMEOUT_S
    assert len(slow_body) == 10 * 1024


def test_connections_close_after_each_response_like_the_sync_worker(server):
    # The proxy may keep idle backend connections longer than gunicorn's keep-alive and
    # reuse one gunicorn already closed (a 502, not retried for POST). Never keep them.
    connection = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    try:
        connection.request("GET", "/fast")
        response = connection.getresponse()
        assert response.read() == b"ok"
        assert response.getheader("Connection", "").lower() == "close"
    finally:
        connection.close()


def test_client_that_stops_reading_releases_its_thread():
    # One thread only: without a send timeout the stalled client would hold it forever.
    with _gunicorn("--threads", "1", env={"PROBE_SEND_TIMEOUT_S": "1"}) as port:
        stalled = socket.create_connection(("127.0.0.1", port))
        try:
            stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            stalled.sendall(b"GET /big HTTP/1.1\r\nHost: probe\r\n\r\n")
            time.sleep(0.5)
            status, data, elapsed = _get(port, "/fast", timeout=15)
            assert (status, data) == (200, b"ok")
            assert elapsed < 10
        finally:
            stalled.close()


@pytest.mark.django_db
def test_production_entry_point_bounds_client_writes():
    from config import wsgi
    from config.client_timeouts import CLIENT_SEND_STALL_TIMEOUT_S

    timeouts = []

    class FakeSocket:
        def settimeout(self, value):
            timeouts.append(value)

    def start_response(status, headers, exc_info=None):
        return lambda data: None

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/robots.txt",
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(),
        "gunicorn.socket": FakeSocket(),
    }
    response = wsgi.application(environ, start_response)
    try:
        assert b"".join(response) == b"User-agent: *\nDisallow: /\n"
    finally:
        response.close()
    assert timeouts == [CLIENT_SEND_STALL_TIMEOUT_S]
    assert CLIENT_SEND_STALL_TIMEOUT_S == 30
