"""Public query limits with real SQLite FTS and narrow week projections."""

from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from archive.models import Item

pytestmark = pytest.mark.django_db


def test_week_pages_project_only_public_fields_and_visit_every_item(client):
    shared_at = timezone.make_aware(datetime(2026, 7, 1, 12))
    items = [
        Item.objects.create(
            original_url=f"https://example.com/week/{index}",
            shared_at=shared_at + timedelta(minutes=index // 7),
            title="Synthetic",
            transcript="x" * (2 * 1024 * 1024 if index == 0 else 100),
            long_summary="unused",
            classification_evidence={"unused": "payload"},
            is_public=index != 10,
        )
        for index in range(123)
    ]
    queries = []

    def record(execute, sql, params, many, context):
        queries.append(sql)
        return execute(sql, params, many, context)

    url = reverse("archive:weekly-items-json", kwargs={"week": "2026-W27"})
    seen = []
    with connection.execute_wrapper(record):
        for _ in range(4):
            response = client.get(url)
            assert response.status_code == 200
            payload = response.json()
            assert len(payload["items"]) <= 50
            assert len(response.content) < 50_000
            seen.extend(item["id"] for item in payload["items"])
            url = payload["next"]
            if url is None:
                break
            assert urlsplit(url).path == reverse(
                "archive:weekly-items-json", kwargs={"week": "2026-W27"}
            )
        else:
            pytest.fail("Continuation did not terminate")
    assert seen == [item.pk for item in items if item.is_public]
    selects = [sql for sql in queries if 'FROM "archive_item"' in sql]
    assert len(selects) == 3
    for sql in selects:
        assert "LIMIT 51" in sql
        assert not any(
            f'"{name}"' in sql
            for name in ("transcript", "notes", "long_summary", "classification_evidence")
        )


@pytest.mark.parametrize("cursor", ["bad", "x" * 1000])
def test_week_refuses_invalid_cursor(client, cursor):
    response = client.get(
        reverse("archive:weekly-items-json", kwargs={"week": "2026-W27"}), {"cursor": cursor}
    )
    assert response.status_code == 400
    assert response.json() == {"error": "Invalid cursor"}


@pytest.mark.parametrize(
    "query", ["x" * 513, "word " * 33, " ".join(map(str, range(33))), "ü" * 65]
)
def test_search_refuses_excess_work_before_fts_cursor(client, query):
    def refuse_fts(execute, sql, params, many, context):
        assert "archive_item_fts" not in sql
        return execute(sql, params, many, context)

    with connection.execute_wrapper(refuse_fts):
        response = client.get(reverse("archive:search"), {"q": query})
    assert response.status_code == 400
    assert response.context["results"] == []
    assert response.context["search_error"]
    assert len(response.context["search_query"]) <= 512


@pytest.mark.parametrize("query", ["ü" * 64, "wort " * 31 + "wort", "a" + "!" * 511])
def test_search_exact_limits_preserve_unicode_prefix_and_punctuation(client, query):
    item = Item.objects.create(original_url="https://example.com/boundary", title=query)
    response = client.get(reverse("archive:search"), {"q": query})
    assert response.status_code == 200
    assert [result.pk for result in response.context["results"]] == [item.pk]
