from __future__ import annotations

from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from archive.media_archival import can_archive_audio
from archive.models import EnrichmentStatus, Item, ItemKind

STEPS = ("summary", "media_archive", "article_audio")
HOLD_ARTICLE_AUDIO_ERROR = (
    "Held by reprocess_items --hold-article-audio; "
    "release with reprocess_items --only article_audio"
)
STATUS_FIELDS = (
    "enrichment_status",
    "summary_status",
    "transcript_status",
    "media_archive_status",
    "article_audio_status",
)
IN_FLIGHT = Q(
    *(Q(**{field: EnrichmentStatus.PROCESSING}) for field in STATUS_FIELDS), _connector=Q.OR
)


def parse_item_ids(tokens: list[str]) -> list[int]:
    """Parse ``318`` / ``152-176`` tokens into sorted, unique item ids."""
    ids: set[int] = set()
    for token in tokens:
        for part in token.split(","):
            part = part.strip()
            if not part:
                continue
            start_text, separator, end_text = part.partition("-")
            try:
                start = int(start_text)
                end = int(end_text) if separator else start
            except ValueError as exc:
                raise CommandError(f"Invalid item id or range: {part!r}") from exc
            if start < 1 or end < start:
                raise CommandError(f"Invalid item id or range: {part!r}")
            ids.update(range(start, end + 1))
    if not ids:
        raise CommandError("No item ids given.")
    return sorted(ids)


class Command(BaseCommand):
    help = (
        "Re-queue one failed processing step for specific items so the worker retries it. "
        "Only the named step is reset; other steps and metadata stay untouched."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--ids",
            nargs="+",
            required=True,
            help="Item ids and inclusive ranges, e.g. --ids 318 152-176.",
        )
        parser.add_argument(
            "--only",
            choices=STEPS,
            required=True,
            help="The single step to re-queue.",
        )
        parser.add_argument(
            "--stagger",
            type=int,
            default=0,
            help=(
                "Seconds between the scheduled retry times of consecutive items, so the "
                "worker does not hit an upstream API with all of them at once. Default 0. "
                "Not available for article_audio."
            ),
        )
        parser.add_argument(
            "--hold-article-audio",
            action="store_true",
            help=(
                "With --only summary: mark the pending, not yet submitted article audio of "
                "re-queued articles as failed so a successful summary does not start paid "
                "text-to-speech. Release later with --only article_audio."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing anything.",
        )

    def handle(self, *args, **options) -> None:
        step: str = options["only"]
        stagger: int = options["stagger"]
        dry_run: bool = options["dry_run"]
        hold_article_audio: bool = options["hold_article_audio"]
        if stagger < 0:
            raise CommandError("--stagger must be zero or positive.")
        if hold_article_audio and step != "summary":
            raise CommandError("--hold-article-audio only applies to --only summary.")
        if stagger and step == "article_audio":
            # The worker runs pending article audio whenever it processes the item for
            # another step, so a poll time cannot reliably delay it.
            raise CommandError("--stagger does not apply to --only article_audio.")

        item_ids = parse_item_ids(options["ids"])
        items = Item.objects.in_bulk(item_ids)
        now = timezone.now()
        prefix = "[dry-run] " if dry_run else ""
        queued = skipped = held = 0

        for item_id in item_ids:
            item = items.get(item_id)
            if item is None:
                self.stdout.write(f"{prefix}item {item_id}: skipped, not found")
                skipped += 1
                continue
            reason = _skip_reason(item, step)
            if reason:
                self.stdout.write(f"{prefix}item {item_id}: skipped, {reason}")
                skipped += 1
                continue

            due_at = now + timedelta(seconds=stagger * queued)
            hold = hold_article_audio and _article_audio_holdable(item)
            if not dry_run and not _requeue(item_id, step, due_at, hold):
                self.stdout.write(f"item {item_id}: skipped, {step} changed concurrently")
                skipped += 1
                continue
            queued += 1
            if step == "article_audio":
                line = f"{prefix}item {item_id}: article_audio failed -> pending"
            else:
                line = (
                    f"{prefix}item {item_id}: {step} failed "
                    f"(retries {getattr(item, f'{step}_retry_count')}) -> queued, "
                    f"due {due_at.isoformat()}"
                )
            if hold:
                held += 1
                line += "; article audio pending -> held"
            self.stdout.write(line)

        verb = "Would re-queue" if dry_run else "Re-queued"
        summary = f"{prefix}{verb} {step} for {queued} item(s), skipped {skipped}"
        if hold_article_audio:
            summary += f", article audio held for {held}"
        self.stdout.write(self.style.SUCCESS(summary + "."))


def _skip_reason(item: Item, step: str) -> str:
    if any(getattr(item, field) == EnrichmentStatus.PROCESSING for field in STATUS_FIELDS):
        return "the worker is processing it, try again later"
    status = getattr(item, f"{step}_status")
    if status != EnrichmentStatus.FAILED:
        return f"{step} is {status}, not failed"
    if step == "media_archive" and (item.has_archived_audio or not can_archive_audio(item)):
        return "media archival has nothing to archive"
    if step == "article_audio" and (
        item.kind != ItemKind.ARTICLE or item.has_generated_article_audio
    ):
        return "article audio not applicable"
    if step == "article_audio" and item.article_audio_job_id:
        # Releasing would only re-poll the old synthesis job, not synthesize again.
        return "article audio has an earlier synthesis job"
    return ""


def _article_audio_holdable(item: Item) -> bool:
    return (
        item.kind == ItemKind.ARTICLE
        and item.article_audio_status == EnrichmentStatus.PENDING
        and not item.has_generated_article_audio
        and not item.article_audio_job_id
    )


def _requeue(item_id: int, step: str, due_at: datetime, hold_article_audio: bool) -> bool:
    """Reset one failed step unless the item changed or the worker claimed it meanwhile.

    A worker holding the item would save its stale in-memory state afterwards (for
    example put held article audio back to pending), so in-flight items are skipped.
    """
    with transaction.atomic():
        idle_item = Item.objects.filter(pk=item_id).exclude(IN_FLIGHT)
        if hold_article_audio:
            held = idle_item.filter(
                kind=ItemKind.ARTICLE,
                article_audio_status=EnrichmentStatus.PENDING,
                article_audio_artifact_path="",
                article_audio_job_id="",
            ).update(
                article_audio_status=EnrichmentStatus.FAILED,
                article_audio_error=HOLD_ARTICLE_AUDIO_ERROR,
                article_audio_poll_at=None,
            )
            if not held:
                transaction.set_rollback(True)
                return False
        failed_step = idle_item.filter(**{f"{step}_status": EnrichmentStatus.FAILED})
        if step == "article_audio":
            updated = failed_step.filter(article_audio_job_id="").update(
                article_audio_status=EnrichmentStatus.PENDING,
                article_audio_error="",
                article_audio_poll_at=None,
            )
        else:
            # A failed step with retries left and a due retry time is how the worker
            # itself schedules a retry; resetting the counter restores the full backoff.
            updated = failed_step.update(**{f"{step}_retry_count": 0, f"{step}_retry_at": due_at})
        if not updated:
            transaction.set_rollback(True)
        return bool(updated)
