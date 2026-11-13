"""Version-one capture identity; changing normalization requires a data migration."""

from hashlib import sha256
from urllib.parse import urlsplit


def normalize_capture_url(url: str) -> str:
    """Compare URLs without changing paths, credentials, or any query bytes."""
    without_fragment = url.split("#", 1)[0]
    try:
        parts = urlsplit(without_fragment)
        userinfo, separator, host_port = parts.netloc.rpartition("@")
        if not separator:
            host_port = parts.netloc
        host_port = host_port.lower()
        if parts.port == {"http": 80, "https": 443}.get(parts.scheme):
            if parts.port is not None:
                host_port = host_port.rsplit(":", 1)[0]
        authority = (userinfo + separator if separator else "") + host_port
        # Keep even an empty query delimiter; urlunsplit would discard it.
        prefix_length = len(parts.scheme) + 3 + len(parts.netloc)
        return parts.scheme.lower() + "://" + authority + without_fragment[prefix_length:]
    except ValueError:
        # Legacy/operator-written malformed URLs must not prevent other captures.
        return without_fragment


def capture_url_digest(url: str) -> str:
    return sha256(normalize_capture_url(url).encode("utf-8", errors="surrogatepass")).hexdigest()
