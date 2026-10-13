import pytest
from django.contrib.auth.password_validation import validate_password
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.test import RequestFactory

from archive.login_throttle import client_address


@pytest.fixture(autouse=True)
def throttle_cache(settings):
    settings.CACHES = {
        **settings.CACHES,
        "login_throttle": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test-login-throttle",
            "OPTIONS": {"MAX_ENTRIES": 10000},
        },
    }
    settings.LOGIN_THROTTLE_PROXY_HOPS = 0
    settings.LOGIN_FAILURE_WINDOW_S = 900
    settings.LOGIN_FAILURE_LIMIT = 2
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = 50
    caches["login_throttle"].clear()
    yield
    caches["login_throttle"].clear()


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/accounts/login/", "/admin/login/"])
def test_failures_block_correct_password_on_both_paths(client, editor_user, url):
    data = {"username": editor_user.username, "password": "wrong"}
    for _ in range(2):
        assert client.post(url, data).status_code == 200
    data["password"] = "secret-pass"
    assert client.post(url, data).status_code == 200
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_success_resets_pair(client, editor_user):
    data = {"username": editor_user.username, "password": "wrong"}
    client.post("/accounts/login/", data)
    assert client.post("/accounts/login/", {**data, "password": "secret-pass"}).status_code == 302
    client.logout()
    client.post("/accounts/login/", data)
    assert client.post("/accounts/login/", {**data, "password": "secret-pass"}).status_code == 302


@pytest.mark.django_db
def test_proxy_clients_have_separate_budgets(client, editor_user, settings):
    settings.LOGIN_THROTTLE_PROXY_HOPS = 1
    data = {"username": editor_user.username, "password": "wrong"}
    for _ in range(2):
        client.post("/accounts/login/", data, HTTP_X_FORWARDED_FOR="192.0.2.99, 192.0.2.1")
    assert (
        client.post(
            "/accounts/login/",
            {**data, "password": "secret-pass"},
            HTTP_X_FORWARDED_FOR="192.0.2.99, 192.0.2.1",
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/accounts/login/",
            {**data, "password": "secret-pass"},
            HTTP_X_FORWARDED_FOR="192.0.2.99, 192.0.2.2",
        ).status_code
        == 302
    )


@pytest.mark.parametrize(
    "hops,header,expected",
    [
        (0, "192.0.2.1", "127.0.0.1"),
        (1, "192.0.2.99, 192.0.2.1", "192.0.2.1"),
        (2, "192.0.2.99, 192.0.2.1, 192.0.2.2", "192.0.2.1"),
        (2, "192.0.2.1", "127.0.0.1"),
        (1, "invalid", "127.0.0.1"),
    ],
)
def test_proxy_hops(settings, hops, header, expected):
    settings.LOGIN_THROTTLE_PROXY_HOPS = hops
    request = RequestFactory().post("/accounts/login/", HTTP_X_FORWARDED_FOR=header)
    assert client_address(request) == expected


@pytest.mark.parametrize("password", ["short", "123456789012", "password"])
def test_standard_password_validators(password):
    with pytest.raises(ValidationError):
        validate_password(password)


@pytest.mark.django_db
def test_address_budget_blocks_other_username(client, editor_user, settings):
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = 2
    for name in ("unknown-one", "unknown-two"):
        client.post("/accounts/login/", {"username": name, "password": "wrong"})
    assert (
        client.post(
            "/admin/login/",
            {
                "username": editor_user.username,
                "password": "secret-pass",
            },
        ).status_code
        == 200
    )
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_expired_window_allows_login(client, editor_user):
    import time
    from unittest.mock import patch

    for _ in range(2):
        client.post("/accounts/login/", {"username": editor_user.username, "password": "wrong"})
    with patch("django.core.cache.backends.locmem.time.time", return_value=time.time() + 901):
        assert (
            client.post(
                "/accounts/login/",
                {
                    "username": editor_user.username,
                    "password": "secret-pass",
                },
            ).status_code
            == 302
        )


@pytest.mark.django_db
@pytest.mark.parametrize("pair_limit,address_limit", [(0, 0), (-1, -1), (2, 0), (0, 2)])
def test_disabled_buckets(client, editor_user, settings, pair_limit, address_limit):
    settings.LOGIN_FAILURE_LIMIT = pair_limit
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = address_limit
    data = {"username": editor_user.username, "password": "wrong"}
    for _ in range(2):
        client.post("/accounts/login/", data)
    expected = 302 if pair_limit <= 0 and address_limit <= 0 else 200
    assert (
        client.post("/accounts/login/", {**data, "password": "secret-pass"}).status_code == expected
    )


def test_locked_address_does_not_create_pair_keys(settings):
    from archive.login_throttle import _keys, is_locked, record_failure

    settings.LOGIN_FAILURE_LIMIT = 0
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = 2
    request = RequestFactory().post("/accounts/login/")
    for _ in range(2):
        record_failure(request, "owner")
    for index in range(500):
        record_failure(request, f"unknown-{index}")
    assert is_locked(request, "never-seen")
    assert len(caches["login_throttle"]._cache) == 1
    assert caches["login_throttle"].get(_keys(request, "never-seen")[1]) == 2


@pytest.mark.django_db
def test_disabled_address_bucket_allows_success_below_pair_limit(client, editor_user, settings):
    settings.LOGIN_ADDRESS_FAILURE_LIMIT = 0
    data = {"username": editor_user.username, "password": "wrong"}
    client.post("/accounts/login/", data)
    assert client.post("/accounts/login/", {**data, "password": "secret-pass"}).status_code == 302
