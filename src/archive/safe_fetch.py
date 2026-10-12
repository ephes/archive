"""Guarded opener for URLs that come from shared links and third-party pages.

Only ``http``/``https`` URLs are fetched, proxies from the environment are
ignored, redirects are capped and re-checked, and every TCP connection goes to
an address that was resolved and checked first, so a DNS answer cannot change
between the check and the connect. Hosts listed in ``ARCHIVE_FETCH_PRIVATE_HOSTS``
may resolve to private or loopback addresses.

Standard library only: ``direct_media_download.py`` loads this file in an
isolated interpreter without Django.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

MAX_REDIRECTS = 5
PRIVATE_HOSTS_ENV_VAR = "ARCHIVE_FETCH_PRIVATE_HOSTS"
ALLOWED_SCHEMES = frozenset({"http", "https"})
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")


class BlockedFetchError(OSError):
    """The URL or the address it resolved to must not be fetched."""


def private_hosts() -> set[str]:
    raw = os.environ.get(PRIVATE_HOSTS_ENV_VAR, "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def require_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
        raise BlockedFetchError("Only http and https URLs can be fetched")


def check_resolved_address(host: str, address: str) -> None:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise BlockedFetchError("Host resolved to an invalid address") from exc
    if isinstance(ip, ipaddress.IPv6Address):
        # Judge IPv4 addresses carried inside IPv6 ones by the IPv4 rules.
        if ip in _NAT64_NETWORK:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        else:
            ip = ip.ipv4_mapped or ip.sixtofour or ip
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise BlockedFetchError("Host resolved to a blocked network address")
    # The deprecated site-local range fec0::/10 is not flagged by is_global.
    public = ip.is_global and not getattr(ip, "is_site_local", False)
    if not public and host.lower() not in private_hosts():
        raise BlockedFetchError("Host resolved to a non-public network address")


def resolve_host(host: str, port: int) -> list[tuple[Any, ...]]:
    """Resolve host for TCP; split out so tests can stub DNS."""
    return list(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))


def _create_checked_connection(
    address: tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]
    source_address: tuple[str, int] | None = None,
    *,
    all_errors: bool = False,
) -> socket.socket:
    """``socket.create_connection`` that checks, then connects to, the resolved address."""
    host, port = address
    resolved = resolve_host(host, port)
    for *_ignored, sockaddr in resolved:
        check_resolved_address(host, str(sockaddr[0]))
    last_error: OSError = OSError(f"Host {host} did not resolve")
    for family, socktype, proto, _canonname, sockaddr in resolved:
        sock = socket.socket(family, socktype, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last_error = exc
    raise last_error


class _CheckedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_checked_connection


class _CheckedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_checked_connection


class _CheckedHTTPHandler(HTTPHandler):
    def http_open(self, req: Request) -> Any:
        return self.do_open(_CheckedHTTPConnection, req)


class _CheckedHTTPSHandler(HTTPSHandler):
    def https_open(self, req: Request) -> Any:
        return self.do_open(_CheckedHTTPSConnection, req)


class _CheckedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's own limit counts distinct destinations; count every hop.
        hops = getattr(req, "archive_redirect_hops", 0) + 1
        try:
            if hops > MAX_REDIRECTS:
                raise BlockedFetchError(f"URL fetch exceeded {MAX_REDIRECTS} redirects")
            require_http_url(newurl)
        except BlockedFetchError:
            fp.close()
            raise
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is not None:
            new_request.archive_redirect_hops = hops  # type: ignore[attr-defined]
        return new_request


def open_public_url(request: Request, *, timeout: float) -> Any:
    """Open a third-party URL; raises ``OSError`` (``BlockedFetchError``) when refused."""
    require_http_url(request.full_url)
    opener = build_opener(
        ProxyHandler({}),
        _CheckedHTTPHandler(),
        _CheckedHTTPSHandler(),
        _CheckedRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)
