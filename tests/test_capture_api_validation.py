import json
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from django.urls import resolve

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


@pytest.mark.parametrize("method,path", [("post", "/api/items/"), ("patch", "/api/items/1/")])
@pytest.mark.parametrize("body", [b"\xff", b"\xff\xfe{\x00\x00", b"{", b"[]"])
def test_api_rejects_invalid_json_encoding(client, no_capture, method, path, body):
    response = getattr(client, method)(path, body, content_type="application/json", headers=HEADERS)
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid JSON"}
    for call in no_capture:
        call.assert_not_called()


@pytest.mark.parametrize("method,path", [("post", "/api/items/"), ("patch", "/api/items/1/")])
def test_api_authenticates_before_decoding(client, method, path):
    response = getattr(client, method)(path, b"\xff", content_type="application/json")
    assert response.status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize("method,path", [("post", "/api/items/"), ("patch", "/api/items/{pk}/")])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_api_preserves_valid_json_encodings(client, method, path, encoding):
    item = Item.objects.create(original_url="https://example.com/existing")
    body = json.dumps({"url": "https://example.com/story", "kind": "quote"}).encode(encoding)
    response = getattr(client, method)(
        path.format(pk=item.pk), body, content_type="application/json", headers=HEADERS
    )
    assert response.status_code == (201 if method == "post" else 200)


@pytest.mark.parametrize(
    "value",
    [
        "9999-12-31T23:59:59-23:59",
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59Z",
        "0001-01-01T00:00:00",
        "2026-02-30T12:00:00Z",
        "2026-01-01T12:00:00+24:00",
        "not-a-date",
    ],
)
def test_capture_rejects_unrepresentable_timestamp(client, no_capture, settings, value):
    settings.TIME_ZONE = "Europe/Berlin"
    response = client.post(
        "/api/items/",
        json.dumps(
            {
                "url": "https://example.com/story",
                "original_published_at": value,
            }
        ),
        content_type="application/json",
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid original_published_at"}
    for call in no_capture:
        call.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-01-01T12:00:00+02:00", datetime(2026, 1, 1, 10, tzinfo=UTC)),
        ("2026-01-01T12:00:00", datetime(2026, 1, 1, 11, tzinfo=UTC)),
        ("0001-01-01T00:00:00Z", datetime(1, 1, 1, tzinfo=UTC)),
        ("9999-12-31T22:59:59Z", datetime(9999, 12, 31, 22, 59, 59, tzinfo=UTC)),
    ],
)
def test_capture_normalizes_timestamp_to_utc(client, settings, value, expected):
    settings.TIME_ZONE = "Europe/Berlin"
    response = client.post(
        "/api/items/",
        json.dumps(
            {
                "url": "https://example.com/story",
                "original_published_at": value,
            }
        ),
        content_type="application/json",
        headers=HEADERS,
    )
    assert response.status_code == 201
    item = Item.objects.get()
    assert item.original_published_at == expected
    assert client.get(item.get_absolute_url()).status_code == 200


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


@pytest.mark.parametrize("method,path", [("post", "/api/items/"), ("patch", "/api/items/1/")])
@pytest.mark.parametrize(
    "members",
    [
        '"url":"https://example.com/a","url":"https://example.com/b"',
        '"url":"https://example.com/b","url":"https://example.com/a"',
        '"kind":"article","kind":"quote"',
        '"kind":"quote","kind":"article"',
        '"notes":"first","notes":"second"',
        '"notes":"second","notes":"first"',
        '"ignored":{"nested":[{"key":1,"key":2}]}',
        '"ignored":{"nested":[{"key":2,"key":1}]}',
        '"notes":"first","no\\u0074es":"second"',
    ],
)
def test_capture_refuses_duplicate_json_members_before_work(
    client, no_capture, method, path, members
):
    # The default URL/kind keep unrelated validation from masking duplicate handling.
    body = '{"url":"https://example.com/story","kind":"quote",' + members + "}"
    response = getattr(client, method)(path, body, content_type="application/json", headers=HEADERS)
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid JSON"}
    for call in no_capture:
        call.assert_not_called()


@pytest.mark.parametrize("method,path", [("POST", "/api/items/"), ("PATCH", "/api/items/1/")])
@pytest.mark.parametrize("declared_length", [True, False])
def test_capture_rejects_oversized_body_before_decoding(
    rf, monkeypatch, no_capture, method, path, declared_length
):
    body = b'{"ignored":"' + b"x" * (1024 * 1024) + b'"}'
    request = rf.generic(method, path, body, content_type="application/json", headers=HEADERS)
    if not declared_length:
        # Model a server that supplies a readable stream without Content-Length.
        del request.META["CONTENT_LENGTH"]
    read = Mock(wraps=request.read)
    monkeypatch.setattr(request, "read", read)
    parse = Mock(side_effect=AssertionError("must bound the body before JSON decoding"))
    monkeypatch.setattr("archive.views.json.loads", parse)

    route = resolve(path)
    response = route.func(request, **route.kwargs)

    assert response.status_code == 413
    assert response.content == b'{"error": "Request body too large"}'
    parse.assert_not_called()
    if declared_length:
        read.assert_not_called()
    else:
        read.assert_called_once_with(1024 * 1024 + 1)
    for work in no_capture:
        work.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_interrupted_capture_body_is_rejected_before_work(rf, monkeypatch, no_capture, method):
    item = Item.objects.create(original_url="https://example.com/existing")
    before = list(Item.objects.values())
    path = "/api/items/" if method == "POST" else f"/api/items/{item.pk}/"
    prefix = b'{"url":"https://example.com/new","kind":"quote"}'
    request = rf.generic(method, path, prefix, content_type="application/json", headers=HEADERS)
    stream = Mock()
    stream.read.side_effect = [prefix, ConnectionResetError("private transport details")]
    monkeypatch.setattr(request, "_stream", stream)
    route = resolve(path)
    response = route.func(request, **route.kwargs)

    assert response.status_code == 400
    assert json.loads(response.content) == {"error": "Could not read request body"}
    assert stream.read.call_count == 2
    assert list(Item.objects.values()) == before
    for work in no_capture:
        work.assert_not_called()


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_capture_rejects_lone_surrogates_before_work(client, no_capture, field, surrogate):
    body = json.dumps({"url": "https://example.com/story", field: f"text {surrogate}"})
    response = client.post("/api/items/", body, content_type="application/json", headers=HEADERS)

    assert response.status_code == 400
    assert response.json() == {"error": f"Invalid {field}"}
    for work in no_capture:
        work.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_capture_preserves_unicode_scalar_values(client, ensure_ascii):
    notes = "Grüße aus Köln 📝"
    body = json.dumps(
        {"url": "https://example.com/story", "notes": notes}, ensure_ascii=ensure_ascii
    )
    response = client.post(
        "/api/items/", body.encode(), content_type="application/json", headers=HEADERS
    )

    assert response.status_code == 201
    assert Item.objects.get().notes == notes
