import os
from pathlib import Path

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
]

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
    }
}

AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []

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
ARCHIVE_TRANSCRIPTION_POLL_SECONDS = float(
    os.getenv("ARCHIVE_TRANSCRIPTION_POLL_SECONDS", "30")
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
