import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.urls import reverse

from archive.models import Item

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "archive" / "templates"

INLINE_STYLE_ELEMENT = re.compile(rb"<style\b", re.IGNORECASE)


class InlineCodeFinder(HTMLParser):
    """Collects inline scripts, style elements/attributes and on* handlers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name.lower() for name, _ in attrs}
        if tag == "script" and "src" not in names:
            # Browsers use the first occurrence of a duplicated attribute.
            script_type = next((value or "" for name, value in attrs if name == "type"), "")
            if script_type.lower() not in {"application/json", "application/ld+json"}:
                self.problems.append(f"inline <script> ({self.getpos()})")
        if tag == "style":
            self.problems.append(f"<style> element ({self.getpos()})")
        if "style" in names:
            self.problems.append(f"style attribute on <{tag}> ({self.getpos()})")
        for name in names:
            if name.startswith("on"):
                self.problems.append(f"{name} handler on <{tag}> ({self.getpos()})")


def inline_code(content: bytes) -> list[str]:
    finder = InlineCodeFinder()
    finder.feed(content.decode())
    finder.close()
    return finder.problems


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
    assert inline_code(content) == []
    assert INLINE_STYLE_ELEMENT.search(content) is None


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
        assert [p for p in inline_code(response.content) if "handler" in p] == []


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


def test_inline_code_guard_catches_evasive_forms() -> None:
    assert inline_code(b'<p style = "color: red">x</p>')
    assert inline_code(b'<script data-src="/x.js">alert(1)</script>')
    assert inline_code(b'<button ONCLICK="x()">x</button>')
    assert inline_code(b'<script type="text/javascript" type="application/json">x()</script>')
    assert inline_code(b'<script type="application/json">{}</script>') == []
    assert inline_code(b'<script src="/static/x.js" defer></script>') == []


def test_policy_middleware_wraps_static_files_and_redirects() -> None:
    from config.settings import base

    middleware = base.MIDDLEWARE
    csp = middleware.index("django.middleware.csp.ContentSecurityPolicyMiddleware")
    assert csp < middleware.index("whitenoise.middleware.WhiteNoiseMiddleware")
    assert csp < middleware.index("django.middleware.common.CommonMiddleware")


@pytest.mark.django_db
def test_slash_redirect_carries_policy(client) -> None:
    response = client.get("/accounts/login")

    assert response.status_code == 301
    assert_strict_policy(response)


@pytest.mark.django_db
def test_external_audio_has_visible_link(client) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        title="External episode",
        audio_url="http://127.0.0.1:9000/episode.mp3",
    )

    response = client.get(reverse("archive:item-detail", kwargs={"pk": item.pk}))

    content = response.content.decode()
    player_end = content.index("</audio>")
    assert 'href="http://127.0.0.1:9000/episode.mp3"' in content[player_end:]
    assert "Open original audio" in content[player_end:]
