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
