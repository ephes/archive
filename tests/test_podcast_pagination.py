from datetime import timedelta
from xml.etree import ElementTree as ET

import pytest
from django.db.models.query import QuerySet
from django.urls import reverse
from django.utils import timezone

from archive.classification import podcast_feed_decision_for_item
from archive.models import Item, ItemKind, PodcastFeedPolicy


@pytest.fixture
def podcast_corpus(db):
    now = timezone.now()
    substantial = "A connected narrative about a synthetic topic. " * 12
    variants = [
        {},
        {"archived_audio_path": "", "kind": ItemKind.ARTICLE, "long_summary": substantial},
        {
            "archived_audio_path": "",
            "kind": ItemKind.ARTICLE,
            "podcast_feed_policy": PodcastFeedPolicy.INCLUDE,
        },
        {"podcast_feed_policy": PodcastFeedPolicy.EXCLUDE},
        {"short_summary": "\t\u2003\n"},
        {"archived_audio_path": "   ", "kind": ItemKind.VIDEO},
        {"archived_audio_path": "\t ", "kind": ItemKind.ARTICLE, "long_summary": substantial},
        {"title": "\t\u2003\n"},
        {"is_public": False},
        {"published_at": None},
        {
            "archived_audio_path": "",
            "kind": ItemKind.ARTICLE,
            "long_summary": substantial + " https://example.com/a https://example.com/b",
        },
        {
            "archived_audio_path": "",
            "article_audio_artifact_path": "",
            "podcast_feed_policy": PodcastFeedPolicy.INCLUDE,
        },
        {"kind": ItemKind.ARTICLE},
    ]
    items = []
    for index in range(260):
        fields = {
            "original_url": f"https://example.com/feed/{index}",
            "title": f"Synthetic item {index}",
            "short_summary": "Synthetic summary.",
            "long_summary": "Brief body.",
            "notes": "Unused notes " * 1000,
            "transcript": "Unused transcript " * 1000,
            "archived_audio_path": "items/synthetic.mp3",
            "article_audio_artifact_path": "synthetic/generated.mp3",
            "kind": ItemKind.PODCAST_EPISODE,
            "shared_at": now - timedelta(minutes=index // 3),
        }
        variant = variants[index % len(variants)]
        fields.update(variant)
        item = Item.objects.create(**fields)
        if "published_at" in variant:
            # Public save fills this in; model a legacy unpublished row explicitly.
            Item.objects.filter(pk=item.pk).update(published_at=None)
            item.published_at = None
        items.append(item)
    ordered = sorted(items, key=lambda item: (item.shared_at, item.pk), reverse=True)
    return [item for item in ordered if podcast_feed_decision_for_item(item).eligible]


@pytest.mark.django_db
@pytest.mark.parametrize("page", [1, 2, 3])
def test_podcast_pages_preserve_policy_and_bound_materialization(
    client,
    monkeypatch,
    podcast_corpus,
    page,
):
    assert len(podcast_corpus) == 100
    # Add one more eligible item so page three exercises a short final page.
    extra = Item.objects.create(
        original_url="https://example.com/feed/extra",
        title="Extra synthetic episode",
        short_summary="Extra summary.",
        archived_audio_path="items/extra.mp3",
        shared_at=timezone.now() - timedelta(days=1),
    )
    expected = [*podcast_corpus, extra]
    full_loads = []
    batch_sizes = []
    original_from_db = Item.from_db
    original_iterator = QuerySet.iterator

    def from_db(cls, db, field_names, values):
        if "transcript" in field_names:
            full_loads.append(True)
        return original_from_db(db, field_names, values)

    def iterator(queryset, chunk_size=None):
        if queryset.model is Item:
            batch_sizes.append(chunk_size)
        return original_iterator(queryset, chunk_size=chunk_size)

    monkeypatch.setattr(Item, "from_db", classmethod(from_db))
    monkeypatch.setattr(QuerySet, "iterator", iterator)
    url = (
        reverse("archive:podcast-feed")
        if page == 1
        else reverse("archive:podcast-feed-page", kwargs={"page": page})
    )
    response = client.get(url)
    assert response.status_code == 200
    channel = ET.fromstring(response.content).find("channel")
    entries = channel.findall("item")
    selected = expected[(page - 1) * 50 : page * 50]
    assert [entry.findtext("title") for entry in entries] == [item.title for item in selected]
    links = {
        link.attrib["rel"]: link.attrib["href"]
        for link in channel.findall("{http://www.w3.org/2005/Atom}link")
    }
    assert ("previous" in links) == (page > 1)
    assert ("next" in links) == (page < 3)
    assert len(full_loads) == len(selected)
    assert batch_sizes and all(size is not None and 0 < size <= 100 for size in batch_sizes)


@pytest.mark.django_db
@pytest.mark.parametrize("page", [0, 3, 4])
def test_podcast_invalid_pages_stay_404(client, podcast_corpus, page):
    response = client.get(reverse("archive:podcast-feed-page", kwargs={"page": page}))
    assert response.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    "edit",
    [
        {"is_public": False},
        {"published_at": None},
        {"podcast_feed_policy": PodcastFeedPolicy.EXCLUDE},
        {"short_summary": "\t\u2003"},
        {"long_summary": "Now brief."},
    ],
)
def test_podcast_rechecks_selected_items_after_policy_scan(client, monkeypatch, edit):
    changed = Item.objects.create(
        original_url="https://example.com/changed",
        title="Changed item",
        kind=ItemKind.ARTICLE,
        short_summary="Synthetic summary.",
        long_summary="A connected narrative about a synthetic topic. " * 12,
        article_audio_artifact_path="synthetic/generated.mp3",
    )
    kept = Item.objects.create(
        original_url="https://example.com/kept",
        title="Unchanged item",
        short_summary="Synthetic summary.",
        archived_audio_path="items/kept.mp3",
    )
    original_iterator = QuerySet.iterator
    edits = []

    def iterator(queryset, chunk_size=None):
        yield from original_iterator(queryset, chunk_size=chunk_size)
        # Simulate an operator edit between the policy scan and page hydration.
        edits.append(Item.objects.filter(pk=changed.pk).update(**edit))

    monkeypatch.setattr(QuerySet, "iterator", iterator)
    response = client.get(reverse("archive:podcast-feed"))
    assert response.status_code == 200
    channel = ET.fromstring(response.content).find("channel")
    assert [entry.findtext("title") for entry in channel.findall("item")] == [kept.title]
    assert edits == [1]
