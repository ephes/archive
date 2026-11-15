"""Real worker/signal journey; only the upstream summary service is synthetic."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import django


def main():
    django.setup()  # type: ignore[attr-defined]
    from django.core.management import call_command
    from django.db import connections

    from archive import services
    from archive.models import EnrichmentStatus as Status
    from archive.models import Item, ProcessingEvent
    from archive.summaries import GeneratedSummary

    root = Path(os.environ["JOURNEY_ROOT"])
    mode = os.environ["JOURNEY_MODE"]

    def generate(item, timeout):
        assert not connections["default"].in_atomic_block
        if mode == "worker":
            receipt = root / "stage.receipt"
            receipt.write_text(str(item.pk))
            receipt.replace(root / "stage.ready")
            deadline = time.monotonic() + 25
            while not (root / "stage.release").exists():
                if time.monotonic() > deadline:
                    raise AssertionError("parent did not release summary")
                time.sleep(0.01)
        return GeneratedSummary("Committed short", "Committed long", ("synthetic",))

    try:
        if mode == "setup":
            call_command("migrate", verbosity=0)
            for n in range(2):
                Item.objects.create(
                    original_url=f"https://example.test/synthetic-{n}",
                    **{
                        f"{s}_status": Status.PENDING if s == "summary" else Status.COMPLETE
                        for s in (
                            "summary",
                            "transcript",
                            "media_archive",
                            "enrichment",
                            "article_audio",
                        )
                    },
                )
        elif mode == "inspect":
            print(
                json.dumps(
                    {
                        "rows": list(
                            Item.objects.order_by("pk").values(
                                "summary_status", "short_summary", "processing_started_at"
                            )
                        ),
                        "successes": ProcessingEvent.objects.filter(succeeded=True).count(),
                    },
                    default=str,
                )
            )
        else:
            # Real Command, SIGTERM/SIGINT/SIGALRM handlers and setitimer.
            with patch.object(services, "generate_item_summaries", generate):
                call_command(
                    "run_metadata_worker",
                    once=mode == "restart",
                    limit=2,
                    interval=1,
                    stale_processing_after=60,
                )
    finally:
        connections.close_all()


if __name__ == "__main__":
    main()
