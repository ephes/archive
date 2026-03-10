from __future__ import annotations

import signal
from threading import Event

from django.core.management.base import BaseCommand

from archive.services import claim_pending_item, enrich_item_metadata, recover_processing_items


class Command(BaseCommand):
    help = "Process pending Archive metadata extraction jobs."

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

    def _request_shutdown(self, signum, _frame) -> None:
        self.stderr.write(f"Received signal {signum}; shutting down after current item.")
        self._shutdown_requested.set()

    def handle(self, *args, **options) -> None:
        signal.signal(signal.SIGTERM, self._request_shutdown)
        signal.signal(signal.SIGINT, self._request_shutdown)

        recovered = recover_processing_items()
        if recovered:
            self.stderr.write(f"Recovered {recovered} stale processing item(s).")

        while not self._shutdown_requested.is_set():
            processed = 0
            for _ in range(options["limit"]):
                if self._shutdown_requested.is_set():
                    break
                item = claim_pending_item()
                if item is None:
                    break
                processed += 1
                self.stdout.write(f"Processing item {item.pk}: {item.original_url}")
                success = enrich_item_metadata(item=item, timeout=options["timeout"])
                item.refresh_from_db(fields=["enrichment_status", "enrichment_error", "title"])
                if success:
                    self.stdout.write(f"Completed item {item.pk}: {item.display_title}")
                else:
                    self.stderr.write(
                        "Metadata extraction did not fully succeed for item "
                        f"{item.pk}; status={item.enrichment_status}; error={item.enrichment_error}"
                    )
            if options["once"]:
                self.stdout.write(self.style.SUCCESS(f"Processed {processed} item(s)."))
                return
            if processed == 0:
                self._shutdown_requested.wait(options["interval"])

        self.stdout.write("Archive metadata worker exiting cleanly.")
