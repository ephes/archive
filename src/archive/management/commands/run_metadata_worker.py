from __future__ import annotations

import contextlib
import signal
from datetime import timedelta
from threading import Event

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from archive.models import Item
from archive.services import (
    claim_pending_item,
    enrich_item_metadata,
    recover_processing_item,
    recover_stale_processing_items,
)
from archive.transcriptions import staging_allowance_seconds


class ItemProcessingStalled(BaseException):
    pass


class Command(BaseCommand):
    help = (
        "Process pending Archive metadata, media-archival, transcript, summary, tag, "
        "and article-audio jobs."
    )

    DEFAULT_ARTICLE_AUDIO_TIMEOUT = 30
    MAX_NUMERIC_OPTION = 2**31 - 1

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._shutdown_requested = Event()

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process available items once and exit.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum items per processing pass.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=10,
            help="Idle poll interval in seconds for long-running worker mode.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=15,
            help="Total metadata fetch budget in seconds (DNS, redirects and body reads included).",
        )
        parser.add_argument(
            "--summary-timeout",
            type=int,
            default=60,
            help="Per-request timeout in seconds for summary generation.",
        )
        parser.add_argument(
            "--transcription-timeout",
            type=int,
            default=1800,
            help=(
                "Budget in seconds for media download and transcription; staging a large "
                "archived file gets at least its size at "
                "ARCHIVE_TRANSCRIPTION_UPLOAD_MIN_BYTES_PER_SECOND."
            ),
        )
        parser.add_argument(
            "--media-archive-timeout",
            type=int,
            default=3600,
            help="Budget in seconds for remote audio archival (download and extraction).",
        )
        parser.add_argument(
            "--stale-processing-after",
            type=int,
            default=None,
            help=(
                "Age in seconds after which startup and runtime recovery re-queues "
                "stale processing items. "
                "Defaults to the larger of 1 hour or twice the combined per-item stage "
                "timeouts, counting the transcript stage at least as long as staging a "
                "file of ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES takes."
            ),
        )

    def _request_shutdown(self, signum, _frame) -> None:
        self.stderr.write(f"Received signal {signum}; shutting down after current item.")
        self._shutdown_requested.set()

    def handle(self, *args, **options) -> None:
        stale_processing_after = self._validate_options(options)
        signal.signal(signal.SIGTERM, self._request_shutdown)
        signal.signal(signal.SIGINT, self._request_shutdown)

        while not self._shutdown_requested.is_set():
            recovered = recover_stale_processing_items(
                stale_before=timezone.now() - timedelta(seconds=stale_processing_after)
            )
            if recovered:
                self.stderr.write(
                    "Recovered "
                    f"{recovered} stale processing item(s) during worker runtime "
                    f"(without a claim timestamp or older than {stale_processing_after}s)."
                )
            processed = 0
            unsuccessful = 0
            timed_out_in_pass = False
            skipped_item_ids: set[int] = set()
            for _ in range(options["limit"]):
                if self._shutdown_requested.is_set():
                    break
                item = claim_pending_item(exclude_ids=skipped_item_ids)
                if item is None:
                    break
                processed += 1
                self.stdout.write(f"Processing item {item.pk}: enrichment pass.")
                claimed_started_at = item.processing_started_at
                try:
                    with self._item_processing_timeout(stale_processing_after):
                        success = enrich_item_metadata(
                            item=item,
                            timeout=options["timeout"],
                            summary_timeout=options["summary_timeout"],
                            media_archive_timeout=options["media_archive_timeout"],
                            transcription_timeout=options["transcription_timeout"],
                        )
                except Item.DoesNotExist:
                    if Item.objects.using(item._state.db).filter(pk=item.pk).exists():
                        raise
                    self.stdout.write(f"Retired deleted item {item.pk}: enrichment pass.")
                    continue
                except ItemProcessingStalled:
                    unsuccessful += 1
                    timed_out_in_pass = True
                    skipped_item_ids.add(item.pk)
                    recovered = recover_processing_item(
                        item.pk, expected_started_at=claimed_started_at
                    )
                    if recovered:
                        self.stderr.write(
                            "Processing item "
                            f"{item.pk} exceeded the stale-processing window of "
                            f"{stale_processing_after}s; re-queued its in-flight work."
                        )
                    else:
                        self.stderr.write(
                            "Processing item "
                            f"{item.pk} exceeded the stale-processing window of "
                            f"{stale_processing_after}s, but no processing state "
                            "remained to recover."
                        )
                    continue
                try:
                    item.refresh_from_db(
                        fields=[
                            "enrichment_status",
                            "transcript_status",
                            "media_archive_status",
                            "summary_status",
                            "article_audio_status",
                        ]
                    )
                except Item.DoesNotExist:
                    self.stdout.write(f"Retired deleted item {item.pk}: enrichment pass.")
                    continue
                if success:
                    self.stdout.write(f"Completed item {item.pk}: enrichment pass.")
                else:
                    unsuccessful += 1
                    self.stderr.write(
                        "Background enrichment did not fully succeed for item "
                        f"{item.pk}; metadata_status={item.enrichment_status}; "
                        f"media_archive_status={item.media_archive_status}; "
                        f"transcript_status={item.transcript_status}; "
                        f"summary_status={item.summary_status}; "
                        f"article_audio_status={item.article_audio_status}"
                    )
            if options["once"]:
                self.stdout.write(f"Processed {processed} item(s).")
                if unsuccessful:
                    raise CommandError(f"{unsuccessful} unsuccessful item(s) in this pass.")
                return
            if processed == 0 or timed_out_in_pass:
                self._shutdown_requested.wait(options["interval"])

        self.stdout.write("Archive enrichment worker exiting cleanly.")

    def _validate_options(self, options) -> int:
        fields = [
            "limit",
            "timeout",
            "summary_timeout",
            "transcription_timeout",
            "media_archive_timeout",
        ]
        if not options["once"]:
            fields.append("interval")
        if options["stale_processing_after"] is not None:
            fields.append("stale_processing_after")
        for field in fields:
            self._validate_positive_option(field, options[field])
        seconds = self._stale_processing_after_seconds(options)
        self._validate_positive_option("stale_processing_after", seconds)
        return seconds

    def _validate_positive_option(self, field: str, value: int) -> None:
        # A portable signed-32-bit seconds envelope fits setitimer, timedelta
        # and Event.wait without platform overflow. Check before claiming work.
        if type(value) is not int or not 1 <= value <= self.MAX_NUMERIC_OPTION:
            raise CommandError(
                f"--{field.replace('_', '-')} must be an integer "
                f"from 1 to {self.MAX_NUMERIC_OPTION}."
            )

    def _stale_processing_after_seconds(self, options) -> int:
        explicit = options["stale_processing_after"]
        if explicit is not None:
            return explicit

        # A large staged upload extends the transcript stage past its timeout.
        transcript_stage = max(
            options["transcription_timeout"],
            staging_allowance_seconds(settings.ARCHIVE_MEDIA_ARCHIVE_MAX_BYTES),
        )
        combined_stage_timeouts = (
            options["timeout"]
            + options["summary_timeout"]
            + transcript_stage
            + options["media_archive_timeout"]
            + self.DEFAULT_ARTICLE_AUDIO_TIMEOUT
        )
        return max(3600, combined_stage_timeouts * 2)

    def _raise_item_processing_stalled(self, signum, _frame) -> None:
        raise ItemProcessingStalled(f"Processing alarm fired ({signum}).")

    @contextlib.contextmanager
    def _item_processing_timeout(self, seconds: int):
        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._raise_item_processing_stalled)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
