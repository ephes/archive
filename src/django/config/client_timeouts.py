"""Bound how long one blocked socket operation with a client may hold a server thread.

Threaded gunicorn workers keep heartbeating while a request thread waits, so a
client that stops reading a large response would otherwise hold its thread
forever. Each socket write (gunicorn's ``sendall`` of one response chunk) and each
request-body read must now finish within the timeout, or gunicorn closes the
connection. Audio is written in small blocks, so a slow but steady reader is not
cut off; a large single-chunk response must reach the client within the timeout,
as under the former sync worker. Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

CLIENT_SEND_STALL_TIMEOUT_S = 30.0

WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


def with_client_send_timeout(app: WSGIApp, timeout: float = CLIENT_SEND_STALL_TIMEOUT_S) -> WSGIApp:
    def wrapped(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        settimeout = getattr(environ.get("gunicorn.socket"), "settimeout", None)
        if settimeout is not None:
            settimeout(timeout)
        return app(environ, start_response)

    return wrapped
