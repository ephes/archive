from datetime import timedelta
from xml.etree import ElementTree as ET

import pytest
from django.urls import reverse
from django.utils import timezone

from archive.models import Item

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def parse_channel(content: bytes) -> ET.Element:
    root = ET.fromstring(content)
    channel = root.find("channel")
    assert channel is not None
    return channel


@pytest.mark.django_db
def test_rss_feed_includes_only_eligible_public_items_and_uses_detail_urls(client) -> None:
    now = timezone.now()
    summary_item = Item.objects.create(
        original_url="https://example.com/summary",
        title="Summary item",
        short_summary="Use this summary",
        notes="Do not use these notes",
        shared_at=now,
    )
    Item.objects.create(
        original_url="https://example.com/notes",
        title="Notes item",
        notes="Use these notes",
        shared_at=now - timedelta(minutes=1),
    )
    Item.objects.create(
        original_url="https://example.com/source",
        title="Source item",
        source="Deutschlandfunk",
        shared_at=now - timedelta(minutes=2),
    )
    fallback_item = Item.objects.create(
        original_url="https://example.com/fallback",
        title="Fallback item",
        shared_at=now - timedelta(minutes=3),
    )
    Item.objects.create(
        original_url="https://example.com/private",
        title="Private item",
        is_public=False,
        shared_at=now - timedelta(minutes=4),
    )
    Item.objects.create(
        original_url="https://example.com/untitled",
        title="",
        notes="Public but not feed eligible",
        shared_at=now - timedelta(minutes=5),
    )

    response = client.get(reverse("archive:rss-feed"))

    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/rss+xml")

    channel = parse_channel(response.content)
    items = channel.findall("item")

    assert [item.findtext("title") for item in items] == [
        "Summary item",
        "Notes item",
        "Source item",
        "Fallback item",
    ]
    assert items[0].findtext("link") == f"http://testserver{summary_item.get_absolute_url()}"
    assert items[0].findtext("guid") == f"http://testserver{summary_item.get_absolute_url()}"
    assert items[0].findtext("description") == "Use this summary"
    assert items[1].findtext("description") == "Use these notes"
    assert items[2].findtext("description") == "Archived from Deutschlandfunk."
    assert items[3].findtext("description") == f"Archived link: {fallback_item.original_url}"

    atom_links = channel.findall("atom:link", ATOM_NS)
    assert {(link.attrib["rel"], link.attrib["href"]) for link in atom_links} == {
        ("self", "http://testserver/feeds/rss.xml"),
    }


@pytest.mark.django_db
def test_rss_feed_excludes_whitespace_only_titles(client) -> None:
    Item.objects.create(
        original_url="https://example.com/whitespace",
        title="   ",
    )

    response = client.get(reverse("archive:rss-feed"))

    assert response.status_code == 200
    channel = parse_channel(response.content)
    assert channel.findall("item") == []


@pytest.mark.django_db
def test_rss_feed_archive_uses_fixed_size_item_windows(client) -> None:
    now = timezone.now()
    for index in range(55):
        Item.objects.create(
            original_url=f"https://example.com/items/{index}",
            title=f"Item {index}",
            shared_at=now - timedelta(minutes=index),
        )

    response = client.get(reverse("archive:rss-feed"))
    archive_response = client.get(reverse("archive:rss-feed-page", kwargs={"page": 2}))

    assert response.status_code == 200
    assert archive_response.status_code == 200

    channel = parse_channel(response.content)
    archive_channel = parse_channel(archive_response.content)
    main_items = channel.findall("item")
    archive_items = archive_channel.findall("item")

    assert len(main_items) == 50
    assert len(archive_items) == 5
    assert main_items[0].findtext("title") == "Item 0"
    assert main_items[-1].findtext("title") == "Item 49"
    assert archive_items[0].findtext("title") == "Item 50"
    assert archive_items[-1].findtext("title") == "Item 54"

    main_links = {
        (link.attrib["rel"], link.attrib["href"]) for link in channel.findall("atom:link", ATOM_NS)
    }
    archive_links = {
        (link.attrib["rel"], link.attrib["href"])
        for link in archive_channel.findall("atom:link", ATOM_NS)
    }

    assert ("self", "http://testserver/feeds/rss.xml") in main_links
    assert ("next", "http://testserver/feeds/rss/page/2.xml") in main_links
    assert ("self", "http://testserver/feeds/rss/page/2.xml") in archive_links
    assert ("previous", "http://testserver/feeds/rss.xml") in archive_links


@pytest.mark.django_db
def test_rss_feed_page_1_redirects_to_canonical_feed_url(client) -> None:
    response = client.get(reverse("archive:rss-feed-page", kwargs={"page": 1}))

    assert response.status_code == 301
    assert response["Location"] == reverse("archive:rss-feed")


@pytest.mark.django_db
def test_empty_rss_feed_returns_empty_channel(client) -> None:
    response = client.get(reverse("archive:rss-feed"))

    assert response.status_code == 200
    channel = parse_channel(response.content)
    assert channel.findall("item") == []
    atom_links = channel.findall("atom:link", ATOM_NS)
    assert {(link.attrib["rel"], link.attrib["href"]) for link in atom_links} == {
        ("self", "http://testserver/feeds/rss.xml"),
    }


@pytest.mark.django_db
@pytest.mark.parametrize("page", [0, 999])
def test_rss_feed_returns_404_for_out_of_range_pages(client, page: int) -> None:
    Item.objects.create(
        original_url="https://example.com/item",
        title="Feed item",
    )

    response = client.get(reverse("archive:rss-feed-page", kwargs={"page": page}))

    assert response.status_code == 404


@pytest.mark.django_db
def test_public_pages_expose_rss_autodiscovery(client) -> None:
    response = client.get(reverse("archive:overview"))

    assert response.status_code == 200
    assert (
        b'<link rel="alternate" type="application/rss+xml" title="Archive RSS feed"'
        in response.content
    )
    assert b'href="/feeds/rss.xml"' in response.content


@pytest.mark.django_db
def test_podcast_feed_includes_only_items_with_local_archived_audio_and_summary(client) -> None:
    archived_item = Item.objects.create(
        original_url="https://example.com/episode-1",
        title="Archived episode",
        short_summary="Podcast summary",
        kind="podcast_episode",
        archived_audio_path="items/1/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=4096,
    )
    video_item = Item.objects.create(
        original_url="https://example.com/video.mp4",
        title="Archived video",
        short_summary="Video summary",
        kind="video",
        archived_audio_path="items/2/audio/extracted.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=5120,
        archived_video_path="items/2/video/source.mp4",
        archived_video_content_type="video/mp4",
        archived_video_size_bytes=8192,
    )
    Item.objects.create(
        original_url="https://example.com/no-summary",
        title="Missing summary",
        kind="podcast_episode",
        archived_audio_path="items/3/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=4096,
    )
    Item.objects.create(
        original_url="https://example.com/no-audio",
        title="Missing audio",
        short_summary="Looks eligible otherwise",
        kind="podcast_episode",
    )
    Item.objects.create(
        original_url="https://example.com/private",
        title="Private archived episode",
        short_summary="Podcast summary",
        kind="podcast_episode",
        is_public=False,
        archived_audio_path="items/5/audio/source.mp3",
        archived_audio_content_type="audio/mpeg",
        archived_audio_size_bytes=4096,
    )

    response = client.get(reverse("archive:podcast-feed"))

    assert response.status_code == 200
    channel = parse_channel(response.content)
    items = channel.findall("item")
    assert [item.findtext("title") for item in items] == ["Archived video", "Archived episode"]
    first_enclosure = items[0].find("enclosure")
    assert first_enclosure is not None
    video_audio_url = reverse("archive:item-archived-audio", kwargs={"pk": video_item.pk})
    assert first_enclosure.attrib == {
        "url": f"http://testserver{video_audio_url}",
        "length": "5120",
        "type": "audio/mpeg",
    }
    assert items[0].findtext("description") == "Video summary"
    enclosure = items[1].find("enclosure")
    assert enclosure is not None
    archived_audio_url = reverse("archive:item-archived-audio", kwargs={"pk": archived_item.pk})
    assert enclosure.attrib == {
        "url": f"http://testserver{archived_audio_url}",
        "length": "4096",
        "type": "audio/mpeg",
    }
    assert items[1].findtext("description") == "Podcast summary"


@pytest.mark.django_db
def test_podcast_feed_archive_uses_fixed_size_item_windows(client) -> None:
    now = timezone.now()
    for index in range(55):
        Item.objects.create(
            original_url=f"https://example.com/podcast/{index}",
            title=f"Podcast {index}",
            short_summary=f"Summary {index}",
            kind="podcast_episode",
            shared_at=now - timedelta(minutes=index),
            archived_audio_path=f"items/{index}/audio/source.mp3",
            archived_audio_content_type="audio/mpeg",
            archived_audio_size_bytes=1024 + index,
        )

    response = client.get(reverse("archive:podcast-feed"))
    archive_response = client.get(reverse("archive:podcast-feed-page", kwargs={"page": 2}))

    assert response.status_code == 200
    assert archive_response.status_code == 200

    channel = parse_channel(response.content)
    archive_channel = parse_channel(archive_response.content)
    main_items = channel.findall("item")
    archive_items = archive_channel.findall("item")

    assert len(main_items) == 50
    assert len(archive_items) == 5
    assert main_items[0].findtext("title") == "Podcast 0"
    assert archive_items[0].findtext("title") == "Podcast 50"

    main_links = {
        (link.attrib["rel"], link.attrib["href"]) for link in channel.findall("atom:link", ATOM_NS)
    }
    archive_links = {
        (link.attrib["rel"], link.attrib["href"])
        for link in archive_channel.findall("atom:link", ATOM_NS)
    }

    assert ("self", "http://testserver/feeds/podcast.xml") in main_links
    assert ("next", "http://testserver/feeds/podcast/page/2.xml") in main_links
    assert ("self", "http://testserver/feeds/podcast/page/2.xml") in archive_links
    assert ("previous", "http://testserver/feeds/podcast.xml") in archive_links


@pytest.mark.django_db
def test_podcast_feed_page_1_redirects_to_canonical_feed_url(client) -> None:
    response = client.get(reverse("archive:podcast-feed-page", kwargs={"page": 1}))

    assert response.status_code == 301
    assert response["Location"] == reverse("archive:podcast-feed")
