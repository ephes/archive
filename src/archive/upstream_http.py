"""Origin confinement for credential-bearing upstream requests."""

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


def open_authenticated_url(request: Request, *, timeout: float):
    """Keep bearer credentials on their initial origin across every redirect."""
    _origin(request.full_url)
    return build_opener(_AuthenticatedRedirectHandler()).open(request, timeout=timeout)
