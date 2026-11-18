import os
from pathlib import Path

from django.utils.csp import CSP

from archive.capture_config import capture_dedupe_seconds
from archive.transcription_config import (
    transcription_max_gap_seconds,
    transcription_max_job_age_seconds,
)

DJANGO_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[4]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-secret-key")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "archive.apps.ArchiveConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
]

# Content-Security-Policy: everything same-origin. Page CSS/JS live in static files
# (no inline code). media-src also allows https: because an item without a stored
# copy plays its original audio_url from the source host.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "media-src": [CSP.SELF, "https:"],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
}

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.getenv("DJANGO_DB_PATH", str(PROJECT_ROOT / "db.sqlite3")),
        # Acquire the writer lock before capture's read, waiting up to the busy timeout.
        "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 5},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{name}"}
    for name in (
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    )
]
AUTHENTICATION_BACKENDS = [
    "archive.login_throttle.LoginThrottleBackend",
    "django.contrib.auth.backends.ModelBackend",
]
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "login_throttle": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "archive-login-throttle",
        "OPTIONS": {"MAX_ENTRIES": 10000},
    },
}
LOGIN_FAILURE_LIMIT = int(os.getenv("ARCHIVE_LOGIN_FAILURE_LIMIT", "5"))
LOGIN_ADDRESS_FAILURE_LIMIT = int(os.getenv("ARCHIVE_LOGIN_ADDRESS_FAILURE_LIMIT", "50"))
LOGIN_FAILURE_WINDOW_S = int(os.getenv("ARCHIVE_LOGIN_FAILURE_WINDOW_S", "900"))
LOGIN_THROTTLE_PROXY_HOPS = int(os.getenv("ARCHIVE_LOGIN_THROTTLE_PROXY_HOPS", "0"))

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = Path(os.getenv("DJANGO_STATIC_ROOT", str(PROJECT_ROOT / "staticfiles")))
STORAGES: dict[str, dict[str, object]] = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

LOGIN_URL = "archive:login"
LOGIN_REDIRECT_URL = "archive:item-new"
LOGOUT_REDIRECT_URL = "archive:overview"

ARCHIVE_API_TOKEN = os.getenv("ARCHIVE_API_TOKEN", "")
ARCHIVE_CAPTURE_DEDUPE_SECONDS = capture_dedupe_seconds(
    os.getenv("ARCHIVE_CAPTURE_DEDUPE_SECONDS", "86400")
)
ARCHIVE_SUMMARY_API_KEY = os.getenv("ARCHIVE_SUMMARY_API_KEY", "")
ARCHIVE_SUMMARY_API_BASE = os.getenv("ARCHIVE_SUMMARY_API_BASE", "https://api.openai.com/v1")
ARCHIVE_SUMMARY_MODEL = os.getenv("ARCHIVE_SUMMARY_MODEL", "gpt-4o-mini")
ARCHIVE_TRANSCRIPTION_API_KEY = os.getenv(
    "ARCHIVE_TRANSCRIPTION_API_KEY",
    ARCHIVE_SUMMARY_API_KEY,
)
ARCHIVE_TRANSCRIPTION_API_BASE = os.getenv(
    "ARCHIVE_TRANSCRIPTION_API_BASE",
    ARCHIVE_SUMMARY_API_BASE,
)
ARCHIVE_TRANSCRIPTION_MODEL = os.getenv(
    "ARCHIVE_TRANSCRIPTION_MODEL",
    "gpt-4o-mini-transcribe",
)
# Seconds between status requests for a running Voxhelm batch transcription job.
ARCHIVE_TRANSCRIPTION_POLL_SECONDS = float(
    os.getenv("ARCHIVE_TRANSCRIPTION_POLL_SECONDS", "60")
)
# A batch job still unfinished this long after submission fails the transcript step.
ARCHIVE_TRANSCRIPTION_MAX_JOB_AGE_SECONDS = transcription_max_job_age_seconds(
    os.getenv("ARCHIVE_TRANSCRIPTION_MAX_JOB_AGE_SECONDS", "7200")
)
ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS = transcription_max_gap_seconds(
    os.getenv("ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS", "600")
)
ARCHIVE_ARTICLE_AUDIO_API_KEY = os.getenv(
    "ARCHIVE_ARTICLE_AUDIO_API_KEY",
    ARCHIVE_TRANSCRIPTION_API_KEY,
)
ARCHIVE_ARTICLE_AUDIO_API_BASE = os.getenv(
    "ARCHIVE_ARTICLE_AUDIO_API_BASE",
    ARCHIVE_TRANSCRIPTION_API_BASE,
)
ARCHIVE_ARTICLE_AUDIO_MODEL = os.getenv("ARCHIVE_ARTICLE_AUDIO_MODEL", "tts-1")
ARCHIVE_ARTICLE_AUDIO_VOICE = os.getenv("ARCHIVE_ARTICLE_AUDIO_VOICE", "")
ARCHIVE_ARTICLE_AUDIO_LANGUAGE = os.getenv("ARCHIVE_ARTICLE_AUDIO_LANGUAGE", "")
ARCHIVE_ARTICLE_AUDIO_POLL_SECONDS = int(
    os.getenv("ARCHIVE_ARTICLE_AUDIO_POLL_SECONDS", "30")
)
ARCHIVE_ARTICLE_AUDIO_SCRIPT_MAX_CHARS = int(
    os.getenv("ARCHIVE_ARTICLE_AUDIO_SCRIPT_MAX_CHARS", "5000")
)
ARCHIVE_ARTICLE_AUDIO_MAX_BYTES = int(
    os.getenv("ARCHIVE_ARTICLE_AUDIO_MAX_BYTES", str(50 * 1024 * 1024))
)
ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES = int(
    os.getenv("ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES", str(250 * 1024 * 1024))
)
ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN = os.getenv(
    "ARCHIVE_MEDIA_EXTRACTION_FFMPEG_BIN",
    "ffmpeg",
)
_archive_media_ytdlp_js_runtimes = os.getenv("ARCHIVE_MEDIA_YTDLP_JS_RUNTIMES")
if _archive_media_ytdlp_js_runtimes is None:
    ARCHIVE_MEDIA_YTDLP_JS_RUNTIMES = ["node"]
else:
    ARCHIVE_MEDIA_YTDLP_JS_RUNTIMES = [
        item.strip() for item in _archive_media_ytdlp_js_runtimes.split(",") if item.strip()
    ]
ARCHIVE_MEDIA_STORAGE_BACKEND = os.getenv(
    "ARCHIVE_MEDIA_STORAGE_BACKEND",
    "django.core.files.storage.FileSystemStorage",
)
ARCHIVE_MEDIA_STORAGE_LOCATION = os.getenv(
    "ARCHIVE_MEDIA_STORAGE_LOCATION",
    str(PROJECT_ROOT / "archive-media"),
)
ARCHIVE_MEDIA_STORAGE_BASE_URL = os.getenv("ARCHIVE_MEDIA_STORAGE_BASE_URL", "")
ARCHIVE_MEDIA_STORAGE_BUCKET_NAME = os.getenv("ARCHIVE_MEDIA_STORAGE_BUCKET_NAME", "")
ARCHIVE_MEDIA_STORAGE_ENDPOINT_URL = os.getenv("ARCHIVE_MEDIA_STORAGE_ENDPOINT_URL", "")
ARCHIVE_MEDIA_STORAGE_REGION_NAME = os.getenv("ARCHIVE_MEDIA_STORAGE_REGION_NAME", "")
ARCHIVE_MEDIA_STORAGE_ACCESS_KEY_ID = os.getenv("ARCHIVE_MEDIA_STORAGE_ACCESS_KEY_ID", "")
ARCHIVE_MEDIA_STORAGE_SECRET_ACCESS_KEY = os.getenv(
    "ARCHIVE_MEDIA_STORAGE_SECRET_ACCESS_KEY",
    "",
)
ARCHIVE_MEDIA_STORAGE_ADDRESSING_STYLE = os.getenv(
    "ARCHIVE_MEDIA_STORAGE_ADDRESSING_STYLE",
    "path",
)

archive_media_storage: dict[str, object] = {
    "BACKEND": ARCHIVE_MEDIA_STORAGE_BACKEND,
    "OPTIONS": {},
}
archive_media_options = archive_media_storage["OPTIONS"]
if not isinstance(archive_media_options, dict):
    raise RuntimeError("Archive media storage options must be a dictionary")
if ARCHIVE_MEDIA_STORAGE_BACKEND == "django.core.files.storage.FileSystemStorage":
    archive_media_options["location"] = ARCHIVE_MEDIA_STORAGE_LOCATION
    if ARCHIVE_MEDIA_STORAGE_BASE_URL:
        archive_media_options["base_url"] = ARCHIVE_MEDIA_STORAGE_BASE_URL
elif ARCHIVE_MEDIA_STORAGE_BACKEND == "storages.backends.s3.S3Storage":
    archive_media_options.update(
        {
            "bucket_name": ARCHIVE_MEDIA_STORAGE_BUCKET_NAME,
            "endpoint_url": ARCHIVE_MEDIA_STORAGE_ENDPOINT_URL or None,
            "region_name": ARCHIVE_MEDIA_STORAGE_REGION_NAME or None,
            "access_key": ARCHIVE_MEDIA_STORAGE_ACCESS_KEY_ID or None,
            "secret_key": ARCHIVE_MEDIA_STORAGE_SECRET_ACCESS_KEY or None,
            "default_acl": "private",
            "file_overwrite": True,
            "querystring_auth": True,
            "addressing_style": ARCHIVE_MEDIA_STORAGE_ADDRESSING_STYLE,
        }
    )
STORAGES["archive_media"] = archive_media_storage
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
