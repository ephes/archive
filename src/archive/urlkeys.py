"""URL fingerprints used to recognise repeated captures of the same page.

Kept free of model imports so ``archive.models`` can use it. Migration 0014 carries a frozen
copy of :func:`normalize_capture_url`; change both together (and backfill) if the rules change.
"""

from __future__ import annotations

import hashlib
from urllib.parse import unquote, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


def _is_tracking_param(raw_name: str) -> bool:
    name = unquote(raw_name.replace("+", " ")).lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def normalize_capture_url(url: str) -> str:
    """Return the URL form used to detect repeated captures.

    Lower-cases scheme and host, drops default ports, the fragment and common tracking
    parameters (``utm_*``, ``fbclid``, ``gclid``, ``mc_cid``, ``mc_eid``). Path and the
    remaining query parameters are kept byte for byte, in their original order.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host
    if port is not None and _DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{host}:{port}"
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    path = parts.path or "/"
    query = "&".join(
        piece
        for piece in parts.query.split("&")
        if not (piece and _is_tracking_param(piece.split("=", 1)[0]))
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def capture_key_for_url(url: str) -> str:
    """SHA-256 hex digest of the normalised URL."""
    return hashlib.sha256(normalize_capture_url(url).encode("utf-8")).hexdigest()
