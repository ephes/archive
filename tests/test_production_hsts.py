import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("max_age,expected", [(None, 31536000), ("3600", 3600), ("0", 0)])
def test_production_hsts_settings(max_age, expected):
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
    env["DJANGO_SECRET_KEY"] = "configured-test-key"
    env.pop("DJANGO_SECURE_HSTS_SECONDS", None)
    if max_age is not None:
        env["DJANGO_SECURE_HSTS_SECONDS"] = max_age
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; "
            "print(settings.SECURE_HSTS_SECONDS, settings.SECURE_HSTS_INCLUDE_SUBDOMAINS, "
            "settings.SECURE_HSTS_PRELOAD)",
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{expected} False False"


def test_https_response_has_hsts(settings, client):
    settings.SECURE_HSTS_SECONDS = 31536000
    response = client.get("/robots.txt", HTTP_X_FORWARDED_PROTO="https")
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "Strict-Transport-Security" not in client.get("/robots.txt").headers


@pytest.mark.parametrize("value", ["", "-1", "invalid"])
def test_production_rejects_invalid_hsts_max_age(value):
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
    env["DJANGO_SECRET_KEY"] = "configured-test-key"
    env["DJANGO_SECURE_HSTS_SECONDS"] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; print(settings.SECURE_HSTS_SECONDS)",
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "DJANGO_SECURE_HSTS_SECONDS" in result.stderr


@pytest.mark.parametrize("module", ["local", "test"])
def test_nonproduction_hsts_is_disabled(module):
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = f"config.settings.{module}"
    env["DJANGO_SECURE_HSTS_SECONDS"] = "31536000"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django.conf import settings; print(settings.SECURE_HSTS_SECONDS)",
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"
