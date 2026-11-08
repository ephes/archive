from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from archive.media_storage import (
    ARCHIVE_MEDIA_STORAGE_ALIAS,
    delete_unreferenced_archive_media_paths,
    iter_archive_media_object_names,
    iter_archive_media_reference_paths,
)


class Command(BaseCommand):
    help = "Find and optionally delete orphaned archive_media storage objects."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--min-age-seconds",
            type=int,
            default=3600,
            help="Age grace (default: one hour; zero disables); pending uploads stay protected.",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete orphaned objects instead of only reporting them.",
        )

    def handle(self, *args, **options) -> None:
        min_age = options["min_age_seconds"]
        if min_age < 0:
            raise CommandError("--min-age-seconds must be non-negative (zero disables age grace)")
        delete_mode = options["delete"]
        mode_label = "delete" if delete_mode else "dry-run"
        orphaned_count = deleted_count = 0

        # Spool to disk before doing anything destructive. This gives sorted,
        # unique output with bounded memory and no deletions on incomplete listing.
        with (
            TemporaryDirectory(prefix="archive-media-") as directory,
            closing(sqlite3.connect(Path(directory) / "inventory.sqlite3")) as inventory,
        ):
            inventory.execute("CREATE TABLE refs (name TEXT PRIMARY KEY) WITHOUT ROWID")
            inventory.executemany(
                "INSERT OR IGNORE INTO refs VALUES (?)",
                ((name,) for name in iter_archive_media_reference_paths()),
            )
            referenced_count = inventory.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
            inventory.execute("CREATE TABLE objects (name TEXT PRIMARY KEY) WITHOUT ROWID")
            inventory.executemany(
                "INSERT OR IGNORE INTO objects VALUES (?)",
                ((name,) for name in iter_archive_media_object_names()),
            )
            object_count = inventory.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
            if delete_mode:
                self.stdout.write("Delete mode: removing orphaned archive media objects.")
            else:
                self.stdout.write("Dry-run mode: reporting orphaned archive media objects only.")
            objects = inventory.execute(
                "SELECT objects.name FROM objects LEFT JOIN refs ON objects.name = refs.name "
                "WHERE refs.name IS NULL ORDER BY objects.name"
            )
            for (path,) in objects:
                orphaned_count += 1
                self.stdout.write(f"ORPHAN {path}")
                if delete_mode and self._is_old_enough(path, min_age):
                    # Recheck reservations and references in the final transaction.
                    deleted_count += len(delete_unreferenced_archive_media_paths((path,)))

        if not orphaned_count:
            self.stdout.write("No orphaned archive media objects found.")
        elif delete_mode:
            deleted_label = "object" if deleted_count == 1 else "objects"
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {deleted_count} orphaned archive media {deleted_label}."
                )
            )
        else:
            self.stdout.write("Re-run with --delete to remove the orphaned archive media objects.")
        self.stdout.write(
            "Summary: "
            f"referenced={referenced_count} "
            f"objects={object_count} "
            f"orphaned={orphaned_count} "
            f"deleted={deleted_count} "
            f"mode={mode_label}"
        )

    def _is_old_enough(self, path: str, min_age_seconds: int) -> bool:
        if min_age_seconds <= 0:
            return True
        try:
            modified = storages[ARCHIVE_MEDIA_STORAGE_ALIAS].get_modified_time(path)
            return (timezone.now() - modified).total_seconds() >= min_age_seconds
        except Exception:
            # Unknown age (unsupported backend, object already gone, backend
            # error such as an S3 404): keep the object and continue the sweep.
            return False
