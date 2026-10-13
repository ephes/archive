import os

from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *  # noqa: F403

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY.strip() or SECRET_KEY == "dev-only-secret-key":
    raise ImproperlyConfigured(
        "Production requires DJANGO_SECRET_KEY to be set to a non-blank, non-development key."
    )

DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
try:
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
except ValueError as exc:
    raise ImproperlyConfigured(
        "DJANGO_SECURE_HSTS_SECONDS must be a non-negative integer."
    ) from exc
if SECURE_HSTS_SECONDS < 0:
    raise ImproperlyConfigured("DJANGO_SECURE_HSTS_SECONDS must be a non-negative integer.")
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# Production runs behind exactly one trusted reverse proxy (Traefik), which appends the
# client address to X-Forwarded-For. Without this, all visitors would share one bucket.
LOGIN_THROTTLE_PROXY_HOPS = int(os.getenv("ARCHIVE_LOGIN_THROTTLE_PROXY_HOPS", "1"))
