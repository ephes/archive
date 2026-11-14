from datetime import UTC, datetime, timedelta

import pytest

from archive.models import Item

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def test_busy_week_cards_are_bounded_and_equal_times_traverse_once(client):
    items = Item.objects.bulk_create(
        [Item(original_url=f"https://example.com/{n}", shared_at=NOW) for n in range(1003)]
    )
    Item.objects.create(original_url="https://example.com/private", shared_at=NOW, is_public=False)
    Item.objects.create(
        original_url="https://example.com/older", shared_at=NOW - timedelta(weeks=1)
    )
    expected = [item.pk for item in reversed(items)]
    seen = []
    for number in range(1, 22):
        response = client.get("/", {"week": "2026-W45", "page": number})
        assert response.status_code == 200
        cards = list(response.context["items"])
        assert len(cards) <= 50
        seen.extend(item.pk for item in cards)
        page = response.context["page_obj"]
        assert page.number == number
        assert page.paginator.count == 1003
        assert page.paginator.num_pages == 21
        content = response.content.decode()
        assert content.count('<article class="card">') == len(cards)
        if number < 21:
            assert f"?week=2026-W45&page={number + 1}" in content
        if number > 1:
            assert f"?week=2026-W45&page={number - 1}" in content
        assert '?week=2026-W44"' in content
    assert seen == expected


@pytest.mark.parametrize("value,number", [("bad", 1), (0, 2), (-1, 2), (999, 2)])
def test_overview_invalid_page_and_default_week(client, value, number):
    Item.objects.bulk_create(
        [Item(original_url=f"https://example.com/{n}", shared_at=NOW) for n in range(51)]
    )
    response = client.get("/", {"page": value})
    assert response.status_code == 200
    assert response.context["page_obj"].number == number
    assert response.context["current_page"].token == "2026-W45"


def test_empty_overview_and_unknown_week(client):
    response = client.get("/", {"page": 999})
    assert response.status_code == 200
    assert list(response.context["items"]) == []
    assert response.context["page_obj"].number == 1
    assert b"No public items for this week yet." in response.content
    Item.objects.create(original_url="https://example.com/one", shared_at=NOW)
    assert client.get("/", {"week": "2026-W44"}).status_code == 400
