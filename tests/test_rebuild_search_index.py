from io import StringIO
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.urls import reverse

from archive.models import Item

pytestmark = pytest.mark.django_db


def item_rows():
    return list(Item.objects.order_by("pk").values())


def public_results(client, term):
    response = client.get(reverse("archive:search"), {"q": term})
    assert response.status_code == 200
    return [item.pk for item in response.context["results"]]


def test_rebuild_repairs_real_missing_and_stale_terms_twice(client):
    content = dict(
        title="Titlemarker",
        short_summary="Shortmarker",
        long_summary="Longmarker",
        notes="Notesmarker",
        transcript="Transcriptmarker",
    )
    public = Item.objects.create(original_url="https://example.test/public", **content)
    private = Item.objects.create(
        original_url="https://example.test/private", is_public=False, **content
    )
    before = item_rows()
    with connection.cursor() as cursor:
        # External-content FTS allows index-only mutations without touching Items.
        cursor.execute("INSERT INTO archive_item_fts(archive_item_fts) VALUES ('delete-all')")
        cursor.execute(
            "INSERT INTO archive_item_fts(rowid, title) VALUES (%s, %s)",
            [public.pk, "Obsoletemarker"],
        )
        cursor.execute(
            "INSERT INTO archive_item_fts(rowid, title) VALUES (%s, %s)",
            [private.pk, "Titlemarker Obsoleteprivate"],
        )
    assert public_results(client, "Titlemarker") == []
    assert public_results(client, "Obsoletemarker") == [public.pk]
    assert item_rows() == before

    for _ in range(2):
        stdout = StringIO()
        call_command("rebuild_search_index", stdout=stdout, no_color=True)
        assert stdout.getvalue() == "Rebuilt Archive search index for 2 items.\n"
        for term in content.values():
            assert public_results(client, term) == [public.pk]
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rowid FROM archive_item_fts WHERE archive_item_fts MATCH %s",
                    [term],
                )
                assert {row[0] for row in cursor.fetchall()} == {public.pk, private.pk}
        assert public_results(client, "Obsoletemarker") == []
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT rowid FROM archive_item_fts WHERE archive_item_fts MATCH %s",
                ["Obsoleteprivate"],
            )
            assert cursor.fetchall() == []
        assert item_rows() == before


def test_rebuild_refuses_unsupported_backend_without_sql(monkeypatch):
    Item.objects.create(original_url="https://example.test/item", notes="Preserved")
    before = item_rows()
    stdout = StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(connection, "vendor", "postgresql")
        cursor = Mock(side_effect=AssertionError("Unsupported backend must not open a cursor"))
        patch.setattr(connection, "cursor", cursor)
        with pytest.raises(CommandError, match="supports SQLite only"):
            call_command("rebuild_search_index", stdout=stdout)
        cursor.assert_not_called()
    assert stdout.getvalue() == ""
    assert item_rows() == before


def test_rebuild_refuses_missing_table_without_attempting_repair():
    Item.objects.create(original_url="https://example.test/item", notes="Preserved")
    before = item_rows()
    # pytest's transaction rolls this isolated schema change back after the test.
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE archive_item_fts")
    stdout = StringIO()
    statements = []

    def record(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record), pytest.raises(CommandError, match="Run migrations"):
        call_command("rebuild_search_index", stdout=stdout)
    assert statements
    assert not any("INSERT" in sql.upper() for sql in statements)
    assert stdout.getvalue() == ""
    assert item_rows() == before


def test_failed_rebuild_propagates_error_without_success_or_item_changes():
    Item.objects.create(original_url="https://example.test/item", transcript="Preserved transcript")
    before = item_rows()
    stdout = StringIO()
    attempted = []

    def fail_rebuild(execute, sql, params, many, context):
        if "INSERT INTO archive_item_fts" in sql:
            attempted.append(sql)
            raise OperationalError("synthetic rebuild failure")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_rebuild):
        with pytest.raises(OperationalError, match="synthetic rebuild failure"):
            call_command("rebuild_search_index", stdout=stdout)
    assert len(attempted) == 1
    assert stdout.getvalue() == ""
    assert item_rows() == before


@pytest.mark.parametrize("count,label", [(0, "items"), (1, "item"), (3, "items")])
def test_success_count_and_item_preservation(count, label):
    for index in range(count):
        Item.objects.create(original_url=f"https://example.test/{index}", title="Synthetic title")
    before = item_rows()
    stdout = StringIO()
    call_command("rebuild_search_index", stdout=stdout, no_color=True)
    assert stdout.getvalue() == f"Rebuilt Archive search index for {count} {label}.\n"
    assert item_rows() == before
