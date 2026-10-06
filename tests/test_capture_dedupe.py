from __future__ import annotations

import importlib
import json
from datetime import timedelta

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from archive.capture import (
    capture_item,
    capture_key_for_url,
    normalize_capture_url,
)
from archive.models import EnrichmentStatus, Item

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, settings):
    """Fail loudly if capture ever reaches a paid or remote API."""

    def _blocked(*args, **kwargs):
        raise AssertionError("network access is not allowed in capture tests")

    for module in ("summaries", "transcriptions", "article_audio", "metadata"):
        monkeypatch.setattr(f"archive.{module}.urlopen", _blocked)
    settings.ARCHIVE_API_TOKEN = TOKEN
    settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS = 24 * 60 * 60


def _post(client, api_url: str, payload: dict, **headers):
    return client.post(
        api_url,
        data=json.dumps(payload),
        content_type="application/json",
        headers={"Authorization": f"Bearer {TOKEN}", **headers},
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Example.COM/Path?a=1#frag", "https://example.com/Path?a=1"),
        (
            "https://example.com/p?utm_source=x&b=2&UTM_Medium=y&fbclid=z",
            "https://example.com/p?b=2",
        ),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("http://example.com:8080/x", "http://example.com:8080/x"),
        ("https://example.com/x?b=2&a=1", "https://example.com/x?b=2&a=1"),
        ("https://example.com/x?a=%FF&utm_source=1", "https://example.com/x?a=%FF"),
        ("https://example.com/x?a=%2B&b=+", "https://example.com/x?a=%2B&b=+"),
    ],
)
def test_normalize_capture_url(url: str, expected: str) -> None:
    assert normalize_capture_url(url) == expected


def test_migration_normalizer_matches_runtime() -> None:
    migration = importlib.import_module("archive.migrations.0014_item_capture_dedupe")
    for url in (
        "HTTPS://Example.COM/Path?a=1&utm_campaign=x#frag",
        "http://user:pw@[::1]:8000/x?gclid=1&q=",
        "https://example.com/x?a=%FF&a=%FE&utm%5Fsource=1",
        "https://example.com/x?a=1&&b=2&utm_x=&",
        "https://example.com",
    ):
        assert migration._normalize_capture_url(url) == normalize_capture_url(url)


@pytest.mark.django_db
def test_repeated_share_returns_existing_item(client, api_url: str) -> None:
    first = _post(client, api_url, {"url": "https://example.com/story"})
    second = _post(client, api_url, {"url": "https://example.com/story"})

    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    body = second.json()
    assert body["duplicate"] is True
    assert body["id"] == first.json()["id"]
    assert body["detail_url"] == first.json()["detail_url"]
    assert Item.objects.count() == 1
    # Only one item is queued for the paid enrichment steps.
    assert Item.objects.filter(summary_status=EnrichmentStatus.PENDING).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "variant",
    [
        "https://example.com/story#comments",
        "https://example.com/story?utm_source=mastodon&utm_medium=share",
        "HTTPS://EXAMPLE.com/story",
    ],
)
def test_url_variants_dedupe(client, api_url: str, variant: str) -> None:
    first = _post(client, api_url, {"url": "https://example.com/story"})
    second = _post(client, api_url, {"url": variant})

    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert Item.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("first_url", "second_url"),
    [
        ("https://example.com/watch?v=one", "https://example.com/watch?v=two"),
        ("https://example.com/x?a=%FF", "https://example.com/x?a=%FE"),
        ("https://example.com/Story", "https://example.com/story"),
        ("https://example.com/x?a=1&&b=2", "https://example.com/x?a=1&b=2"),
    ],
)
def test_different_url_is_not_a_duplicate(
    client, api_url: str, first_url: str, second_url: str
) -> None:
    _post(client, api_url, {"url": first_url})
    second = _post(client, api_url, {"url": second_url})

    assert second.status_code == 201
    assert Item.objects.count() == 2


@pytest.mark.django_db
def test_share_after_window_creates_new_item(client, api_url: str) -> None:
    first = _post(client, api_url, {"url": "https://example.com/story"})
    Item.objects.filter(pk=first.json()["id"]).update(
        shared_at=timezone.now() - timedelta(hours=25)
    )

    second = _post(client, api_url, {"url": "https://example.com/story"})
    third = _post(client, api_url, {"url": "https://example.com/story"})

    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    # The newest capture is the one later shares are compared against.
    assert third.status_code == 200
    assert third.json()["id"] == second.json()["id"]
    assert Item.objects.count() == 2
    old = Item.objects.get(pk=first.json()["id"])
    assert old.capture_key is None


@pytest.mark.django_db
def test_zero_window_disables_url_dedupe(client, api_url: str, settings) -> None:
    settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS = 0

    first = _post(client, api_url, {"url": "https://example.com/story"})
    second = _post(client, api_url, {"url": "https://example.com/story"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert Item.objects.count() == 2


@pytest.mark.django_db
def test_idempotency_key_is_honoured(client, api_url: str, settings) -> None:
    settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS = 0
    headers = {"Idempotency-Key": "share-123"}

    first = _post(client, api_url, {"url": "https://example.com/story"}, **headers)
    second = _post(client, api_url, {"url": "https://example.com/story#x"}, **headers)
    other_key = _post(
        client, api_url, {"url": "https://example.com/story"}, **{"Idempotency-Key": "share-456"}
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == {**first.json(), "duplicate": True}
    assert other_key.status_code == 201
    assert Item.objects.count() == 2


@pytest.mark.django_db
def test_idempotency_key_reused_for_other_url_conflicts(client, api_url: str) -> None:
    headers = {"Idempotency-Key": "share-123"}
    _post(client, api_url, {"url": "https://example.com/story"}, **headers)

    response = _post(client, api_url, {"url": "https://example.com/other"}, **headers)

    assert response.status_code == 409
    assert Item.objects.count() == 1


@pytest.mark.django_db
def test_too_long_idempotency_key_is_rejected(client, api_url: str) -> None:
    response = _post(
        client, api_url, {"url": "https://example.com/story"}, **{"Idempotency-Key": "k" * 256}
    )

    assert response.status_code == 400
    assert Item.objects.count() == 0


@pytest.mark.django_db
def test_capture_retries_after_losing_unique_race(monkeypatch) -> None:
    """Simulate a concurrent capture committing between our lookup and our insert."""
    import archive.capture as capture

    winner = Item.objects.create(
        original_url="https://example.com/race",
        capture_key=capture_key_for_url("https://example.com/race"),
    )
    real_once = capture._capture_once
    calls = {"n": 0}

    def racing_once(item, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Our lookup ran before the concurrent capture committed, so we insert blindly.
            item.capture_key = kwargs["key"]
            item._capture_managed = True
            item.save()  # violates the unique constraint
        return real_once(item, **kwargs)

    monkeypatch.setattr(capture, "_capture_once", racing_once)

    result = capture_item(Item(original_url="https://example.com/race"))

    assert calls["n"] == 2
    assert result.duplicate is True
    assert result.item.pk == winner.pk
    assert Item.objects.count() == 1


@pytest.mark.django_db
def test_form_warns_about_duplicate_and_allows_saving_on_purpose(
    client, editor_user, api_url: str
) -> None:
    first = _post(client, api_url, {"url": "https://example.com/story"})
    client.force_login(editor_user)
    form_data = {
        "original_url": "https://example.com/story#again",
        "kind": "link",
        "podcast_feed_policy": "auto",
        "is_public": "on",
    }

    warned = client.post(reverse("archive:item-new"), data=form_data)

    assert warned.status_code == 200
    assert b"already captured" in warned.content
    assert b'name="confirm_duplicate"' in warned.content
    assert Item.objects.count() == 1

    saved = client.post(reverse("archive:item-new"), data={**form_data, "confirm_duplicate": "1"})

    assert saved.status_code == 302
    assert Item.objects.count() == 2
    new_item = Item.objects.exclude(pk=first.json()["id"]).get()
    assert new_item.capture_key == capture_key_for_url("https://example.com/story")
    assert Item.objects.get(pk=first.json()["id"]).capture_key is None


def _form_data(url: str, **extra) -> dict:
    return {
        "original_url": url,
        "kind": "link",
        "podcast_feed_policy": "auto",
        "is_public": "on",
        **extra,
    }


@pytest.mark.django_db
def test_editing_duplicate_url_restores_previous_capture(client, editor_user, api_url: str) -> None:
    first_id = _post(client, api_url, {"url": "https://example.com/a"}).json()["id"]
    client.force_login(editor_user)
    client.post(
        reverse("archive:item-new"),
        data=_form_data("https://example.com/a", confirm_duplicate="1"),
    )
    duplicate = Item.objects.exclude(pk=first_id).get()

    from archive.forms import ItemForm

    form = ItemForm(instance=duplicate, data=_form_data("https://example.com/c"))
    assert form.is_valid(), form.errors
    form.save()

    again = _post(client, api_url, {"url": "https://example.com/a"})
    assert again.status_code == 200
    assert again.json()["id"] == first_id
    duplicate.refresh_from_db()
    assert duplicate.url_key == capture_key_for_url("https://example.com/c")
    assert duplicate.capture_key == capture_key_for_url("https://example.com/c")
    assert Item.objects.count() == 2


@pytest.mark.django_db
def test_deleting_newest_copy_falls_back_to_previous(client, editor_user, api_url: str) -> None:
    first_id = _post(client, api_url, {"url": "https://example.com/a"}).json()["id"]
    client.force_login(editor_user)
    client.post(
        reverse("archive:item-new"),
        data=_form_data("https://example.com/a", confirm_duplicate="1"),
    )
    Item.objects.exclude(pk=first_id).delete()

    again = _post(client, api_url, {"url": "https://example.com/a"})

    assert again.status_code == 200
    assert again.json()["id"] == first_id


@pytest.mark.django_db
def test_items_created_outside_capture_are_recognised(client, api_url: str) -> None:
    item = Item.objects.create(original_url="https://example.com/admin-added")

    response = _post(client, api_url, {"url": "https://example.com/admin-added#x"})

    assert response.status_code == 200
    assert response.json()["id"] == item.pk


@pytest.mark.django_db
def test_idempotency_key_is_remembered_when_url_dedupe_wins(client, api_url: str) -> None:
    first = _post(client, api_url, {"url": "https://example.com/story"})
    keyed = _post(
        client, api_url, {"url": "https://example.com/story"}, **{"Idempotency-Key": "k1"}
    )
    Item.objects.filter(pk=first.json()["id"]).update(shared_at=timezone.now() - timedelta(days=2))

    retry = _post(
        client, api_url, {"url": "https://example.com/story"}, **{"Idempotency-Key": "k1"}
    )
    reused = _post(
        client, api_url, {"url": "https://example.com/other"}, **{"Idempotency-Key": "k1"}
    )

    assert keyed.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]
    assert reused.status_code == 409
    assert Item.objects.count() == 1


@pytest.mark.django_db
def test_idempotency_key_matches_original_request_after_url_edit(client, api_url: str) -> None:
    created = _post(
        client, api_url, {"url": "https://example.com/story"}, **{"Idempotency-Key": "k1"}
    )
    item = Item.objects.get(pk=created.json()["id"])
    item.original_url = "https://example.com/corrected"
    item.save()

    retry = _post(
        client, api_url, {"url": "https://example.com/story"}, **{"Idempotency-Key": "k1"}
    )
    edited = _post(
        client, api_url, {"url": "https://example.com/corrected"}, **{"Idempotency-Key": "k1"}
    )

    assert retry.status_code == 200
    assert retry.json()["id"] == item.pk
    assert edited.status_code == 409


@pytest.mark.django_db
def test_search_triggers_survive_migration() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'archive_item'"
        )
        triggers = {row[0] for row in cursor.fetchall()}
    assert {"archive_item_ai", "archive_item_ad", "archive_item_au"} <= triggers


@pytest.mark.django_db(transaction=True)
def test_migration_backfill_keeps_duplicates_and_keys_newest() -> None:
    from django.db.migrations.executor import MigrationExecutor

    before = [("archive", "0013_alter_item_kind")]
    after = [("archive", "0014_item_capture_dedupe")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old_apps = executor.loader.project_state(before).apps
    OldItem = old_apps.get_model("archive", "Item")
    now = timezone.now()
    older = OldItem.objects.create(
        original_url="https://example.com/dup", shared_at=now - timedelta(days=2)
    )
    newer = OldItem.objects.create(
        original_url="https://EXAMPLE.com/dup?utm_source=x#y", shared_at=now - timedelta(days=1)
    )
    single = OldItem.objects.create(original_url="https://example.com/single", shared_at=now)

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(after)
    new_apps = executor.loader.project_state(after).apps
    NewItem = new_apps.get_model("archive", "Item")

    assert NewItem.objects.count() == 3
    assert NewItem.objects.get(pk=older.pk).capture_key is None
    assert NewItem.objects.get(pk=older.pk).url_key == capture_key_for_url(
        "https://example.com/dup"
    )
    assert NewItem.objects.get(pk=newer.pk).capture_key == capture_key_for_url(
        "https://example.com/dup"
    )
    assert NewItem.objects.get(pk=single.pk).capture_key == capture_key_for_url(
        "https://example.com/single"
    )

    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db
def test_form_without_confirmation_never_saves_a_racing_duplicate(
    client, editor_user, api_url: str, monkeypatch
) -> None:
    """A capture that commits after the form's warning check must still be deduplicated."""
    import archive.views as views

    first = _post(client, api_url, {"url": "https://example.com/story"})
    monkeypatch.setattr(views, "find_recent_capture", lambda url: None)
    client.force_login(editor_user)

    response = client.post(
        reverse("archive:item-new"),
        data={
            "original_url": "https://example.com/story",
            "kind": "link",
            "podcast_feed_policy": "auto",
            "is_public": "on",
        },
    )

    assert response.status_code == 200
    assert b"already captured" in response.content
    assert Item.objects.count() == 1
    assert Item.objects.get().pk == first.json()["id"]


@pytest.mark.django_db
def test_ordinary_saves_do_not_overwrite_capture_key(client, api_url: str) -> None:
    item = Item.objects.create(original_url="https://example.com/a")
    item.title = "Edited"
    item.save()

    again = _post(client, api_url, {"url": "https://example.com/a"})

    assert again.status_code == 200
    assert again.json()["id"] == item.pk


@pytest.mark.django_db
def test_resaving_after_url_edit_keeps_new_capture_key(client, api_url: str) -> None:
    item = Item.objects.get(
        pk=_post(client, api_url, {"url": "https://example.com/a"}).json()["id"]
    )
    item.original_url = "https://example.com/b"
    item.save()
    item.notes = "second save of the same instance"
    item.save()

    item.refresh_from_db()
    assert item.capture_key == capture_key_for_url("https://example.com/b")
    to_b = _post(client, api_url, {"url": "https://example.com/b"})
    to_a = _post(client, api_url, {"url": "https://example.com/a"})
    assert to_b.status_code == 200
    assert to_b.json()["id"] == item.pk
    assert to_a.status_code == 201
