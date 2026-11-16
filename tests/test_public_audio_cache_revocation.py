"""Public audio responses must be revalidated so unpublication takes effect."""

from __future__ import annotations

import pytest
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.urls import reverse

from archive.models import EnrichmentStatus, Item, ItemKind

PAYLOAD = b"ID3-public-audio"  # 16 bytes
ROUTES = ["item-archived-audio", "item-article-audio"]


@pytest.fixture(autouse=True)
def isolated_storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "archive_media": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "media")},
        },
    }


@pytest.fixture
def closes(monkeypatch):
    seen: list[str] = []
    original = File.close

    def close(self):
        seen.append(getattr(self, "name", "") or "")
        return original(self)

    monkeypatch.setattr(File, "close", close)
    return seen


def _publish(route: str, name: str = "items/1/audio/a/source.mp3") -> tuple[Item, str]:
    path = storages["archive_media"].save(name, ContentFile(PAYLOAD))
    if route == "item-archived-audio":
        fields = {
            "kind": ItemKind.PODCAST_EPISODE,
            "archived_audio_path": path,
            "archived_audio_size_bytes": len(PAYLOAD),
        }
    else:
        fields = {
            "kind": ItemKind.ARTICLE,
            "article_audio_artifact_path": "/speech.mp3",
            "article_audio_status": EnrichmentStatus.COMPLETE,
            "article_audio_file": path,
            "article_audio_size_bytes": len(PAYLOAD),
        }
    item = Item.objects.create(original_url="https://example.com/episode", **fields)
    return item, reverse(f"archive:{route}", kwargs={"pk": item.pk})


def _body(response) -> bytes:
    return b"".join(response.streaming_content) if response.streaming else response.content


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("method", "headers", "status"),
    [("get", {}, 200), ("head", {}, 200), ("get", {"Range": "bytes=1-3"}, 206)],
)
def test_public_audio_requires_revalidation_and_carries_a_generation_validator(
    client, route, method, headers, status
):
    _, url = _publish(route)

    response = getattr(client, method)(url, headers=headers)
    _body(response)
    response.close()

    assert response.status_code == status
    assert response["Cache-Control"] == "public, no-cache"
    assert "max-age" not in response["Cache-Control"]
    assert response["ETag"].startswith('"') and response["ETag"].endswith('"')


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("method", ["get", "head"])
def test_matching_validator_revalidates_with_304_and_closes_once(client, closes, route, method):
    _, url = _publish(route)
    first = client.get(url)
    _body(first)
    first.close()
    etag = first["ETag"]
    closes.clear()

    for candidate in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        response = getattr(client, method)(
            url, headers={"If-None-Match": candidate, "Range": "bytes=1-3"}
        )
        assert response.status_code == 304
        assert response["ETag"] == etag
        assert response["Cache-Control"] == "public, no-cache"
        assert response.content == b""
        response.close()
    assert len(closes) == 4


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    ("method", "headers"),
    [("get", {}), ("head", {}), ("get", {"Range": "bytes=1-3"})],
)
@pytest.mark.parametrize("change", ["unpublish", "delete"])
def test_unpublished_audio_is_rechecked_and_errors_are_not_cacheable(
    client, route, method, headers, change
):
    item, url = _publish(route)
    first = client.get(url)
    _body(first)
    first.close()
    etag = first["ETag"]
    if change == "unpublish":
        Item.objects.filter(pk=item.pk).update(is_public=False)
    else:
        Item.objects.filter(pk=item.pk).delete()

    response = getattr(client, method)(url, headers={**headers, "If-None-Match": etag})

    assert response.status_code == 404
    assert response["Cache-Control"] == "no-store"
    assert "ETag" not in response


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
def test_new_object_generation_changes_the_validator(client, route):
    item, url = _publish(route)
    first = client.get(url)
    _body(first)
    first.close()
    replacement = storages["archive_media"].save("items/1/audio/b/source.mp3", ContentFile(PAYLOAD))
    field = "archived_audio_path" if route == "item-archived-audio" else "article_audio_file"
    Item.objects.filter(pk=item.pk).update(**{field: replacement})

    response = client.get(url, headers={"If-None-Match": first["ETag"]})

    assert response.status_code == 200
    assert response["ETag"] != first["ETag"]
    assert _body(response) == PAYLOAD
    response.close()


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
def test_if_range_honours_only_the_current_strong_validator(client, route):
    _, url = _publish(route)
    first = client.get(url)
    _body(first)
    first.close()
    etag = first["ETag"]

    matching = client.get(url, headers={"Range": "bytes=1-3", "If-Range": etag})
    assert matching.status_code == 206
    assert _body(matching) == PAYLOAD[1:4]
    matching.close()
    for stale in ('"stale"', f"W/{etag}", "Tue, 01 Jan 2030 00:00:00 GMT"):
        response = client.get(url, headers={"Range": "bytes=1-3", "If-Range": stale})
        assert response.status_code == 200
        assert _body(response) == PAYLOAD
        response.close()


@pytest.mark.django_db
@pytest.mark.parametrize("route", ROUTES)
def test_other_methods_are_not_cacheable(client, route):
    _, url = _publish(route)

    response = client.post(url)

    assert response.status_code == 405
    assert response["Cache-Control"] == "no-store"
