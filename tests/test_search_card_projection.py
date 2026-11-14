import pytest
from django.db import connection

from archive.models import Item

pytestmark = pytest.mark.django_db
TEXT_FIELDS = ("short_summary", "notes", "long_summary", "transcript")


@pytest.mark.parametrize("fallback", TEXT_FIELDS)
def test_search_fetches_bounded_excerpt_and_keeps_full_detail(
    client, fallback, django_assert_num_queries
):
    fields = {field: "" for field in TEXT_FIELDS}
    for field in TEXT_FIELDS[TEXT_FIELDS.index(fallback) :]:
        fields[field] = field + " 界🦉<>&\n" * 50000 + f"{field}_FULL_END"
    item = Item.objects.create(
        original_url="https://example.com/large-search",
        title="Needle",
        source="Synthetic source",
        **fields,
    )
    with django_assert_num_queries(3) as queries:
        response = client.get("/search/", {"q": "need"})
    assert response.status_code == 200
    result = response.context["results"][0]
    assert set(TEXT_FIELDS) <= result.get_deferred_fields()
    assert len(result.card_excerpt) <= 241
    assert result.card_excerpt == fields[fallback][:241]
    # Inspect actual database output, not only the final template truncation.
    with connection.cursor() as cursor:
        cursor.execute(queries.captured_queries[-1]["sql"])
        rows = cursor.fetchall()
    assert len(rows) == 1
    assert all(len(value) <= 500 for value in rows[0] if isinstance(value, str))
    assert len(response.content) < 15000
    assert f"{fallback}_FULL_END".encode() not in response.content
    assert item.get_absolute_url().encode() in response.content
    assert b"Synthetic source" in response.content
    assert fallback.encode() in response.content
    detail = client.get(item.get_absolute_url())
    assert detail.status_code == 200
    assert f"{fallback}_FULL_END".encode() in detail.content
    item.refresh_from_db()
    assert getattr(item, fallback) == fields[fallback]


def test_search_empty_and_short_fallbacks_preserve_display(client):
    blank = Item.objects.create(original_url="https://example.com/blank", title="Needle")
    short = Item.objects.create(
        original_url="https://example.com/short", title="Needle", notes="Short & safe"
    )
    response = client.get("/search/", {"q": "need"})
    assert {result.pk: result.card_excerpt for result in response.context["results"]} == {
        blank.pk: "",
        short.pk: "Short & safe",
    }
    assert b"Short &amp; safe" in response.content
