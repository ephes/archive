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
