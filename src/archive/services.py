from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone

from archive.metadata import AUDIO_SUFFIXES, extract_metadata_from_url
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.summaries import generate_item_summaries
from archive.transcriptions import can_transcribe_item, generate_item_transcript

logger = logging.getLogger(__name__)
VIDEO_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"}
SUMMARY_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
)
TRANSCRIBABLE_ITEM_KINDS = (ItemKind.PODCAST_EPISODE, ItemKind.VIDEO)


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

    if _has_generated_summaries(item):
        item.summary_status = EnrichmentStatus.COMPLETE
        item.summary_error = ""
        item.summary_retry_count = 0
        item.summary_retry_at = None
    else:
        item.summary_status = EnrichmentStatus.PENDING
        item.summary_error = ""
        item.summary_retry_count = 0
        item.summary_retry_at = None

    if item.has_transcript:
        item.transcript_status = EnrichmentStatus.COMPLETE
        item.transcript_error = ""
        item.transcript_generated = bool(item.transcript_generated)
    else:
        item.transcript_status = EnrichmentStatus.PENDING
        item.transcript_error = ""
    return item


def enrich_item_metadata(
    item: Item,
    timeout: int = 15,
    summary_timeout: int = 60,
    transcription_timeout: int = 300,
) -> bool:
    metadata_success = True
    transcript_success = True
    summary_success = True

    if item.enrichment_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        metadata_success = _enrich_item_metadata_fields(item=item, timeout=timeout)

    if item.transcript_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        transcript_success = enrich_item_transcript(item=item, timeout=transcription_timeout)

    if item.summary_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        summary_success = enrich_item_summaries(item=item, timeout=summary_timeout)

    return metadata_success and transcript_success and summary_success


def enrich_item_summaries(item: Item, timeout: int = 60) -> bool:
    if not _summary_generation_needed(item):
        if item.summary_status != EnrichmentStatus.COMPLETE or item.summary_error:
            item.summary_status = EnrichmentStatus.COMPLETE
            item.summary_error = ""
            item.summary_retry_count = 0
            item.summary_retry_at = None
            item.save(
                update_fields=[
                    "summary_status",
                    "summary_error",
                    "summary_retry_count",
                    "summary_retry_at",
                ]
            )
        return True

    try:
        generated = generate_item_summaries(item=item, timeout=timeout)
    except Exception as exc:
        logger.exception("Summary generation failed for item %s", item.pk)
        return _mark_summary_failure(item, str(exc))

    update_fields: list[str] = []
    if (not item.short_summary or item.short_summary_generated) and generated.short_summary:
        item.short_summary = generated.short_summary
        item.short_summary_generated = True
        update_fields.extend(["short_summary", "short_summary_generated"])
    if (not item.long_summary or item.long_summary_generated) and generated.long_summary:
        item.long_summary = generated.long_summary
        item.long_summary_generated = True
        update_fields.extend(["long_summary", "long_summary_generated"])
    if (not item.tags or item.tags_generated) and generated.tags:
        item.tags = "\n".join(generated.tags)
        item.tags_generated = True
        update_fields.extend(["tags", "tags_generated"])

    if not _has_generated_summaries(item):
        return _mark_summary_failure(item, "Summary response did not produce all required fields")

    item.summary_status = EnrichmentStatus.COMPLETE
    item.summary_error = ""
    item.summary_retry_count = 0
    item.summary_retry_at = None
    update_fields.extend(
        ["summary_status", "summary_error", "summary_retry_count", "summary_retry_at"]
    )
    item.save(update_fields=sorted(set(update_fields)))
    return True


def enrich_item_transcript(item: Item, timeout: int = 300) -> bool:
    if item.has_transcript:
        if item.transcript_status != EnrichmentStatus.COMPLETE or item.transcript_error:
            item.transcript_status = EnrichmentStatus.COMPLETE
            item.transcript_error = ""
            item.save(update_fields=["transcript_status", "transcript_error"])
        return True

    if not can_transcribe_item(item):
        if item.transcript_status != EnrichmentStatus.COMPLETE or item.transcript_error:
            item.transcript_status = EnrichmentStatus.COMPLETE
            item.transcript_error = ""
            item.save(update_fields=["transcript_status", "transcript_error"])
        return True

    try:
        transcript = generate_item_transcript(item=item, timeout=timeout)
    except Exception as exc:
        logger.exception("Transcript generation failed for item %s", item.pk)
        return _mark_transcript_failure(item, str(exc))

    item.transcript = transcript
    item.transcript_generated = True
    item.transcript_status = EnrichmentStatus.COMPLETE
    item.transcript_error = ""
    update_fields = [
        "transcript",
        "transcript_generated",
        "transcript_status",
        "transcript_error",
    ]

    if _summary_should_refresh_from_transcript(item):
        item.summary_status = EnrichmentStatus.PENDING
        item.summary_error = ""
        item.summary_retry_count = 0
        item.summary_retry_at = None
        update_fields.extend(
            [
                "summary_status",
                "summary_error",
                "summary_retry_count",
                "summary_retry_at",
            ]
        )

    item.save(update_fields=sorted(set(update_fields)))
    return True
def recover_processing_items() -> int:
    return Item.objects.filter(
        Q(enrichment_status=EnrichmentStatus.PROCESSING)
        | Q(summary_status=EnrichmentStatus.PROCESSING)
        | Q(transcript_status=EnrichmentStatus.PROCESSING)
    ).update(
        enrichment_status=Case(
            When(
                enrichment_status=EnrichmentStatus.PROCESSING,
                then=Value(EnrichmentStatus.PENDING),
            ),
            default=F("enrichment_status"),
        ),
        summary_status=Case(
            When(
                summary_status=EnrichmentStatus.PROCESSING,
                then=Value(EnrichmentStatus.PENDING),
            ),
            default=F("summary_status"),
        ),
        transcript_status=Case(
            When(
                transcript_status=EnrichmentStatus.PROCESSING,
                then=Value(EnrichmentStatus.PENDING),
            ),
            default=F("transcript_status"),
        ),
        summary_retry_at=Case(
            When(summary_status=EnrichmentStatus.PROCESSING, then=Value(None)),
            default=F("summary_retry_at"),
        ),
    )


def claim_pending_item() -> Item | None:
    now = timezone.now()
    with transaction.atomic():
        item = (
            Item.objects.select_for_update()
            .filter(
                Q(enrichment_status=EnrichmentStatus.PENDING)
                | _claimable_transcript_query()
                | _claimable_summary_query(now)
            )
            .order_by("shared_at", "id")
            .first()
        )
        if item is None:
            return None

        update_fields: list[str] = []
        if item.enrichment_status == EnrichmentStatus.PENDING:
            item.enrichment_status = EnrichmentStatus.PROCESSING
            item.enrichment_error = ""
            update_fields.extend(["enrichment_status", "enrichment_error"])
        if _summary_should_start(item, now):
            item.summary_status = EnrichmentStatus.PROCESSING
            item.summary_error = ""
            item.summary_retry_at = None
            update_fields.extend(["summary_status", "summary_error", "summary_retry_at"])
        if _transcript_should_start(item):
            item.transcript_status = EnrichmentStatus.PROCESSING
            item.transcript_error = ""
            update_fields.extend(["transcript_status", "transcript_error"])

        if update_fields:
            item.save(update_fields=sorted(set(update_fields)))
    return item


def enrich_pending_items(
    limit: int = 1,
    timeout: int = 15,
    summary_timeout: int = 60,
    transcription_timeout: int = 300,
) -> int:
    processed = 0
    for _ in range(limit):
        item = claim_pending_item()
        if item is None:
            break
        enrich_item_metadata(
            item=item,
            timeout=timeout,
            summary_timeout=summary_timeout,
            transcription_timeout=transcription_timeout,
        )
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


def _has_generated_summaries(item: Item) -> bool:
    return bool(item.short_summary.strip() and item.long_summary.strip() and item.tag_list)


def _summary_generation_needed(item: Item) -> bool:
    if not _has_generated_summaries(item):
        return True
    return (
        any(
            (
                item.short_summary_generated,
                item.long_summary_generated,
                item.tags_generated,
            )
        )
        and item.has_transcript
    )


def _enrich_item_metadata_fields(item: Item, timeout: int = 15) -> bool:
    try:
        metadata = extract_metadata_from_url(item.original_url, timeout=timeout)
    except Exception as exc:
        logger.exception("Metadata extraction failed for item %s", item.pk)
        return _mark_metadata_failure(item, str(exc))

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


def _mark_metadata_failure(item: Item, error_message: str) -> bool:
    item.enrichment_error = error_message
    if item.has_required_feed_metadata:
        item.enrichment_status = EnrichmentStatus.COMPLETE
    else:
        item.enrichment_status = EnrichmentStatus.FAILED
    item.save(update_fields=["enrichment_status", "enrichment_error"])
    return False


def _mark_summary_failure(item: Item, error_message: str) -> bool:
    item.summary_error = error_message
    item.summary_retry_count += 1
    if _has_generated_summaries(item):
        item.summary_status = EnrichmentStatus.COMPLETE
        item.summary_retry_count = 0
        item.summary_retry_at = None
    else:
        item.summary_status = EnrichmentStatus.FAILED
        item.summary_retry_at = _next_summary_retry_at(item.summary_retry_count)
    item.save(
        update_fields=[
            "summary_status",
            "summary_error",
            "summary_retry_count",
            "summary_retry_at",
        ]
    )
    return False


def _mark_transcript_failure(item: Item, error_message: str) -> bool:
    item.transcript_error = error_message
    if item.has_transcript:
        item.transcript_status = EnrichmentStatus.COMPLETE
    else:
        item.transcript_status = EnrichmentStatus.FAILED
    item.save(update_fields=["transcript_status", "transcript_error"])
    return False
def _next_summary_retry_at(retry_count: int) -> datetime | None:
    # retry_count tracks failures so far after incrementing in _mark_summary_failure:
    # 1/2/3 map to the configured backoff slots, and 4 means retries are exhausted.
    if retry_count > len(SUMMARY_RETRY_DELAYS):
        return None
    return timezone.now() + SUMMARY_RETRY_DELAYS[retry_count - 1]


def _claimable_summary_query(now: datetime) -> Q:
    return Q(summary_status=EnrichmentStatus.PENDING) | (
        Q(summary_status=EnrichmentStatus.FAILED)
        & Q(summary_retry_count__lte=len(SUMMARY_RETRY_DELAYS))
        & (Q(summary_retry_at__isnull=True) | Q(summary_retry_at__lte=now))
    )


def _summary_should_start(item: Item, now: datetime) -> bool:
    if item.summary_status == EnrichmentStatus.PENDING:
        return True
    if item.summary_status != EnrichmentStatus.FAILED:
        return False
    if item.summary_retry_count > len(SUMMARY_RETRY_DELAYS):
        return False
    return item.summary_retry_at is None or item.summary_retry_at <= now


def _claimable_transcript_query() -> Q:
    transcribable_query = ~Q(audio_url="") | ~Q(media_url="") | Q(kind__in=TRANSCRIBABLE_ITEM_KINDS)
    return Q(transcript_status=EnrichmentStatus.PENDING) & transcribable_query


def _transcript_should_start(item: Item) -> bool:
    return item.transcript_status == EnrichmentStatus.PENDING and can_transcribe_item(item)


def _summary_should_refresh_from_transcript(item: Item) -> bool:
    return any(
        (
            not item.short_summary,
            not item.long_summary,
            not item.tags,
            item.short_summary_generated,
            item.long_summary_generated,
            item.tags_generated,
        )
    )


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
