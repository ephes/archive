from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from django.contrib.auth import authenticate
from django.contrib.auth.backends import ModelBackend
from django.core.cache import caches
from django.test import RequestFactory

from archive.login_throttle import LoginThrottleBackend, _keys, is_locked, reset


@pytest.mark.parametrize("pair_limit,address_limit", [(2, 0), (0, 2), (2, 2)])
def test_inflight_budget_precedes_password_verification(
    monkeypatch, settings, pair_limit, address_limit
):
    settings.LOGIN_FAILURE_LIMIT = pair_limit
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = address_limit
    settings.LOGIN_FAILURE_WINDOW_S = 60
    caches["login_throttle"].clear()
    entered = Event()
    release = Event()
    lock = Lock()
    calls = []

    def password_backend(self, request, **kwargs):
        with lock:
            calls.append(request)
            if len(calls) >= 2:
                entered.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(ModelBackend, "authenticate", password_backend)
    requests = [RequestFactory().post("/accounts/login/") for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(authenticate, request=r, username="synthetic", password="bad")
            for r in requests
        ]
        try:
            assert entered.wait(3)
            assert is_locked(requests[0], "synthetic")
        finally:
            release.set()
        assert all(f.result() is None for f in futures)
    assert len(calls) == 2
    caches["login_throttle"].clear()


def test_success_preserves_other_reservations(monkeypatch, settings):
    settings.LOGIN_FAILURE_LIMIT = 3
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = 0
    settings.LOGIN_FAILURE_WINDOW_S = 60
    caches["login_throttle"].clear()
    first_entered = Event()
    release = Event()
    request = RequestFactory().post("/accounts/login/")
    backend = LoginThrottleBackend()
    successful = SimpleNamespace(get_username=lambda: "synthetic")
    calls = []

    def password_backend(self, req, **kwargs):
        calls.append(req)
        if req is request:
            first_entered.set()
            assert release.wait(5)
            return None
        return successful

    monkeypatch.setattr(ModelBackend, "authenticate", password_backend)
    from django.core.exceptions import PermissionDenied

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(backend.authenticate, request, username="synthetic", password="bad")
        try:
            assert first_entered.wait(3)
            other = RequestFactory().post("/accounts/login/")
            assert backend.authenticate(other, username="synthetic", password="ok") is successful
            reset(other, "synthetic")
            from archive.login_throttle import _count

            assert _count(_keys(request, "synthetic")[0]) == 1
        finally:
            release.set()
        with pytest.raises(PermissionDenied):
            pending.result()
    caches["login_throttle"].clear()


def test_old_success_cannot_release_new_window_reservation(monkeypatch, settings):
    import time as clock

    from django.core.exceptions import PermissionDenied

    from archive import login_throttle

    settings.LOGIN_FAILURE_LIMIT = 1
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = 1
    settings.LOGIN_FAILURE_WINDOW_S = 60
    caches["login_throttle"].clear()
    now = [clock.time()]
    monkeypatch.setattr(login_throttle, "time", lambda: now[0])
    monkeypatch.setattr("django.core.cache.backends.locmem.time.time", lambda: now[0])
    old = RequestFactory().post("/old")
    new = RequestFactory().post("/new")
    entered_old, entered_new, release_old, release_new = [Event() for _ in range(4)]
    user = SimpleNamespace(get_username=lambda: "synthetic")

    def password_backend(self, req, **kwargs):
        entered, release = (entered_old, release_old) if req is old else (entered_new, release_new)
        entered.set()
        assert release.wait(5)
        return user if req is old else None

    monkeypatch.setattr(ModelBackend, "authenticate", password_backend)
    backend = LoginThrottleBackend()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(backend.authenticate, old, username="synthetic", password="ok")
        try:
            assert entered_old.wait(3)
            now[0] += 61
            second = pool.submit(backend.authenticate, new, username="synthetic", password="bad")
            assert entered_new.wait(3)
            release_old.set()
            assert first.result() is user
            reset(old, "synthetic")
            assert is_locked(new, "synthetic")
            for key in _keys(new, "synthetic"):
                assert login_throttle._count(key) == 1
        finally:
            release_old.set()
            release_new.set()
        with pytest.raises(PermissionDenied):
            second.result()
    assert is_locked(new, "synthetic")
    caches["login_throttle"].clear()
