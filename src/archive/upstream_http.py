"""Origin confinement for credential-bearing upstream requests."""

import json
import subprocess
import sys
from collections.abc import Iterable
from contextlib import contextmanager
from email.message import Message
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError as exc:
        raise OSError("Upstream URL must be HTTP(S) without userinfo") from exc


def api_url(base_url: str, path: str) -> str:
    """Resolve an API path, allowing only destinations on the configured origin."""
    target = urljoin(base_url.rstrip("/") + "/", path.strip())
    if _origin(target) != _origin(base_url):
        raise OSError("Upstream destination must match the configured API origin")
    return target


class _AuthenticatedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            if _origin(newurl) != _origin(req.full_url):
                raise OSError("Authenticated upstream redirect changed origin")
        except OSError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_authenticated_url(request: Request, *, timeout: float):
    """Keep bearer credentials on their initial origin across every redirect."""
    _origin(request.full_url)
    return build_opener(_AuthenticatedRedirectHandler()).open(request, timeout=timeout)


@contextmanager
def open_authenticated_url(
    request: Request, *, timeout: float, max_bytes: int = 8 * 1024 * 1024, audio: bool = False
):
    """Bound DNS, headers, redirects, bodies and child teardown by one deadline.

    Only the disposable stdlib transport process performs network I/O. Killing and
    reaping it also closes its sockets, including a stalled resolver or error read.
    Bodies stay on disk; credentials travel over stdin, never command arguments.
    """
    _origin(request.full_url)
    deadline = monotonic() + timeout
    with TemporaryDirectory(prefix="archive-upstream-") as directory:
        path = Path(directory) / "body"
        request_path = Path(directory) / "request"
        if request.data is not None:
            with request_path.open("wb") as output:
                if isinstance(request.data, bytes):
                    output.write(request.data)
                elif isinstance(request.data, Iterable):
                    for chunk in request.data:
                        output.write(chunk)
                        if monotonic() >= deadline:
                            raise OSError("Upstream exchange exceeded the total timeout")
                else:
                    raise OSError("Unsupported upstream request body")
        options = {
            "url": request.full_url,
            "headers": dict(request.header_items()),
            "method": request.get_method(),
            "data_path": str(request_path) if request.data is not None else None,
            "path": str(path),
            "timeout": timeout,
            "max_bytes": max_bytes,
            "audio": audio,
        }
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise OSError("Upstream exchange exceeded the total timeout")
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).with_name("upstream_download.py"))],
                input=json.dumps(options),
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("Upstream exchange exceeded the total timeout") from exc
        if monotonic() >= deadline:
            raise OSError("Upstream exchange exceeded the total timeout")
        try:
            info = json.loads(result.stdout)
        except ValueError as exc:
            raise OSError("Upstream transport failed") from exc
        if result.returncode:
            raise OSError(info.get("error", "Upstream transport failed"))
        headers = Message()
        for key, value in info["headers"]:
            headers[key] = value
        if info["is_error"]:
            # The diagnostic body is already bounded and the child has closed.
            raise HTTPError(
                request.full_url,
                info["status"],
                "Upstream error",
                headers,
                BytesIO(path.read_bytes()),
            )
        with path.open("rb") as source:
            yield SimpleNamespace(headers=headers, read=source.read)
