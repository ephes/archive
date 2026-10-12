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
