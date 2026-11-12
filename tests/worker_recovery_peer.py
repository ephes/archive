"""Synthetic subprocess peer for the durable SQLite worker journey."""

from __future__ import annotations

import json
import os
import time
from contextlib import nullcontext
from datetime import datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import django


def main() -> None:
    from django.conf import settings

    # Configure the synthetic storage before model imports resolve its alias.
    root = Path(os.environ["JOURNEY_ROOT"])
    settings.STORAGES["archive_media"]["OPTIONS"]["location"] = str(root / "media")
    django.setup()  # type: ignore[attr-defined]
    from django.core.files.base import ContentFile
    from django.core.files.storage import storages
    from django.core.management import call_command
    from django.db import connections

    from archive import services
    from archive.management.commands.run_metadata_worker import Command
    from archive.media_archival import ArchivedAudio, save_reserved_media_file
    from archive.models import EnrichmentStatus as Status
    from archive.models import Item, ItemKind, PendingArchiveMediaUpload, ProcessingEvent

    storage = storages["archive_media"]
    mode = os.environ["JOURNEY_MODE"]
    now = datetime.fromisoformat(os.environ["JOURNEY_NOW"])
    stages = ("enrichment", "summary", "transcript", "media_archive", "article_audio")

    def read(name):
        with storage.open(name) as handle:
            return handle.read().decode()

    def download(item, timeout):
        assert not connections["default"].in_atomic_block
        staged = mode != "killed" or os.environ["JOURNEY_POINT"] == "before-publication"
        name = ""
        if staged:
            name = save_reserved_media_file(
                target_name=f"staged/{mode}.mp3",
                upload_name="audio.mp3",
                file_handle=BytesIO(mode.encode()),
                using="default",
            )
        if mode in {"killed", "late"}:
            receipt = root / f"{mode}.receipt"
            receipt.write_text(
                json.dumps(
                    {
                        "pk": item.pk,
                        "started_at": item.processing_started_at.isoformat(),
                        "staged": name,
                    }
                )
            )
            receipt.replace(root / f"{mode}.ready")
            deadline = time.monotonic() + 120  # outlives parent restart + inspect
            while not (root / f"{mode}.release").exists():
                if time.monotonic() > deadline:
                    raise AssertionError("parent did not release synthetic upstream")
                time.sleep(0.02)
        return ArchivedAudio(name, "audio/mpeg", len(mode))

    try:
        with patch("django.utils.timezone.now", return_value=now):
            if mode == "setup":
                call_command("migrate", verbosity=0)
                complete = {f"{stage}_status": Status.COMPLETE for stage in stages}
                storage.save("shared/video.mp4", ContentFile(b"referenced bytes"))
                Item.objects.create(
                    original_url="https://example.test/target.mp4",
                    media_url="https://example.test/target.mp4",
                    kind=ItemKind.VIDEO,
                    archived_video_path="shared/video.mp4",
                    **{**complete, "media_archive_status": Status.PENDING},
                )
                Item.objects.create(
                    original_url="https://example.test/shared",
                    archived_video_path="shared/video.mp4",
                    **complete,
                )
            elif mode == "inspect":
                item = Item.objects.order_by("pk").first()
                assert item is not None
                print(
                    json.dumps(
                        {
                            "status": item.media_archive_status,
                            "started_at": (
                                item.processing_started_at.isoformat()
                                if item.processing_started_at
                                else None
                            ),
                            "audio": item.archived_audio_path,
                            "bytes": (
                                read(item.archived_audio_path) if item.archived_audio_path else ""
                            ),
                            "shared_bytes": read("shared/video.mp4"),
                            "reservations": list(
                                PendingArchiveMediaUpload.objects.values_list("prefix", flat=True)
                            ),
                            "successes": ProcessingEvent.objects.filter(succeeded=True).count(),
                        }
                    )
                )
            elif mode == "late":
                # Reopen the killed attempt before recovery, then delay its result.
                item = Item.objects.order_by("pk").first()
                assert item is not None and item.media_archive_status == Status.PROCESSING
                with patch.object(services, "archive_item_audio", download):
                    assert services.enrich_item_media_archive(item)
            else:
                with (
                    patch.object(services, "archive_item_audio", download),
                    patch.object(Command, "_item_processing_timeout", lambda *a: nullcontext()),
                    patch("archive.management.commands.run_metadata_worker.signal.signal"),
                ):
                    call_command(
                        "run_metadata_worker", once=True, limit=1, stale_processing_after=120
                    )
    finally:
        connections.close_all()


if __name__ == "__main__":
    main()
