"""Real feed responses with commits from a second SQLite WAL connection."""

import pytest
from test_historical_migrations import run_journey


@pytest.mark.parametrize(
    ("feed", "barrier"),
    [("rss", "selection"), ("podcast", "hydration"), ("rss", "render"), ("podcast", "render")],
)
def test_feed_count_selection_hydration_and_render_share_snapshot(tmp_path, feed, barrier):
    run_journey(tmp_path, r'''
import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory
from archive import views
from archive.models import Item

feed, barrier = FEED, BARRIER
call_command("migrate", verbosity=0)
with connection.cursor() as cursor:
    cursor.execute("PRAGMA journal_mode=WAL")
    assert cursor.fetchone()[0] == "wal"
now = datetime(2026, 11, 1, tzinfo=UTC)
rows = Item.objects.bulk_create([
    Item(original_url=f"https://example.test/{n}", title=f"Synthetic {n}",
         short_summary=f"Before summary {n}", published_at=now, shared_at=now,
         kind="article" if n % 2 == 0 else "podcast_episode",
         long_summary="A connected narrative about a synthetic topic. " * 12,
         archived_audio_path="" if n % 2 == 0 else "synthetic.mp3",
         article_audio_artifact_path="synthetic/generated.mp3")
    for n in range(51)
])
view = views.rss_feed if feed == "rss" else views.podcast_feed
request = RequestFactory().get(f"/feeds/{feed if feed == 'podcast' else 'rss'}.xml")
counts = []
original_feed = views._render_feed
def observe_feed(*args, **kwargs):
    response = original_feed(*args, **kwargs)
    counts.append(kwargs["page_obj"].paginator.count)
    return response
with patch.object(views, "_render_feed", observe_feed):
    before = view(request)
assert counts == [51]
assert b'rel="next"' in before.content

writer = sqlite3.connect(settings.DATABASES["default"]["NAME"], timeout=2)
changed = False
def mutate():
    global changed
    assert not changed
    changed = True
    with writer:
        writer.execute("UPDATE archive_item SET is_public=0 WHERE id=?", (rows[50].pk,))
        writer.execute("UPDATE archive_item SET title='' WHERE id=?", (rows[49].pk,))
        # The podcast refusal here is Python's generated-summary coherence policy.
        if feed == "podcast":
            writer.execute("UPDATE archive_item SET long_summary='Brief.' WHERE id=?",
                           (rows[48].pk,))
        else:
            writer.execute("UPDATE archive_item SET title='' WHERE id=?", (rows[48].pk,))
        writer.execute("UPDATE archive_item SET title='After edit', short_summary='After summary' "
                       "WHERE id=?", (rows[47].pk,))
        # A real concurrent publication; copy synthetic required columns only.
        columns = [field.column for field in Item._meta.concrete_fields if not field.primary_key]
        names = ", ".join('"' + column + '"' for column in columns)
        writer.execute(f"INSERT INTO archive_item ({names}) SELECT {names} "
                       "FROM archive_item WHERE id=?", (rows[0].pk,))
        writer.execute("UPDATE archive_item SET title='After publication' "
                       "WHERE id=last_insert_rowid()")
    assert not writer.in_transaction

original_render = views.render
rendered_in_snapshot = []
def render(*args, **kwargs):
    rendered_in_snapshot.append(connection.connection.in_transaction)
    if barrier == "render":
        mutate()
    return original_render(*args, **kwargs)

def interleave(execute, sql, params, many, context):
    if not changed and sql.startswith("SELECT") and '"archive_item"' in sql:
        if ((barrier == "selection" and "LIMIT 50" in sql)
                or (barrier == "hydration" and '"archive_item"."id" IN (' in sql)):
            mutate()
    return execute(sql, params, many, context)

with patch.object(views, "_render_feed", observe_feed), patch.object(views, "render", render), \
        connection.execute_wrapper(interleave):
    observed = view(request)
assert changed, "writer barrier never ran"
assert observed.status_code == 200
assert observed.content == before.content, "feed mixed committed database generations"
assert counts == [51, 51], counts
assert rendered_in_snapshot == [True], rendered_in_snapshot
assert connection.get_autocommit() and not connection.connection.in_transaction
with patch.object(views, "_render_feed", observe_feed):
    after = view(request)
assert counts[-1] == 49, counts
assert after.content != before.content
assert b'After publication' in after.content and b'After summary' in after.content
assert b'rel="next"' not in after.content

# Query and template failures both retire the transaction and allow a fresh read.
def broken_query(execute, sql, params, many, context):
    if sql.startswith("SELECT") and '"archive_item"' in sql and "COUNT(" not in sql:
        raise RuntimeError("synthetic query failure")
    return execute(sql, params, many, context)
for failure in ("query", "render"):
    try:
        if failure == "query":
            with connection.execute_wrapper(broken_query):
                view(request)
        else:
            with patch.object(views, "render", side_effect=RuntimeError("synthetic render")):
                view(request)
    except RuntimeError:
        pass
    else:
        raise AssertionError("synthetic failure did not execute")
    assert connection.get_autocommit() and not connection.connection.in_transaction
    assert view(request).content == after.content
writer.close()
connection.close()
'''.replace("FEED", repr(feed)).replace("BARRIER", repr(barrier)))
