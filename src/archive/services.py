from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone

from archive.article_audio import can_generate_article_audio, generate_item_article_audio
from archive.classification import (
    CURRENT_CLASSIFICATION_ENGINE_VERSION,
    ClassificationDecision,
    MediaCandidate,
    classify_item,
)
from archive.classification import (
    infer_kind as classify_infer_kind,
)
from archive.media_archival import archive_item_audio, can_archive_audio
from archive.metadata import extract_metadata_from_url
from archive.models import EnrichmentStatus, Item, ItemKind
from archive.summaries import generate_item_summaries
from archive.transcriptions import can_transcribe_item, generate_item_transcript

logger = logging.getLogger(__name__)
SUMMARY_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
)
MEDIA_ARCHIVE_RETRY_DELAYS = SUMMARY_RETRY_DELAYS
TRANSCRIBABLE_ITEM_KINDS = (ItemKind.PODCAST_EPISODE, ItemKind.VIDEO)


def infer_kind(url: str, explicit_kind: str = "", audio_url: str = "") -> str:
    return classify_infer_kind(url=url, explicit_kind=explicit_kind, audio_url=audio_url)


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

    if item.has_archived_audio:
        item.media_archive_status = EnrichmentStatus.COMPLETE
        item.media_archive_error = ""
        item.media_archive_retry_count = 0
        item.media_archive_retry_at = None
    elif _supports_media_archive(item):
        item.media_archive_status = EnrichmentStatus.PENDING
        item.media_archive_error = ""
        item.media_archive_retry_count = 0
        item.media_archive_retry_at = None
    else:
        item.media_archive_status = EnrichmentStatus.COMPLETE
        item.media_archive_error = ""
        item.media_archive_retry_count = 0
        item.media_archive_retry_at = None

    if item.has_generated_article_audio:
        item.article_audio_status = EnrichmentStatus.COMPLETE
        item.article_audio_error = ""
        item.article_audio_generated = bool(item.article_audio_generated)
        item.article_audio_poll_at = None
    elif _supports_article_audio(item):
        item.article_audio_status = EnrichmentStatus.PENDING
        item.article_audio_error = ""
        item.article_audio_poll_at = None
    else:
        item.article_audio_status = EnrichmentStatus.COMPLETE
        item.article_audio_error = ""
        item.article_audio_poll_at = None
    return item


def enrich_item_metadata(
    item: Item,
    timeout: int = 15,
    summary_timeout: int = 60,
    media_archive_timeout: int = 300,
    transcription_timeout: int = 300,
    article_audio_timeout: int = 30,
) -> bool:
    metadata_success = True
    media_archive_success = True
    transcript_success = True
    summary_success = True
    article_audio_success = True

    if item.enrichment_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        metadata_success = _enrich_item_metadata_fields(item=item, timeout=timeout)

    if item.media_archive_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        media_archive_success = enrich_item_media_archive(item=item, timeout=media_archive_timeout)

    if item.transcript_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        transcript_success = enrich_item_transcript(item=item, timeout=transcription_timeout)

    if item.summary_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        summary_success = enrich_item_summaries(item=item, timeout=summary_timeout)

    if item.article_audio_status in {EnrichmentStatus.PENDING, EnrichmentStatus.PROCESSING}:
        article_audio_success = enrich_item_article_audio(item=item, timeout=article_audio_timeout)

    return (
        metadata_success
        and media_archive_success
        and transcript_success
        and summary_success
        and article_audio_success
    )


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


def enrich_item_media_archive(item: Item, timeout: int = 300) -> bool:
    if item.has_archived_audio:
        if (
            item.media_archive_status != EnrichmentStatus.COMPLETE
            or item.media_archive_error
            or item.media_archive_retry_count
            or item.media_archive_retry_at is not None
        ):
            item.media_archive_status = EnrichmentStatus.COMPLETE
            item.media_archive_error = ""
            item.media_archive_retry_count = 0
            item.media_archive_retry_at = None
            item.save(
                update_fields=[
                    "media_archive_status",
                    "media_archive_error",
                    "media_archive_retry_count",
                    "media_archive_retry_at",
                ]
            )
        return True

    if not _supports_media_archive(item):
        if (
            item.media_archive_status != EnrichmentStatus.COMPLETE
            or item.media_archive_error
            or item.media_archive_retry_count
            or item.media_archive_retry_at is not None
        ):
            item.media_archive_status = EnrichmentStatus.COMPLETE
            item.media_archive_error = ""
            item.media_archive_retry_count = 0
            item.media_archive_retry_at = None
            item.save(
                update_fields=[
                    "media_archive_status",
                    "media_archive_error",
                    "media_archive_retry_count",
                    "media_archive_retry_at",
                ]
            )
        return True

    try:
        archived_audio = archive_item_audio(item=item, timeout=timeout)
    except Exception as exc:
        logger.exception("Audio archival failed for item %s", item.pk)
        return _mark_media_archive_failure(item, str(exc))

    item.archived_audio_path = archived_audio.object_name
    item.archived_audio_content_type = archived_audio.content_type
    item.archived_audio_size_bytes = archived_audio.size_bytes
    item.archived_video_path = archived_audio.source_object_name
    item.archived_video_content_type = archived_audio.source_content_type
    item.archived_video_size_bytes = archived_audio.source_size_bytes
    item.media_archive_status = EnrichmentStatus.COMPLETE
    item.media_archive_error = ""
    item.media_archive_retry_count = 0
    item.media_archive_retry_at = None
    item.save(
        update_fields=[
            "archived_audio_path",
            "archived_audio_content_type",
            "archived_audio_size_bytes",
            "archived_video_path",
            "archived_video_content_type",
            "archived_video_size_bytes",
            "media_archive_status",
            "media_archive_error",
            "media_archive_retry_count",
            "media_archive_retry_at",
        ]
    )
    return True


def enrich_item_article_audio(item: Item, timeout: int = 30) -> bool:
    if item.has_generated_article_audio:
        if (
            item.article_audio_status != EnrichmentStatus.COMPLETE
            or item.article_audio_error
            or item.article_audio_poll_at is not None
        ):
            item.article_audio_status = EnrichmentStatus.COMPLETE
            item.article_audio_error = ""
            item.article_audio_poll_at = None
            item.save(
                update_fields=[
                    "article_audio_status",
                    "article_audio_error",
                    "article_audio_poll_at",
                ]
            )
        return True

    if not _supports_article_audio(item):
        if (
            item.article_audio_status != EnrichmentStatus.COMPLETE
            or item.article_audio_error
            or item.article_audio_poll_at is not None
        ):
            item.article_audio_status = EnrichmentStatus.COMPLETE
            item.article_audio_error = ""
            item.article_audio_poll_at = None
            item.save(
                update_fields=[
                    "article_audio_status",
                    "article_audio_error",
                    "article_audio_poll_at",
                ]
            )
        return True

    if not _article_audio_source_ready(item):
        item.article_audio_status = EnrichmentStatus.PENDING
        item.article_audio_error = ""
        item.article_audio_poll_at = None
        item.save(
            update_fields=["article_audio_status", "article_audio_error", "article_audio_poll_at"]
        )
        return True

    try:
        update = generate_item_article_audio(item=item, timeout=timeout)
    except Exception as exc:
        logger.exception("Article audio generation failed for item %s", item.pk)
        return _mark_article_audio_failure(item, str(exc))

    if update.is_complete:
        item.article_audio_job_id = update.job_id
        item.article_audio_artifact_path = update.artifact_path
        item.article_audio_generated = True
        item.article_audio_status = EnrichmentStatus.COMPLETE
        item.article_audio_error = ""
        item.article_audio_poll_at = None
        item.save(
            update_fields=[
                "article_audio_job_id",
                "article_audio_artifact_path",
                "article_audio_generated",
                "article_audio_status",
                "article_audio_error",
                "article_audio_poll_at",
            ]
        )
        return True

    if update.is_pending:
        item.article_audio_job_id = update.job_id
        item.article_audio_status = EnrichmentStatus.PENDING
        item.article_audio_error = ""
        item.article_audio_poll_at = _next_article_audio_poll_at()
        item.save(
            update_fields=[
                "article_audio_job_id",
                "article_audio_status",
                "article_audio_error",
                "article_audio_poll_at",
            ]
        )
        return True

    return _mark_article_audio_failure(item, update.error_message)


def recover_processing_items() -> int:
    return _recover_processing_items(_processing_item_query())


def recover_processing_item(item_id: int) -> int:
    return _recover_processing_items(Q(pk=item_id) & _processing_item_query())


def recover_stale_processing_items(*, stale_before: datetime) -> int:
    return _recover_processing_items(
        _processing_item_query() & Q(processing_started_at__lte=stale_before)
    )


def _processing_item_query() -> Q:
    return (
        Q(enrichment_status=EnrichmentStatus.PROCESSING)
        | Q(summary_status=EnrichmentStatus.PROCESSING)
        | Q(transcript_status=EnrichmentStatus.PROCESSING)
        | Q(media_archive_status=EnrichmentStatus.PROCESSING)
        | Q(article_audio_status=EnrichmentStatus.PROCESSING)
    )


def _recover_processing_items(query: Q) -> int:
    return Item.objects.filter(query).update(
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
        media_archive_status=Case(
            When(
                media_archive_status=EnrichmentStatus.PROCESSING,
                then=Value(EnrichmentStatus.PENDING),
            ),
            default=F("media_archive_status"),
        ),
        article_audio_status=Case(
            When(
                article_audio_status=EnrichmentStatus.PROCESSING,
                then=Value(EnrichmentStatus.PENDING),
            ),
            default=F("article_audio_status"),
        ),
        summary_retry_at=Case(
            When(summary_status=EnrichmentStatus.PROCESSING, then=Value(None)),
            default=F("summary_retry_at"),
        ),
        media_archive_retry_at=Case(
            When(media_archive_status=EnrichmentStatus.PROCESSING, then=Value(None)),
            default=F("media_archive_retry_at"),
        ),
        article_audio_poll_at=Case(
            When(article_audio_status=EnrichmentStatus.PROCESSING, then=Value(None)),
            default=F("article_audio_poll_at"),
        ),
        processing_started_at=Value(None),
    )


def claim_pending_item(*, exclude_ids: set[int] | None = None) -> Item | None:
    now = timezone.now()
    with transaction.atomic():
        queryset = Item.objects.select_for_update().filter(
            Q(enrichment_status=EnrichmentStatus.PENDING)
            | _claimable_media_archive_query(now)
            | _claimable_transcript_query()
            | _claimable_summary_query(now)
            | _claimable_article_audio_query(now)
        )
        if exclude_ids:
            queryset = queryset.exclude(pk__in=exclude_ids)
        item = queryset.order_by("shared_at", "id").first()
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
        if _media_archive_should_start(item, now):
            item.media_archive_status = EnrichmentStatus.PROCESSING
            item.media_archive_error = ""
            item.media_archive_retry_at = None
            update_fields.extend(
                ["media_archive_status", "media_archive_error", "media_archive_retry_at"]
            )
        if _transcript_should_start(item):
            item.transcript_status = EnrichmentStatus.PROCESSING
            item.transcript_error = ""
            update_fields.extend(["transcript_status", "transcript_error"])
        if _article_audio_should_start(item, now):
            item.article_audio_status = EnrichmentStatus.PROCESSING
            item.article_audio_error = ""
            update_fields.extend(["article_audio_status", "article_audio_error"])

        if update_fields:
            item.save(update_fields=sorted(set(update_fields)))
    return item


def enrich_pending_items(
    limit: int = 1,
    timeout: int = 15,
    summary_timeout: int = 60,
    media_archive_timeout: int = 300,
    transcription_timeout: int = 300,
    article_audio_timeout: int = 30,
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
            media_archive_timeout=media_archive_timeout,
            transcription_timeout=transcription_timeout,
            article_audio_timeout=article_audio_timeout,
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

    decision = classify_item(
        original_url=item.original_url,
        current_kind=item.kind,
        audio_url=item.audio_url,
        media_url=item.media_url,
        kind_hint=metadata.kind_hint,
        metadata_candidates=tuple(
            MediaCandidate(
                url=candidate.url,
                candidate_type=candidate.candidate_type,
                detection_source=candidate.detection_source,
            )
            for candidate in metadata.media_candidates
        ),
        existing_rule=item.classification_rule,
        existing_evidence={
            **item.classification_evidence,
            "metadata_signals": {
                "html_media_candidates": [
                    {
                        "url": candidate.url,
                        "candidate_type": candidate.candidate_type,
                        "detection_source": candidate.detection_source,
                    }
                    for candidate in metadata.media_candidates
                ],
                "kind_hint": metadata.kind_hint,
            }
        },
    )
    set_item_classification(item=item, decision=decision, update_fields=update_fields)

    item.enrichment_status = EnrichmentStatus.COMPLETE
    item.enrichment_error = ""
    _refresh_media_archive_state(item, update_fields)
    _refresh_article_audio_state(item, update_fields)
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


def _mark_media_archive_failure(item: Item, error_message: str) -> bool:
    item.media_archive_error = error_message
    item.media_archive_retry_count += 1
    if item.has_archived_audio:
        item.media_archive_status = EnrichmentStatus.COMPLETE
        item.media_archive_retry_count = 0
        item.media_archive_retry_at = None
    else:
        item.media_archive_status = EnrichmentStatus.FAILED
        item.media_archive_retry_at = _next_media_archive_retry_at(item.media_archive_retry_count)
    item.save(
        update_fields=[
            "media_archive_status",
            "media_archive_error",
            "media_archive_retry_count",
            "media_archive_retry_at",
        ]
    )
    return False


def _mark_article_audio_failure(item: Item, error_message: str) -> bool:
    item.article_audio_error = error_message
    item.article_audio_status = EnrichmentStatus.FAILED
    item.article_audio_poll_at = None
    item.save(
        update_fields=[
            "article_audio_status",
            "article_audio_error",
            "article_audio_poll_at",
        ]
    )
    return False


def _next_summary_retry_at(retry_count: int) -> datetime | None:
    # retry_count tracks failures so far after incrementing in _mark_summary_failure:
    # 1/2/3 map to the configured backoff slots, and 4 means retries are exhausted.
    if retry_count > len(SUMMARY_RETRY_DELAYS):
        return None
    return timezone.now() + SUMMARY_RETRY_DELAYS[retry_count - 1]


def _next_media_archive_retry_at(retry_count: int) -> datetime | None:
    if retry_count > len(MEDIA_ARCHIVE_RETRY_DELAYS):
        return None
    return timezone.now() + MEDIA_ARCHIVE_RETRY_DELAYS[retry_count - 1]


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
    transcribable_query = (
        ~Q(archived_audio_path="")
        | ~Q(archived_video_path="")
        | ~Q(audio_url="")
        | ~Q(media_url="")
        | Q(kind__in=TRANSCRIBABLE_ITEM_KINDS)
    )
    return Q(transcript_status=EnrichmentStatus.PENDING) & transcribable_query


def _claimable_media_archive_query(now: datetime) -> Q:
    return Q(media_archive_status=EnrichmentStatus.PENDING) | (
        Q(media_archive_status=EnrichmentStatus.FAILED)
        & Q(media_archive_retry_count__lte=len(MEDIA_ARCHIVE_RETRY_DELAYS))
        & (Q(media_archive_retry_at__isnull=True) | Q(media_archive_retry_at__lte=now))
    )


def _transcript_should_start(item: Item) -> bool:
    return item.transcript_status == EnrichmentStatus.PENDING and can_transcribe_item(item)


def _media_archive_should_start(item: Item, now: datetime) -> bool:
    if item.media_archive_status == EnrichmentStatus.PENDING:
        return _supports_media_archive(item)
    if item.media_archive_status != EnrichmentStatus.FAILED:
        return False
    if item.media_archive_retry_count > len(MEDIA_ARCHIVE_RETRY_DELAYS):
        return False
    if item.media_archive_retry_at is not None and item.media_archive_retry_at > now:
        return False
    return _supports_media_archive(item)


def _claimable_article_audio_query(now: datetime) -> Q:
    has_script_source = ~Q(long_summary="") | ~Q(short_summary="") | ~Q(notes="")
    return (
        Q(article_audio_status=EnrichmentStatus.PENDING)
        & Q(kind=ItemKind.ARTICLE)
        & Q(summary_status=EnrichmentStatus.COMPLETE)
        & has_script_source
        & (Q(article_audio_poll_at__isnull=True) | Q(article_audio_poll_at__lte=now))
    )


def _article_audio_should_start(item: Item, now: datetime) -> bool:
    if item.article_audio_status != EnrichmentStatus.PENDING:
        return False
    if item.article_audio_poll_at is not None and item.article_audio_poll_at > now:
        return False
    return _supports_article_audio(item) and _article_audio_source_ready(item)


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


def _supports_article_audio(item: Item) -> bool:
    return item.kind == ItemKind.ARTICLE


def _supports_media_archive(item: Item) -> bool:
    return can_archive_audio(item)


def _article_audio_source_ready(item: Item) -> bool:
    return item.summary_status == EnrichmentStatus.COMPLETE and can_generate_article_audio(item)


def _next_article_audio_poll_at() -> datetime:
    return timezone.now() + timedelta(seconds=settings.ARCHIVE_ARTICLE_AUDIO_POLL_SECONDS)


def request_item_reprocess(item: Item) -> Item:
    update_fields: list[str] = []
    decision = classify_item(
        original_url=item.original_url,
        current_kind=item.kind,
        audio_url=item.audio_url,
        media_url=item.media_url,
        existing_rule=item.classification_rule,
        existing_evidence=item.classification_evidence,
    )
    set_item_classification(item=item, decision=decision, update_fields=update_fields)
    prepare_item_for_enrichment(item)
    update_fields.extend(
        [
            "enrichment_status",
            "enrichment_error",
            "summary_status",
            "summary_error",
            "summary_retry_count",
            "summary_retry_at",
            "transcript_status",
            "transcript_error",
            "transcript_generated",
            "media_archive_status",
            "media_archive_error",
            "media_archive_retry_count",
            "media_archive_retry_at",
            "article_audio_status",
            "article_audio_error",
            "article_audio_generated",
            "article_audio_poll_at",
        ]
    )
    # Reprocess should always force a fresh metadata pass, even when the current
    # row already looks enrichment-complete.
    item.enrichment_status = EnrichmentStatus.PENDING
    item.enrichment_error = ""
    item.save(update_fields=sorted(set(update_fields)))
    return item


def describe_item_downstream_normalization(item: Item) -> list[str]:
    descriptions: list[str] = []
    _normalize_transcript_state_for_replay(item, descriptions=descriptions)
    _normalize_media_archive_state_for_replay(item, descriptions=descriptions)
    _normalize_article_audio_state_for_replay(item, descriptions=descriptions)
    return descriptions


def normalize_item_downstream_state(
    *,
    item: Item,
    update_fields: list[str] | None = None,
) -> list[str]:
    pending_fields = update_fields if update_fields is not None else []
    _normalize_transcript_state_for_replay(item, update_fields=pending_fields)
    _normalize_media_archive_state_for_replay(item, update_fields=pending_fields)
    _normalize_article_audio_state_for_replay(item, update_fields=pending_fields)
    return pending_fields


def _normalize_transcript_state_for_replay(
    item: Item,
    *,
    update_fields: list[str] | None = None,
    descriptions: list[str] | None = None,
) -> None:
    if item.has_transcript:
        _normalize_transcript_fields(
            item=item,
            reason="transcript already exists",
            update_fields=update_fields,
            descriptions=descriptions,
        )
        return

    if can_transcribe_item(item):
        return

    _normalize_transcript_fields(
        item=item,
        reason="item is not transcribable",
        update_fields=update_fields,
        descriptions=descriptions,
    )


def _normalize_transcript_fields(
    *,
    item: Item,
    reason: str,
    update_fields: list[str] | None = None,
    descriptions: list[str] | None = None,
) -> None:
    changes: list[str] = []
    _set_normalized_attr(
        item=item,
        field_name="transcript_status",
        value=EnrichmentStatus.COMPLETE,
        label="status",
        changes=changes,
        update_fields=update_fields,
    )
    _set_normalized_attr(
        item=item,
        field_name="transcript_error",
        value="",
        label="error",
        changes=changes,
        update_fields=update_fields,
    )
    _append_normalization_description(
        feature="transcript",
        reason=reason,
        changes=changes,
        descriptions=descriptions,
    )


def _normalize_article_audio_state_for_replay(
    item: Item,
    *,
    update_fields: list[str] | None = None,
    descriptions: list[str] | None = None,
) -> None:
    if item.has_generated_article_audio:
        reason = "generated article audio already exists"
    elif not _supports_article_audio(item):
        reason = "item is not article-audio eligible"
    else:
        return

    changes: list[str] = []
    _set_normalized_attr(
        item=item,
        field_name="article_audio_status",
        value=EnrichmentStatus.COMPLETE,
        label="status",
        changes=changes,
        update_fields=update_fields,
    )
    _set_normalized_attr(
        item=item,
        field_name="article_audio_error",
        value="",
        label="error",
        changes=changes,
        update_fields=update_fields,
    )
    _set_normalized_attr(
        item=item,
        field_name="article_audio_poll_at",
        value=None,
        label="poll_at",
        changes=changes,
        update_fields=update_fields,
    )
    _append_normalization_description(
        feature="article_audio",
        reason=reason,
        changes=changes,
        descriptions=descriptions,
    )


def _normalize_media_archive_state_for_replay(
    item: Item,
    *,
    update_fields: list[str] | None = None,
    descriptions: list[str] | None = None,
) -> None:
    if item.has_archived_audio:
        reason = "archived audio already exists"
    elif not _supports_media_archive(item):
        reason = "item is not archivable"
    else:
        return

    changes: list[str] = []
    _set_normalized_attr(
        item=item,
        field_name="media_archive_status",
        value=EnrichmentStatus.COMPLETE,
        label="status",
        changes=changes,
        update_fields=update_fields,
    )
    _set_normalized_attr(
        item=item,
        field_name="media_archive_error",
        value="",
        label="error",
        changes=changes,
        update_fields=update_fields,
    )
    _set_normalized_attr(
        item=item,
        field_name="media_archive_retry_count",
        value=0,
        label="retry_count",
        changes=changes,
        update_fields=update_fields,
    )
    _set_normalized_attr(
        item=item,
        field_name="media_archive_retry_at",
        value=None,
        label="retry_at",
        changes=changes,
        update_fields=update_fields,
    )
    _append_normalization_description(
        feature="media_archive",
        reason=reason,
        changes=changes,
        descriptions=descriptions,
    )


def _refresh_article_audio_state(item: Item, update_fields: list[str]) -> None:
    if item.has_generated_article_audio:
        if item.article_audio_status != EnrichmentStatus.COMPLETE:
            item.article_audio_status = EnrichmentStatus.COMPLETE
            update_fields.append("article_audio_status")
        if item.article_audio_error:
            item.article_audio_error = ""
            update_fields.append("article_audio_error")
        if item.article_audio_poll_at is not None:
            item.article_audio_poll_at = None
            update_fields.append("article_audio_poll_at")
        return

    if _supports_article_audio(item):
        if item.article_audio_status == EnrichmentStatus.COMPLETE:
            item.article_audio_status = EnrichmentStatus.PENDING
            update_fields.append("article_audio_status")
        if item.article_audio_error:
            item.article_audio_error = ""
            update_fields.append("article_audio_error")
        return

    if item.article_audio_status != EnrichmentStatus.COMPLETE:
        item.article_audio_status = EnrichmentStatus.COMPLETE
        update_fields.append("article_audio_status")
    if item.article_audio_error:
        item.article_audio_error = ""
        update_fields.append("article_audio_error")
    if item.article_audio_poll_at is not None:
        item.article_audio_poll_at = None
        update_fields.append("article_audio_poll_at")


def _refresh_media_archive_state(item: Item, update_fields: list[str]) -> None:
    if item.has_archived_audio:
        if item.media_archive_status != EnrichmentStatus.COMPLETE:
            item.media_archive_status = EnrichmentStatus.COMPLETE
            update_fields.append("media_archive_status")
        if item.media_archive_error:
            item.media_archive_error = ""
            update_fields.append("media_archive_error")
        if item.media_archive_retry_count:
            item.media_archive_retry_count = 0
            update_fields.append("media_archive_retry_count")
        if item.media_archive_retry_at is not None:
            item.media_archive_retry_at = None
            update_fields.append("media_archive_retry_at")
        return

    if _supports_media_archive(item):
        if item.media_archive_status == EnrichmentStatus.COMPLETE:
            item.media_archive_status = EnrichmentStatus.PENDING
            update_fields.append("media_archive_status")
        if item.media_archive_error:
            item.media_archive_error = ""
            update_fields.append("media_archive_error")
        if item.media_archive_status == EnrichmentStatus.FAILED:
            item.media_archive_status = EnrichmentStatus.PENDING
            update_fields.append("media_archive_status")
        if item.media_archive_retry_count:
            item.media_archive_retry_count = 0
            update_fields.append("media_archive_retry_count")
        if item.media_archive_retry_at is not None:
            item.media_archive_retry_at = None
            update_fields.append("media_archive_retry_at")
        return

    if item.media_archive_status != EnrichmentStatus.COMPLETE:
        item.media_archive_status = EnrichmentStatus.COMPLETE
        update_fields.append("media_archive_status")
    if item.media_archive_error:
        item.media_archive_error = ""
        update_fields.append("media_archive_error")
    if item.media_archive_retry_count:
        item.media_archive_retry_count = 0
        update_fields.append("media_archive_retry_count")
    if item.media_archive_retry_at is not None:
        item.media_archive_retry_at = None
        update_fields.append("media_archive_retry_at")


def _set_normalized_attr(
    *,
    item: Item,
    field_name: str,
    value: object,
    label: str,
    changes: list[str],
    update_fields: list[str] | None = None,
) -> None:
    current_value = getattr(item, field_name)
    if current_value == value:
        return
    changes.append(
        f"{label}: {_display_normalized_value(current_value)} -> "
        f"{_display_normalized_value(value)}"
    )
    if update_fields is not None:
        setattr(item, field_name, value)
        update_fields.append(field_name)


def _append_normalization_description(
    *,
    feature: str,
    reason: str,
    changes: list[str],
    descriptions: list[str] | None = None,
) -> None:
    if descriptions is None or not changes:
        return
    descriptions.append(f"{feature}: {'; '.join(changes)} ({reason})")


def _display_normalized_value(value: object) -> str:
    if value in {"", None}:
        return "none"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _apply_classification_decision(
    *,
    item: Item,
    decision: ClassificationDecision,
    update_fields: list[str],
) -> None:
    if decision.kind != item.kind:
        item.kind = decision.kind
        update_fields.append("kind")
    if decision.rule != item.classification_rule:
        item.classification_rule = decision.rule
        update_fields.append("classification_rule")
    if decision.evidence != item.classification_evidence:
        item.classification_evidence = decision.evidence
        update_fields.append("classification_evidence")
    if item.classification_engine_version != CURRENT_CLASSIFICATION_ENGINE_VERSION:
        item.classification_engine_version = CURRENT_CLASSIFICATION_ENGINE_VERSION
        update_fields.append("classification_engine_version")


def set_item_classification(
    *,
    item: Item,
    decision: ClassificationDecision,
    update_fields: list[str] | None = None,
) -> list[str]:
    pending_fields = update_fields if update_fields is not None else []
    _apply_classification_decision(
        item=item,
        decision=decision,
        update_fields=pending_fields,
    )
    return pending_fields
