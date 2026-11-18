import pytest


@pytest.fixture
def home_url() -> str:
    return "/"


@pytest.fixture
def api_url() -> str:
    return "/api/items/"


@pytest.fixture
def editor_user(django_user_model):
    return django_user_model.objects.create_user(
        username="editor",
        password="secret-pass",
        is_staff=True,
        is_superuser=True,
    )


@pytest.fixture(autouse=True)
def isolated_media_storage(settings, tmp_path, monkeypatch):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }
    from django.core.files.storage import storages

    from archive.models import Item

    monkeypatch.setattr(
        Item._meta.get_field("article_audio_file"), "storage", storages["archive_media"]
    )


@pytest.fixture(autouse=True)
def clear_default_cache():
    """The default local-memory cache outlives a test; start each one empty."""
    from django.core.cache import cache

    cache.clear()


@pytest.fixture(autouse=True)
def ample_temporary_space(monkeypatch, settings, tmp_path):
    """Media tests use tiny fixtures; the host's real free disk must not decide them.

    Tests of the temporary-space guard override ``_temp_free_bytes`` themselves.
    """
    monkeypatch.setattr("archive.media_archival._temp_free_bytes", lambda: 2**62)
    settings.ARCHIVE_MEDIA_TEMP_LOCK_PATH = str(tmp_path / "media-temp-space.lock")
