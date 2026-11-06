import json
from unittest.mock import Mock

import pytest

from archive.models import Item

FIELDS = (
    "url",
    "title",
    "notes",
    "audio_url",
    "media_url",
    "source",
    "author",
    "kind",
    "original_published_at",
)
LIMITS = {
    "url": 200,
    "title": 500,
    "audio_url": 200,
    "media_url": 200,
    "source": 255,
    "author": 255,
    "kind": 32,
}
HEADERS = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def no_capture(monkeypatch):
    classify = Mock(side_effect=AssertionError("must validate before classification"))
    capture = Mock(side_effect=AssertionError("must validate before capture"))
    monkeypatch.setattr("archive.views.classify_item", classify)
    monkeypatch.setattr("archive.views.capture_item", capture)
    return classify, capture


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [42, True, False, 0, [], {}, ["text"]])
def test_capture_rejects_nonstring_fields(client, no_capture, field, value):
    payload = {"url": "https://example.com/story", field: value}
    response = client.post(
        "/api/items/", json.dumps(payload), content_type="application/json", headers=HEADERS
    )
    assert response.status_code == 400
    assert response.json() == {"error": f"Invalid {field}"}
    for call in no_capture:
        call.assert_not_called()


@pytest.mark.parametrize("field,limit", LIMITS.items())
def test_capture_rejects_overlong_fields(client, no_capture, field, limit):
    payload = {"url": "https://example.com/story", field: "x" * (limit + 1)}
    response = client.post(
        "/api/items/", json.dumps(payload), content_type="application/json", headers=HEADERS
    )
    assert response.status_code == 400
    assert response.json() == {"error": f"Invalid {field}"}
    for call in no_capture:
        call.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("empty", [None, "", "  "])
def test_capture_preserves_optional_empty_fields(client, empty):
    payload = dict.fromkeys(FIELDS, empty)
    payload["url"] = "https://example.com/story"
    response = client.post(
        "/api/items/", json.dumps(payload), content_type="application/json", headers=HEADERS
    )
    assert response.status_code == 201
    item = Item.objects.get()
    assert item.notes == item.title == item.source == item.author == ""
    assert item.original_published_at is None


@pytest.mark.django_db
def test_capture_accepts_model_length_boundaries_and_long_notes(client):
    url = "https://example.com/"
    url += "a" * (200 - len(url))
    payload = {
        "url": url,
        "audio_url": url,
        "media_url": url,
        "title": "t" * 500,
        "source": "s" * 255,
        "author": "a" * 255,
        "notes": "n" * 10000,
        "kind": "article",
    }
    response = client.post(
        "/api/items/", json.dumps(payload), content_type="application/json", headers=HEADERS
    )
    assert response.status_code == 201
    item = Item.objects.get()
    for name, value in payload.items():
        assert getattr(item, "original_url" if name == "url" else name) == value
