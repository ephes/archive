import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.urls import reverse

from archive.models import Item

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "archive" / "templates"

INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
INLINE_STYLE = re.compile(rb"<style\b|\sstyle=", re.IGNORECASE)
INLINE_HANDLER = re.compile(rb"\son[a-z]+\s*=", re.IGNORECASE)


def csp_directives(response) -> dict[str, list[str]]:
    header = response.headers.get("Content-Security-Policy", "")
    directives: dict[str, list[str]] = {}
    for part in header.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = tokens[1:]
    return directives


def assert_strict_policy(response) -> None:
    directives = csp_directives(response)
    assert directives.get("default-src") == ["'self'"]
    assert directives.get("object-src") == ["'none'"]
    assert directives.get("base-uri") == ["'self'"]
    assert directives.get("frame-ancestors") == ["'none'"]
    assert directives.get("form-action") == ["'self'"]
    for directive in ("default-src", "script-src", "style-src"):
        values = directives.get(directive, [])
        assert "'unsafe-inline'" not in values
        assert "'unsafe-eval'" not in values
    # Only the explicit media exception may widen the policy.
    widened = {key for key in directives if key.endswith("-src")} - {
        "default-src",
        "object-src",
        "media-src",
    }
    assert widened == set()


def assert_no_inline_code(content: bytes) -> None:
    assert INLINE_SCRIPT.search(content) is None
    assert INLINE_STYLE.search(content) is None
    assert INLINE_HANDLER.search(content) is None


@pytest.mark.django_db
def test_public_pages_send_strict_policy_without_inline_code(client) -> None:
    item = Item.objects.create(
        original_url="https://example.com/article",
        title="Example article",
        audio_url="https://cdn.example.com/episode.mp3",
    )

    for url in (
        reverse("archive:overview"),
        reverse("archive:search") + "?q=example",
        reverse("archive:item-detail", kwargs={"pk": item.pk}),
        reverse("archive:login"),
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        assert_strict_policy(response)
        assert_no_inline_code(response.content)


@pytest.mark.django_db
def test_page_assets_are_same_origin_static_files(client) -> None:
    response = client.get(reverse("archive:overview"))

    assert b'<link rel="stylesheet" href="/static/archive/base.css">' in response.content
    assert b'<script src="/static/archive/base.js" defer></script>' in response.content
    assert finders.find("archive/base.css")
    assert finders.find("archive/base.js")


@pytest.mark.django_db
def test_external_item_audio_stays_playable(client) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        title="External episode",
        audio_url="https://cdn.example.com/episode.mp3",
    )

    response = client.get(reverse("archive:item-detail", kwargs={"pk": item.pk}))

    assert b'src="https://cdn.example.com/episode.mp3"' in response.content
    assert csp_directives(response).get("media-src") == ["'self'", "https:"]


@pytest.mark.django_db
def test_admin_pages_send_policy(client) -> None:
    response = client.get(reverse("admin:login"))
    assert response.status_code == 200
    assert_strict_policy(response)

    user = get_user_model().objects.create_superuser("csp-admin", password="pw-for-tests-only")
    client.force_login(user)
    for url in (
        reverse("admin:index"),
        reverse("admin:archive_item_changelist"),
        reverse("admin:archive_item_add"),
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        assert_strict_policy(response)
        assert INLINE_HANDLER.search(response.content) is None


@pytest.mark.django_db
def test_item_form_sends_policy_without_inline_code(client) -> None:
    user = get_user_model().objects.create_superuser("csp-editor", password="pw-for-tests-only")
    client.force_login(user)

    response = client.get(reverse("archive:item-new"))

    assert response.status_code == 200
    assert_strict_policy(response)
    assert_no_inline_code(response.content)


def test_templates_contain_no_inline_code() -> None:
    for template in TEMPLATES_DIR.rglob("*.html"):
        assert_no_inline_code(template.read_bytes())
