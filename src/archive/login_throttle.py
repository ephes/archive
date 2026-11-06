"""Reserve per-process login budgets before password verification.

Each fixed window counts failed and in-flight attempts per username/address and
per address. Successful logins clear pair failures while preserving other pending
attempts. Request-less authentication and bearer tokens are not limited. Counters
use the local-memory cache, so multiple server processes have independent budgets.
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass, field
from threading import RLock
from time import time
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.cache import caches
from django.core.exceptions import PermissionDenied
from django.dispatch import receiver

CACHE_ALIAS = "login_throttle"
MAX_USERNAME = 150  # longer names are cut for the key; they cannot belong to an account


def client_address(request) -> str:
    """The address failures are counted for.

    ``LOGIN_THROTTLE_PROXY_HOPS`` says how many reverse proxies sit in front of the app.
    With ``n`` hops the
    n-th entry from the right of ``X-Forwarded-For`` is the address the outermost trusted
    proxy saw; anything
    left of it was sent by the client and is ignored. With 0 hops, or when the header is
    missing, too short or
    not an address, the socket address is used."""
    remote = request.META.get("REMOTE_ADDR", "") or "unknown"
    hops = settings.LOGIN_THROTTLE_PROXY_HOPS
    if hops <= 0:
        return remote
    parts = [
        part.strip()
        for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
        if part.strip()
    ]
    if len(parts) < hops:
        return remote
    candidate = parts[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return remote


def _keys(request, username: str) -> tuple[str, str]:
    address = client_address(request)
    name = (username or "")[:MAX_USERNAME].casefold()
    pair = hashlib.sha256(f"{name}\x00{address}".encode()).hexdigest()
    return f"pair:{pair}", f"addr:{hashlib.sha256(address.encode()).hexdigest()}"


@dataclass
class _Window:
    expires_at: float
    failures: int = 0
    pending: set[str] = field(default_factory=set)

    @property
    def count(self) -> int:
        return self.failures + len(self.pending)


_lock = RLock()


def _window(key: str) -> _Window:
    value = caches[CACHE_ALIAS].get(key)
    if isinstance(value, _Window) and value.expires_at > time():
        return value
    return _Window(expires_at=time() + settings.LOGIN_FAILURE_WINDOW_S)


def _save(key: str, window: _Window) -> None:
    remaining = window.expires_at - time()
    if remaining > 0:
        caches[CACHE_ALIAS].set(key, window, timeout=remaining)


def _count(key: str) -> int:
    return _window(key).count


def _enabled_keys(request, username: str) -> list[tuple[str, int]]:
    pair, address = _keys(request, username)
    return [
        (key, limit)
        for key, limit in (
            (pair, settings.LOGIN_FAILURE_LIMIT),
            (address, settings.LOGIN_ADDRESS_FAILURE_LIMIT),
        )
        if limit > 0
    ]


def is_locked(request, username: str) -> bool:
    if request is None:
        return False
    with _lock:
        return any(_count(key) >= limit for key, limit in _enabled_keys(request, username))


def record_failure(request, username: str) -> None:
    if request is None or getattr(request, "_archive_login_accounted", False):
        return
    with _lock:
        if is_locked(request, username):
            return
        for key, _ in _enabled_keys(request, username):
            window = _window(key)
            window.failures += 1
            _save(key, window)


def reset(request, username: str) -> None:
    if request is None or settings.LOGIN_FAILURE_LIMIT <= 0:
        return
    with _lock:
        pair = _keys(request, username)[0]
        window = _window(pair)
        window.failures = 0
        _save(pair, window)


class LoginThrottleBackend(ModelBackend):
    """Reserve per-process budgets before running Django's password backend.

    Request-less authentication delegates to the usual ModelBackend. Keep that
    backend configured for existing sessions and request-less callers. Failed
    request authentication stops the chain to avoid running a second password hash.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if request is None or not isinstance(username, str):
            return None
        request._archive_login_accounted = True
        token = uuid4().hex
        with _lock:
            if is_locked(request, username):
                raise PermissionDenied("Too many failed login attempts.")
            keys = _enabled_keys(request, username)
            for key, _ in keys:
                window = _window(key)
                window.pending.add(token)
                _save(key, window)
        user = None
        try:
            user = super().authenticate(request, username=username, password=password, **kwargs)
        finally:
            with _lock:
                for key, _ in keys:
                    window = _window(key)
                    # The window may have expired while this attempt was running.
                    # An old result cannot alter a newer window's reservations.
                    if token not in window.pending:
                        continue
                    window.pending.remove(token)
                    if user is None:
                        window.failures += 1
                    elif key == _keys(request, username)[0]:
                        window.failures = 0
                    _save(key, window)
        if user is None:
            raise PermissionDenied("Invalid login.")
        return user


@receiver(user_login_failed, dispatch_uid="archive.login_throttle.failed")
def _on_login_failed(sender, credentials, request=None, **kwargs):
    username = credentials.get("username") or credentials.get(get_user_model().USERNAME_FIELD) or ""
    record_failure(request, username if isinstance(username, str) else "")


@receiver(user_logged_in, dispatch_uid="archive.login_throttle.logged_in")
def _on_logged_in(sender, request, user, **kwargs):
    reset(request, user.get_username())
