"""Tiny WSGI app for the gunicorn worker-model test: one slow stream, one fast route."""

import os
import time

SLOW_CHUNKS = 10
SLOW_CHUNK_DELAY_S = 0.5
CHUNK = b"x" * 1024


BIG_CHUNK = b"y" * (64 * 1024)
BIG_CHUNKS = 640  # 40 MiB, far more than socket buffers hold


def _app(environ, start_response):
    if environ.get("PATH_INFO") == "/big":
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(BIG_CHUNKS * len(BIG_CHUNK))),
            ],
        )
        return (BIG_CHUNK for _ in range(BIG_CHUNKS))
    if environ.get("PATH_INFO") == "/slow":
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(SLOW_CHUNKS * len(CHUNK))),
            ],
        )

        def body():
            for _ in range(SLOW_CHUNKS):
                time.sleep(SLOW_CHUNK_DELAY_S)
                yield CHUNK

        return body()
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return [b"ok"]


if os.environ.get("PROBE_SEND_TIMEOUT_S"):
    from config.client_timeouts import with_client_send_timeout

    app = with_client_send_timeout(_app, float(os.environ["PROBE_SEND_TIMEOUT_S"]))
else:
    app = _app
