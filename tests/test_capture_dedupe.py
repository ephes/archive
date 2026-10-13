import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from archive.capture import normalize_capture_url
from archive.models import EnrichmentStatus, Item


@pytest.mark.parametrize(
    "url,expected",
    [
        ("HTTPS://Example.COM:443/Path?a=1#frag", "https://example.com/Path?a=1"),
        ("http://Example.COM:80/x", "http://example.com/x"),
        ("https://USER:Pass@Example.COM:8080/x", "https://USER:Pass@example.com:8080/x"),
        ("http://[ABCD::1]:80/x", "http://[abcd::1]/x"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/x?", "https://example.com/x?"),
        (
            "https://example.com/x?a=%FF&&utm_source=x&b=+",
            "https://example.com/x?a=%FF&&utm_source=x&b=+",
        ),
    ],
)
def test_url_normalization(url, expected):
    assert normalize_capture_url(url) == expected


def share(client, channel, url):
    if channel == "api":
        response = client.post(
            "/api/items/",
            json.dumps({"url": url}),
            content_type="application/json",
            headers={"Authorization": "Bearer test-api-token"},
        )
        assert response.status_code in (200, 201)
        return response.json()["id"], response
    response = client.post(
        reverse("archive:item-new"),
        {
            "original_url": url,
            "kind": "article",
            "podcast_feed_policy": "auto",
            "is_public": "on",
        },
    )
    if response.status_code == 200:
        assert b"already captured" in response.content
        assert b"Your submitted changes were not saved" in response.content
        return response.context["duplicate_item"].pk, response
    assert response.status_code == 302
    return int(response.url.strip("/").split("/")[-1]), response


@pytest.mark.django_db
@pytest.mark.parametrize(
    "first_channel,second_channel",
    [
        ("api", "api"),
        ("web", "web"),
        ("api", "web"),
        ("web", "api"),
    ],
)
def test_repeat_leaves_completed_enrichment_untouched(
    client,
    editor_user,
    monkeypatch,
    first_channel,
    second_channel,
):
    client.force_login(editor_user)
    pk, _ = share(client, first_channel, "https://example.com/story?q=1")
    Item.objects.filter(pk=pk).update(
        summary_status=EnrichmentStatus.COMPLETE,
        short_summary="Paid summary",
        article_audio_status=EnrichmentStatus.COMPLETE,
        article_audio_job_id="paid-job",
    )
    before = Item.objects.values().get(pk=pk)

    def no_enrichment(*args, **kwargs):
        pytest.fail("duplicate capture must not prepare or start enrichment")

    monkeypatch.setattr("archive.capture.prepare_item_for_enrichment", no_enrichment)
    again, response = share(client, second_channel, "HTTPS://EXAMPLE.COM:443/story?q=1#x")
    assert again == pk
    assert Item.objects.count() == 1
    assert Item.objects.values().get(pk=pk) == before
    if second_channel == "api":
        assert response.status_code == 200
        assert response.json()["duplicate"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("channel", ["api", "web"])
@pytest.mark.parametrize(
    "seconds,age,created",
    [(86400, 86390, False), (86400, 86410, True), (60, 61, True), (0, 1, True)],
)
def test_window(client, editor_user, settings, channel, seconds, age, created):
    settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS = seconds
    client.force_login(editor_user)
    old = Item.objects.create(
        original_url="https://example.com/story", shared_at=timezone.now() - timedelta(seconds=age)
    )
    pk, response = share(client, channel, old.original_url)
    assert (pk != old.pk) is created
    assert Item.objects.count() == (2 if created else 1)
    if created:
        assert Item.objects.get(pk=pk).summary_status == EnrichmentStatus.PENDING
    if channel == "api":
        assert response.status_code == (201 if created else 200)
        assert response.json()["duplicate"] is not created


@pytest.mark.django_db
@pytest.mark.parametrize(
    "different",
    [
        "https://example.com/Story?a=1",
        "http://example.com/story?a=1",
        "https://example.com/story?a=2",
        "https://example.com/story?a=1&utm_source=x",
        "https://example.com/story?a=%31",
        "https://example.com/story?a=1&",
        "https://example.com:8443/story?a=1",
    ],
)
def test_distinct_urls_create_items(client, different):
    first, _ = share(client, "api", "https://example.com/story?a=1")
    second, _ = share(client, "api", different)
    assert first != second


def test_concurrent_api_and_form_on_real_sqlite_file(tmp_path):
    # A separate process avoids pytest-django's in-memory DB and test transactions.
    script = """
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import Client
from django.conf import settings
from archive.models import Item
call_command("migrate", verbosity=0)
assert settings.DATABASES["default"]["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
user = get_user_model().objects.create_user(username="editor")
web = Client()
web.force_login(user)
connections.close_all()
lookup = Event()
second_begin = Event()

def run(first):
    db = connections["default"]
    db.ensure_connection()
    identity = id(db.connection)
    def observe(execute, sql, params, many, context):
        if first and "SELECT" in sql and 'FROM "archive_item"' in sql:
            lookup.set()
            assert second_begin.wait(5), "second capture never attempted BEGIN"
        elif not first and sql == "BEGIN IMMEDIATE":
            second_begin.set()
        return execute(sql, params, many, context)
    try:
        if not first:
            assert lookup.wait(5), "first capture never reached lookup"
        with db.execute_wrapper(observe):
            if first:
                response = Client().post("/api/items/", json.dumps({"url": "https://example.com/x"}),
                    content_type="application/json",
                    headers={"Authorization": "Bearer test-api-token"})
                assert response.status_code == 201
                pk = response.json()["id"]
            else:
                response = web.post("/items/new/", {
                    "original_url": "https://example.com/x#frag", "kind": "article",
                    "podcast_feed_policy": "auto", "is_public": "on"})
                assert response.status_code == 200, response.status_code
                assert b"already captured as item #" in response.content
                pk = Item.objects.get().pk
        return pk, identity
    finally:
        db.close()
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(run, [True, False]))
assert results[0][1] != results[1][1], "must use separate connections"
assert results[0][0] == results[1][0], results
assert Item.objects.count() == 1
connections.close_all()
"""
    env = os.environ.copy()
    env.update(
        DJANGO_SETTINGS_MODULE="config.settings.test",
        DJANGO_DB_PATH=str(tmp_path / "capture.sqlite3"),
        ARCHIVE_CAPTURE_DEDUPE_SECONDS="86400",
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.django_db
def test_newest_matching_existing_item_wins(client):
    now = timezone.now()
    Item.objects.create(original_url="https://example.com/x", shared_at=now - timedelta(hours=1))
    Item.objects.create(original_url="https://example.com/x#old", shared_at=now)
    newest = Item.objects.create(original_url="https://EXAMPLE.com:443/x#new", shared_at=now)
    pk, response = share(client, "api", "https://example.com/x")
    assert pk == newest.pk
    assert response.status_code == 200
    assert Item.objects.count() == 3


@pytest.mark.django_db
def test_private_duplicate_form_shows_notice_without_broken_public_link(client, editor_user):
    existing = Item.objects.create(original_url="https://example.com/x", is_public=False)
    client.force_login(editor_user)
    pk, response = share(client, "web", existing.original_url)
    assert pk == existing.pk
    assert response.status_code == 200
    assert b"The existing item is private" in response.content
    assert b"View existing item" not in response.content
    assert response.context["form"]["original_url"].value() == existing.original_url
    assert Item.objects.count() == 1
    existing.refresh_from_db()
    assert existing.is_public is False
