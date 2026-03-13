from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from archive.models import Item, ItemKind
from archive.services import infer_kind


@pytest.mark.django_db
def test_homepage_renders_public_items(client, home_url: str) -> None:
    Item.objects.create(
        original_url="https://example.com/article",
        title="Example article",
        notes="Short note",
    )

    response = client.get(home_url)

    assert response.status_code == 200
    assert b"Example article" in response.content
    assert b"Short note" in response.content


@pytest.mark.django_db
def test_homepage_prefers_short_summary_over_notes(client, home_url: str) -> None:
    Item.objects.create(
        original_url="https://example.com/article",
        title="Example article",
        short_summary="Generated overview summary",
        notes="Internal note that should not lead the card",
    )

    response = client.get(home_url)

    assert response.status_code == 200
    assert b"Generated overview summary" in response.content
    assert b"Internal note that should not lead the card" not in response.content


@pytest.mark.django_db
def test_overview_uses_week_navigation_and_skips_empty_weeks(client, home_url: str) -> None:
    now = timezone.now()
    current_item = Item.objects.create(
        original_url="https://example.com/current",
        title="Current week",
        shared_at=now,
    )
    older_item = Item.objects.create(
        original_url="https://example.com/older",
        title="Older week",
        shared_at=now - timedelta(days=14),
    )

    response = client.get(home_url)

    assert response.status_code == 200
    assert b"Current week" in response.content
    assert current_item.get_absolute_url().encode() in response.content
    assert older_item.get_absolute_url().encode() not in response.content
    older_week = (now - timedelta(days=14)).isocalendar()
    assert (
        b"Older week"
        in client.get(f"{home_url}?week={older_week.year}-W{older_week.week:02d}").content
    )


@pytest.mark.django_db
def test_detail_page_shows_audio_player(client) -> None:
    item = Item.objects.create(
        original_url="https://example.com/episode",
        title="Radio feature",
        short_summary="Short summary",
        long_summary="Long summary for the item.",
        transcript="Transcript paragraph one.\n\nTranscript paragraph two.",
        tags="radio\nfeature\nculture",
        audio_url="https://cdn.example.com/audio.mp3",
        kind=ItemKind.PODCAST_EPISODE,
    )

    response = client.get(reverse("archive:item-detail", kwargs={"pk": item.pk}))

    assert response.status_code == 200
    assert b"audio" in response.content
    assert b"Open original" in response.content
    assert b"Short summary" in response.content
    assert b"Long summary for the item." in response.content
    assert b"Transcript paragraph one." in response.content
    assert b"feature" in response.content


@pytest.mark.django_db
def test_api_creates_item_and_returns_detail_url(client, api_url: str, settings) -> None:
    settings.ARCHIVE_API_TOKEN = "test-token"

    response = client.post(
        api_url,
        data='{"url":"https://example.com/shared","title":"Shared link","notes":"From shortcut"}',
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 201
    body = response.json()
    item = Item.objects.get(pk=body["id"])
    assert item.title == "Shared link"
    assert body["detail_url"].endswith(item.get_absolute_url())


@pytest.mark.django_db
def test_api_rejects_invalid_token(client, api_url: str, settings) -> None:
    settings.ARCHIVE_API_TOKEN = "right-token"

    response = client.post(
        api_url,
        data='{"url":"https://example.com/shared"}',
        content_type="application/json",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_api_accepts_token_auth_without_csrf(settings) -> None:
    settings.ARCHIVE_API_TOKEN = "test-token"
    client = Client(enforce_csrf_checks=True)

    response = client.post(
        reverse("archive:api-items"),
        data='{"url":"https://example.com/shared"}',
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 201


@pytest.mark.django_db
def test_api_rejects_missing_url(client, api_url: str, settings) -> None:
    settings.ARCHIVE_API_TOKEN = "test-token"

    response = client.post(
        api_url,
        data='{"title":"Missing URL"}',
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_api_rejects_invalid_url(client, api_url: str, settings) -> None:
    settings.ARCHIVE_API_TOKEN = "test-token"

    response = client.post(
        api_url,
        data='{"url":"not-a-url"}',
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_api_rejects_invalid_json(client, api_url: str, settings) -> None:
    settings.ARCHIVE_API_TOKEN = "test-token"

    response = client.post(
        api_url,
        data="{",
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_api_rejects_empty_body(client, api_url: str, settings) -> None:
    settings.ARCHIVE_API_TOKEN = "test-token"

    response = client.post(
        api_url,
        data="",
        content_type="application/json",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_editor_form_requires_login_and_creates_item(client, editor_user) -> None:
    new_item_url = reverse("archive:item-new")
    response = client.get(new_item_url)
    assert response.status_code == 302

    client.force_login(editor_user)
    response = client.post(
        new_item_url,
        data={
            "original_url": "https://example.com/manual",
            "title": "Manual capture",
            "short_summary": "Manual short summary",
            "long_summary": "Manual long summary",
            "transcript": "Manual transcript",
            "tags": "manual\ntest",
            "notes": "Fallback form",
            "kind": ItemKind.LINK,
            "source": "Safari",
            "audio_url": "",
            "is_public": "on",
        },
    )

    item = Item.objects.get(title="Manual capture")
    assert response.status_code == 302
    assert response["Location"].endswith(item.get_absolute_url())
    assert item.short_summary == "Manual short summary"
    assert item.long_summary == "Manual long summary"
    assert item.transcript == "Manual transcript"
    assert item.tags == "manual\ntest"


@pytest.mark.django_db
def test_editor_form_exposes_generated_fields_for_manual_edits(client, editor_user) -> None:
    client.force_login(editor_user)

    response = client.get(reverse("archive:item-new"))

    assert response.status_code == 200
    assert b'name="short_summary"' in response.content
    assert b'name="long_summary"' in response.content
    assert b'name="transcript"' in response.content
    assert b'name="tags"' in response.content


@pytest.mark.django_db
def test_detail_page_returns_404_for_non_public_item(client) -> None:
    item = Item.objects.create(
        original_url="https://example.com/private",
        title="Private item",
        is_public=False,
    )

    response = client.get(reverse("archive:item-detail", kwargs={"pk": item.pk}))

    assert response.status_code == 404


@pytest.mark.django_db
def test_detail_page_returns_404_for_unknown_item(client) -> None:
    response = client.get(reverse("archive:item-detail", kwargs={"pk": 99999}))

    assert response.status_code == 404


@pytest.mark.django_db
def test_admin_changelist_is_available_for_staff(client, editor_user) -> None:
    client.force_login(editor_user)

    response = client.get(reverse("admin:archive_item_changelist"))

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("url", "explicit_kind", "audio_url", "expected"),
    [
        ("https://example.com/article", ItemKind.ARTICLE, "", ItemKind.ARTICLE),
        (
            "https://example.com/article",
            "",
            "https://cdn.example.com/audio.mp3",
            ItemKind.PODCAST_EPISODE,
        ),
        ("https://youtu.be/demo", "", "", ItemKind.VIDEO),
        ("https://example.com/audio.mp3", "", "", ItemKind.PODCAST_EPISODE),
        ("https://example.com/article", "", "", ItemKind.LINK),
    ],
)
def test_infer_kind(url: str, explicit_kind: str, audio_url: str, expected: str) -> None:
    assert infer_kind(url=url, explicit_kind=explicit_kind, audio_url=audio_url) == expected
