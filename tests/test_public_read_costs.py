from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection
from django.db.models.query import QuerySet
from django.utils import timezone

from archive import views
from archive.models import Item, ProcessingStep
from archive.processing_health import processing_health_snapshot, record_step_outcome

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


@pytest.mark.parametrize("remove", ["private", "deleted"])
def test_search_rechecks_visibility_at_batched_hydration(
    client, monkeypatch, django_assert_num_queries, remove
):
    for n in range(4):
        Item.objects.create(original_url=f"https://example.com/{n}", title="Rankword " * (n + 1))
    select = views._search_public_item_page
    hydrate = QuerySet.in_bulk
    ranked_ids = []
    fetches = []

    def interleave(*args, **kwargs):
        page = select(*args, **kwargs)
        ranked_ids.extend(page.object_list)
        target = Item.objects.filter(pk=ranked_ids[1])
        if remove == "private":
            target.update(
                is_public=False,
                title="PRIVATE TITLE MARKER",
                short_summary="PRIVATE SUMMARY MARKER",
            )
        else:
            target.delete()
        return page

    def observe_fetch(self, *args, **kwargs):
        with django_assert_num_queries(1) as queries:
            result = hydrate(self, *args, **kwargs)
        fetches.extend(queries.captured_queries)
        return result

    monkeypatch.setattr(views, "_search_public_item_page", interleave)
    monkeypatch.setattr(QuerySet, "in_bulk", observe_fetch)
    response = client.get("/search/", {"q": "rankw"})
    assert response.status_code == 200
    assert [item.pk for item in response.context["results"]] == ranked_ids[:1] + ranked_ids[2:]
    assert b"PRIVATE TITLE MARKER" not in response.content
    assert b"PRIVATE SUMMARY MARKER" not in response.content
    assert len(fetches) == 1


@pytest.mark.parametrize("size", [1, 1000])
def test_week_discovery_returns_one_database_row_per_occupied_week(size, django_assert_num_queries):
    first = datetime(2021, 1, 1, 12, tzinfo=UTC)
    second = datetime(2021, 1, 6, 12, tzinfo=UTC)
    Item.objects.bulk_create(
        [
            Item(original_url=f"https://example.com/{n}", shared_at=stamp)
            for n, stamp in enumerate([first] * size + [second] * size)
        ]
    )
    Item.objects.create(original_url="https://example.com/private", is_public=False, shared_at=NOW)
    with timezone.override("Europe/Berlin"), django_assert_num_queries(1) as queries:
        pages = views._build_week_navigation(Item.objects.filter(is_public=True))
    assert [page.token for page in pages] == ["2021-W01", "2020-W53"]
    # Query count alone cannot distinguish a timestamp per item from grouped weeks.
    with connection.cursor() as cursor:
        cursor.execute(queries.captured_queries[0]["sql"])
        assert len(cursor.fetchall()) == 2


def test_week_discovery_keeps_local_iso_year_dst_gaps_and_adjacent_links(client):
    dates = [
        datetime(2021, 1, 3, 22, 59, tzinfo=UTC),  # Sunday in ISO year 2020
        datetime(2021, 1, 3, 23, 0, tzinfo=UTC),  # local Monday in ISO year 2021
        datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC),  # jump forward, same week
        datetime(2026, 3, 29, 22, 0, tzinfo=UTC),  # local Monday, DST offset
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),  # repeated local hour, same week
    ]
    items = [
        Item.objects.create(original_url=f"https://example.com/{n}", shared_at=stamp)
        for n, stamp in enumerate(dates)
    ]
    with timezone.override("Europe/Berlin"):
        pages = views._build_week_navigation(Item.objects.filter(is_public=True))
        assert [page.token for page in pages] == [
            "2026-W43",
            "2026-W14",
            "2026-W13",
            "2021-W01",
            "2020-W53",
        ]
        response = client.get("/", {"week": "2026-W14"})
    assert response.status_code == 200
    assert response.context["current_page"].token == "2026-W14"
    assert response.context["newer_page"].token == "2026-W43"
    assert response.context["older_page"].token == "2026-W13"
    assert [item.pk for item in response.context["items"]] == [items[4].pk]


@pytest.mark.parametrize("size", [1, 50])
def test_rendered_overview_projects_card_dependencies_with_constant_queries(
    client, size, django_assert_num_queries
):
    items = []
    for n in range(size):
        items.append(
            Item.objects.create(
                original_url=f"https://example.com/card-{n}",
                title=f"Title {n}" if n % 2 else "",
                shared_at=NOW,
                kind="article",
                source=f"Source {n}",
                short_summary=f"Summary {n}" if n % 2 else "",
                notes=f"Note {n}",
                transcript="TRANSCRIPT_MARKER " * 10000,
                long_summary="LONG_SUMMARY_MARKER " * 10000,
                classification_evidence={"large": "EVIDENCE_MARKER " * 1000},
                summary_error="DIAGNOSTIC_MARKER " * 1000,
            )
        )
    # Anonymous first render has no session/content-type/site cache prerequisites.
    with django_assert_num_queries(2) as queries:
        response = client.get("/")
    assert response.status_code == 200
    assert len(response.context["items"]) == size
    for item in response.context["items"]:
        deferred = item.get_deferred_fields()
        assert {
            "transcript",
            "long_summary",
            "classification_evidence",
            "summary_error",
        } <= deferred
    sql = " ".join(q["sql"] for q in queries.captured_queries)
    for field in ("transcript", "long_summary", "classification_evidence", "summary_error"):
        assert f'"{field}"' not in sql
    content = response.content.decode()
    for n, item in enumerate(items):
        assert (f"Title {n}" if n % 2 else item.original_url) in content
        assert (f"Summary {n}" if n % 2 else f"Note {n}") in content
        assert f"Source {n}" in content
        assert item.get_absolute_url() in content
    assert "TRANSCRIPT_MARKER" not in content


@pytest.mark.parametrize("active_count", [0, 1, len(ProcessingStep)])
def test_health_groups_all_current_streak_counts_in_one_query(
    active_count, django_assert_num_queries
):
    steps = ProcessingStep.values[:active_count]
    for index, step in enumerate(steps):
        record_step_outcome(step, succeeded=False, item_id=99, now=NOW - timedelta(days=5))
        record_step_outcome(step, succeeded=True, item_id=0, now=NOW - timedelta(days=4))
        start = NOW - timedelta(hours=30 - index * 5)
        record_step_outcome(step, succeeded=False, item_id=10, now=start)
        record_step_outcome(step, succeeded=False, item_id=10, now=NOW - timedelta(hours=1))
        record_step_outcome(step, succeeded=False, item_id=20, now=NOW - timedelta(minutes=30))
        record_step_outcome(step, succeeded=False, item_id=None, now=NOW)
    with django_assert_num_queries(3 if active_count else 2):
        snapshot = processing_health_snapshot(NOW)
    for index, step in enumerate(steps):
        data = snapshot["steps"][step]
        assert data["failing_items"] == 2
        assert data["failing_since"] == (NOW - timedelta(hours=30 - index * 5)).isoformat()
        assert data["failures"] == (3 if index < 2 else 4)
    if steps:
        record_step_outcome(steps[0], succeeded=True, item_id=30, now=NOW)
        with django_assert_num_queries(3 if active_count > 1 else 2):
            reset = processing_health_snapshot(NOW)
        assert reset["steps"][steps[0]]["failing_items"] == 0
        assert reset["steps"][steps[0]]["failing_since"] is None
