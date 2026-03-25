from __future__ import annotations

import contextlib
import signal
from datetime import timedelta
from threading import Event

from django.core.management.base import BaseCommand
from django.utils import timezone

from archive.services import (
    claim_pending_item,
    enrich_item_metadata,
    recover_processing_item,
    recover_processing_items,
    recover_stale_processing_items,
)


class ItemProcessingStalled(BaseException):
    pass


class Command(BaseCommand):
    help = (
        "Process pending Archive metadata, media-archival, transcript, summary, tag, "
        "and article-audio jobs."
    )

    DEFAULT_ARTICLE_AUDIO_TIMEOUT = 30

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
            help="Per-request timeout in seconds for remote metadata fetches.",
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
            default=300,
            help="Per-request timeout in seconds for media download and transcription.",
        )
        parser.add_argument(
            "--media-archive-timeout",
            type=int,
            default=300,
            help="Per-request timeout in seconds for remote audio archival.",
        )
        parser.add_argument(
            "--stale-processing-after",
            type=int,
            default=None,
            help=(
                "Age in seconds after which runtime recovery re-queues stale processing items. "
                "Defaults to the larger of 1 hour or twice the combined per-item stage timeouts."
            ),
        )

    def _request_shutdown(self, signum, _frame) -> None:
        self.stderr.write(f"Received signal {signum}; shutting down after current item.")
        self._shutdown_requested.set()

    def handle(self, *args, **options) -> None:
        signal.signal(signal.SIGTERM, self._request_shutdown)
        signal.signal(signal.SIGINT, self._request_shutdown)

        recovered = recover_processing_items()
        if recovered:
            self.stderr.write(f"Recovered {recovered} stale processing item(s).")

        stale_processing_after = self._stale_processing_after_seconds(options)
        while not self._shutdown_requested.is_set():
            recovered = recover_stale_processing_items(
                stale_before=timezone.now() - timedelta(seconds=stale_processing_after)
            )
            if recovered:
                self.stderr.write(
                    "Recovered "
                    f"{recovered} stale processing item(s) during worker runtime "
                    f"(older than {stale_processing_after}s)."
                )
            processed = 0
            timed_out_in_pass = False
            skipped_item_ids: set[int] = set()
            for _ in range(options["limit"]):
                if self._shutdown_requested.is_set():
                    break
                item = claim_pending_item(exclude_ids=skipped_item_ids)
                if item is None:
                    break
                processed += 1
                self.stdout.write(f"Processing item {item.pk}: {item.original_url}")
                try:
                    with self._item_processing_timeout(stale_processing_after):
                        success = enrich_item_metadata(
                            item=item,
                            timeout=options["timeout"],
                            summary_timeout=options["summary_timeout"],
                            media_archive_timeout=options["media_archive_timeout"],
                            transcription_timeout=options["transcription_timeout"],
                        )
                except ItemProcessingStalled:
                    timed_out_in_pass = True
                    skipped_item_ids.add(item.pk)
                    recovered = recover_processing_item(item.pk)
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
                item.refresh_from_db(
                    fields=[
                        "enrichment_status",
                        "enrichment_error",
                        "transcript_status",
                        "transcript_error",
                        "media_archive_status",
                        "media_archive_error",
                        "summary_status",
                        "summary_error",
                        "article_audio_status",
                        "article_audio_error",
                        "title",
                    ]
                )
                if success:
                    self.stdout.write(f"Completed item {item.pk}: {item.display_title}")
                else:
                    self.stderr.write(
                        "Background enrichment did not fully succeed for item "
                        f"{item.pk}; metadata_status={item.enrichment_status}; "
                        f"metadata_error={item.enrichment_error}; "
                        f"media_archive_status={item.media_archive_status}; "
                        f"media_archive_error={item.media_archive_error}; "
                        f"transcript_status={item.transcript_status}; "
                        f"transcript_error={item.transcript_error}; "
                        f"summary_status={item.summary_status}; "
                        f"summary_error={item.summary_error}; "
                        f"article_audio_status={item.article_audio_status}; "
                        f"article_audio_error={item.article_audio_error}"
                    )
            if options["once"]:
                self.stdout.write(self.style.SUCCESS(f"Processed {processed} item(s)."))
                return
            if processed == 0 or timed_out_in_pass:
                self._shutdown_requested.wait(options["interval"])

        self.stdout.write("Archive enrichment worker exiting cleanly.")

    def _stale_processing_after_seconds(self, options) -> int:
        explicit = options["stale_processing_after"]
        if explicit is not None:
            return explicit

        combined_stage_timeouts = (
            options["timeout"]
            + options["summary_timeout"]
            + options["transcription_timeout"]
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
