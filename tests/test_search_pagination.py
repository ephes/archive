from datetime import timedelta

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from archive.models import Item


@pytest.mark.django_db
@pytest.mark.parametrize(
    "requested_page, expected_page", [(1, 1), (2, 2), (5, 5), (999, 5), (0, 5), (-1, 5), ("bad", 1)]
)
def test_search_bounds_database_results_and_preserves_pages(client, requested_page, expected_page):
    now = timezone.now()
    public_items = []
    for index in range(105):
        item = Item.objects.create(
            original_url=f"https://example.com/search/{index}",
            title="Needle",
            short_summary="Identical text keeps ranks tied.",
            transcript="synthetic transcript " * 100,
            shared_at=now - timedelta(minutes=index // 3),
            is_public=index % 5 != 0,
        )
        if item.is_public:
            public_items.append(item)
    expected = sorted(public_items, key=lambda item: (item.shared_at, item.pk), reverse=True)
    statements = []

    def record(execute, sql, params, many, context):
        if "archive_item_fts" in sql:
            statements.append((sql, params))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        response = client.get(reverse("archive:search"), {"q": "need", "page": requested_page})
    assert response.status_code == 200
    assert response.context["result_count"] == len(expected) == 84
    page_obj = response.context["page_obj"]
    assert page_obj.number == expected_page
    assert page_obj.paginator.num_pages == 5
    start = (expected_page - 1) * 20
    assert [item.pk for item in response.context["results"]] == [
        item.pk for item in expected[start : start + 20]
    ]
    result_queries = [(sql, params) for sql, params in statements if "COUNT(" not in sql.upper()]
    assert len(result_queries) == 1
    sql, params = result_queries[0]
    assert "LIMIT" in sql.upper() and "OFFSET" in sql.upper()
    assert params[-2:] == [20, start]
    assert any("COUNT(" in sql.upper() for sql, _ in statements)


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["", "!!!", "no_matches"])
def test_empty_search_pages_remain_empty(client, query):
    response = client.get(reverse("archive:search"), {"q": query, "page": 999})
    assert response.status_code == 200
    assert response.context["result_count"] == 0
    assert response.context["results"] == []
    assert response.context["page_obj"].number == 1


@pytest.mark.django_db
def test_search_relevance_precedes_date_order(client):
    now = timezone.now()
    older = Item.objects.create(
        original_url="https://example.com/relevant",
        title="Rankword " * 8,
        shared_at=now - timedelta(days=1),
    )
    newer = Item.objects.create(
        original_url="https://example.com/recent",
        title="Rankword",
        shared_at=now,
    )
    response = client.get(reverse("archive:search"), {"q": "rankw"})
    assert [item.pk for item in response.context["results"]] == [older.pk, newer.pk]
