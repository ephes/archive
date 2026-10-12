import os
import subprocess
import sys
from pathlib import Path

import pytest


def load_settings(module: str, secret: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = f"config.settings.{module}"
    env.pop("DJANGO_SECRET_KEY", None)
    if secret is not None:
        env["DJANGO_SECRET_KEY"] = secret
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; "
            "print(repr(settings.SECRET_KEY), settings.DEBUG, "
            "settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE)",
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("secret", [None, "", " \t\n", "dev-only-secret-key"])
def test_production_requires_explicit_secret(secret):
    result = load_settings("production", secret)

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "DJANGO_SECRET_KEY" in result.stderr


def test_production_preserves_secret_and_secures_cookies():
    result = load_settings("production", "  configured-test-secret  ")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "'  configured-test-secret  ' False True True"


@pytest.mark.parametrize("module", ["local", "test"])
def test_development_and_test_settings_work_without_secret(module):
    result = load_settings(module, None)

    assert result.returncode == 0, result.stderr
    expected = "dev-only-secret-key" if module == "local" else "test-secret-key"
    assert result.stdout.strip() == f"'{expected}' True False False"
