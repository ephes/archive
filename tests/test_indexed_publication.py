import sqlite3

import pytest
from django.db import connection

from archive.models import EnrichmentStatus, Item

pytestmark = pytest.mark.django_db


@pytest.mark.django_db(transaction=True)
def test_status_only_update_does_no_fts_work():
    item = Item.objects.create(original_url="https://example.test/item", transcript="Retainedword")
    connection.commit()  # Flush pending FTS work from the preceding insert.
    raw = connection.connection
    before = raw.total_changes
    Item.objects.filter(pk=item.pk).update(summary_status=EnrichmentStatus.PROCESSING)
    # SQLite counts FTS shadow-table writes too. Only the Item row should change.
    assert raw.total_changes - before == 1


@pytest.mark.parametrize("field", ["title", "short_summary", "long_summary", "notes", "transcript"])
def test_fts_guard_indexes_content_and_null_boundaries(field):
    with connection.cursor() as cursor:
        cursor.execute("SELECT sql FROM sqlite_master WHERE name = 'archive_item_au'")
        update_sql = cursor.fetchone()[0]
    # A nullable synthetic schema also exercises legacy NULL transitions that the
    # current Item model cannot create. Run the installed production trigger verbatim.
    with sqlite3.connect(":memory:") as db:
        columns = ("title", "short_summary", "long_summary", "notes", "transcript")
        db.execute(
            "CREATE TABLE archive_item (id INTEGER PRIMARY KEY, "
            + ", ".join(columns)
            + ", status TEXT)"
        )
        db.execute(
            "CREATE VIRTUAL TABLE archive_item_fts USING fts5("
            + ", ".join(columns)
            + ", content='archive_item', content_rowid='id')"
        )
        db.execute(update_sql)
        db.execute("INSERT INTO archive_item(id) VALUES (1)")
        db.execute("INSERT INTO archive_item_fts(rowid) VALUES (1)")
        for old, new in [(None, "Oldword"), ("Oldword", "Newword"), ("Newword", None)]:
            before = db.total_changes
            db.execute(f"UPDATE archive_item SET {field}=? WHERE id=1", (new,))
            assert db.total_changes - before > 1
            for term in ("Oldword", "Newword"):
                matches = db.execute(
                    "SELECT rowid FROM archive_item_fts WHERE archive_item_fts MATCH ?", (term,)
                ).fetchall()
                assert matches == ([(1,)] if term == new else [])
        db.commit()  # Exclude deferred FTS work from the preceding text change.
        before = db.total_changes
        db.execute(f"UPDATE archive_item SET {field}=NULL, status='done' WHERE id=1")
        assert db.total_changes - before == 1


def test_narrow_publication_persists_derived_date_and_preserves_other_writes(client):
    item = Item.objects.create(
        original_url="https://example.test/publish",
        title="Publicationmarker",
        is_public=False,
        published_at=None,
    )
    Item.objects.filter(pk=item.pk).update(notes="Concurrent operator note")
    item.is_public = True
    item.save(update_fields=["is_public"])
    item.refresh_from_db()
    assert item.published_at == item.shared_at
    assert item.notes == "Concurrent operator note"
    response = client.get("/feeds/rss.xml")
    assert response.status_code == 200
    assert b"Publicationmarker" in response.content


def test_empty_partial_save_remains_a_noop(django_assert_num_queries):
    item = Item.objects.create(original_url="https://example.test/noop", is_public=False)
    item.is_public = True
    item.summary_status = EnrichmentStatus.PROCESSING
    with django_assert_num_queries(0):
        item.save(update_fields=[])
    item.refresh_from_db()
    assert not item.is_public
    assert item.published_at is None
    assert item.processing_started_at is None


@pytest.mark.django_db(transaction=True)
def test_guard_migration_reverses_and_reapplies():
    from django.db.migrations.executor import MigrationExecutor

    latest = MigrationExecutor(connection).loader.graph.leaf_nodes("archive")
    item = Item.objects.create(original_url="https://example.test/rollback", title="Originalword")
    try:
        MigrationExecutor(connection).migrate([("archive", "0018_processingfailureitem")])
        before = connection.connection.total_changes
        Item.objects.filter(pk=item.pk).update(summary_status=EnrichmentStatus.PROCESSING)
        assert connection.connection.total_changes - before > 1
    finally:
        MigrationExecutor(connection).migrate(latest)
    before = connection.connection.total_changes
    Item.objects.filter(pk=item.pk).update(summary_status=EnrichmentStatus.COMPLETE)
    assert connection.connection.total_changes - before == 1
    Item.objects.filter(pk=item.pk).update(title="Replacementword")
    with connection.cursor() as cursor:
        for term, expected in [("Originalword", []), ("Replacementword", [(item.pk,)])]:
            cursor.execute(
                "SELECT rowid FROM archive_item_fts WHERE archive_item_fts MATCH %s", [term]
            )
            assert cursor.fetchall() == expected
