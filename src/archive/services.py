from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone

from archive.metadata import AUDIO_SUFFIXES, extract_metadata_from_url
from archive.models import EnrichmentStatus, Item, ItemKind

logger = logging.getLogger(__name__)
VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}


def infer_kind(url: str, explicit_kind: str = "", audio_url: str = "") -> str:
    if explicit_kind in ItemKind.values:
        return explicit_kind
    if audio_url:
        return "podcast_episode"

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if host in VIDEO_HOSTS:
        return "video"
    if path.endswith(AUDIO_SUFFIXES):
        return "podcast_episode"
    return "link"


@dataclass(frozen=True)
class WeekPage:
    token: str
    year: int
    week: int
    starts_on: date
    ends_on: date

    @property
    def label(self) -> str:
        return f"Week {self.week}, {self.year}"


def to_week_page(value: datetime) -> WeekPage:
    local_value = timezone.localtime(value)
    iso_year, iso_week, _ = local_value.isocalendar()
    starts_on = date.fromisocalendar(iso_year, iso_week, 1)
    ends_on = date.fromisocalendar(iso_year, iso_week, 7)
    return WeekPage(
        token=f"{iso_year}-W{iso_week:02d}",
        year=iso_year,
        week=iso_week,
        starts_on=starts_on,
        ends_on=ends_on,
    )


def week_bounds(page: WeekPage) -> tuple[datetime, datetime]:
    tz = timezone.get_current_timezone()
    starts_at = timezone.make_aware(datetime.combine(page.starts_on, time.min), tz)
    ends_at = timezone.make_aware(datetime.combine(page.ends_on + timedelta(days=1), time.min), tz)
    return starts_at, ends_at


def prepare_item_for_enrichment(item: Item) -> Item:
    if _has_full_metadata(item):
        item.enrichment_status = EnrichmentStatus.COMPLETE
        item.enrichment_error = ""
    else:
        item.enrichment_status = EnrichmentStatus.PENDING
        item.enrichment_error = ""
    return item


def enrich_item_metadata(item: Item, timeout: int = 15) -> bool:
    try:
        metadata = extract_metadata_from_url(item.original_url, timeout=timeout)
    except Exception as exc:
        logger.exception("Metadata extraction failed for item %s", item.pk)
        return _mark_enrichment_failure(item, str(exc))

    update_fields: list[str] = []
    if not item.title and metadata.title:
        item.title = metadata.title
        update_fields.append("title")
    if not item.source and metadata.source:
        item.source = metadata.source
        update_fields.append("source")
    if not item.author and metadata.author:
        item.author = metadata.author
        update_fields.append("author")
    if item.original_published_at is None and metadata.original_published_at is not None:
        item.original_published_at = metadata.original_published_at
        update_fields.append("original_published_at")
    if not item.media_url and metadata.media_url:
        item.media_url = metadata.media_url
        update_fields.append("media_url")
    if not item.audio_url and metadata.audio_url:
        item.audio_url = metadata.audio_url
        update_fields.append("audio_url")

    inferred_kind = _infer_kind_from_metadata(item, metadata.kind_hint)
    if inferred_kind != item.kind:
        item.kind = inferred_kind
        update_fields.append("kind")

    item.enrichment_status = EnrichmentStatus.COMPLETE
    item.enrichment_error = ""
    update_fields.extend(["enrichment_status", "enrichment_error"])
    item.save(update_fields=sorted(set(update_fields)))
    return True


def recover_processing_items() -> int:
    return Item.objects.filter(enrichment_status=EnrichmentStatus.PROCESSING).update(
        enrichment_status=EnrichmentStatus.PENDING,
    )


def claim_pending_item() -> Item | None:
    with transaction.atomic():
        item = (
            Item.objects.select_for_update()
            .filter(enrichment_status=EnrichmentStatus.PENDING)
            .order_by("shared_at", "id")
            .first()
        )
        if item is None:
            return None
        item.enrichment_status = EnrichmentStatus.PROCESSING
        item.enrichment_error = ""
        item.save(update_fields=["enrichment_status", "enrichment_error"])
    return item


def enrich_pending_items(limit: int = 1, timeout: int = 15) -> int:
    processed = 0
    for _ in range(limit):
        item = claim_pending_item()
        if item is None:
            break
        enrich_item_metadata(item=item, timeout=timeout)
        processed += 1
    return processed


def _has_full_metadata(item: Item) -> bool:
    return all(
        (
            item.has_required_feed_metadata,
            item.source.strip(),
            item.author.strip(),
            item.original_published_at is not None,
            item.media_url.strip() or item.audio_url.strip(),
        )
    )


def _mark_enrichment_failure(item: Item, error_message: str) -> bool:
    item.enrichment_error = error_message
    if item.has_required_feed_metadata:
        item.enrichment_status = EnrichmentStatus.COMPLETE
    else:
        item.enrichment_status = EnrichmentStatus.FAILED
    item.save(update_fields=["enrichment_status", "enrichment_error"])
    return False


def _infer_kind_from_metadata(item: Item, kind_hint: str) -> str:
    known_item_kinds = {
        ItemKind.PODCAST_EPISODE,
        ItemKind.VIDEO,
        ItemKind.ARTICLE,
        ItemKind.SOCIAL_POST,
        ItemKind.LINK,
    }

    if item.kind != ItemKind.LINK:
        return item.kind
    if item.audio_url:
        return str(ItemKind.PODCAST_EPISODE)
    if kind_hint in {str(choice) for choice in known_item_kinds}:
        return kind_hint
    if item.media_url and item.media_url != item.original_url:
        media_kind = infer_kind(url=item.media_url, explicit_kind="", audio_url=item.audio_url)
        if media_kind != ItemKind.LINK:
            return media_kind
    return infer_kind(url=item.original_url, explicit_kind="", audio_url=item.audio_url)
