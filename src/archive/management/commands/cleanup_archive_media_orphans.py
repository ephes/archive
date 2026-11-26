from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from botocore.exceptions import BotoCoreError, ClientError
from django.core.files.storage import storages
from django.core.management.base import BaseCommand, CommandError, OutputWrapper
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
        orphaned_count = deleted_count = failed_count = 0
        too_young_count = unknown_age_count = 0

        def count_delete() -> None:
            nonlocal deleted_count
            deleted_count += 1

        def progress() -> str:
            return f"mode={mode_label} deleted={deleted_count} failed={failed_count}"

        def write_receipt(stream: OutputWrapper, message: str) -> None:
            try:
                stream.write(message)
                stream.flush()
            except (OSError, ValueError) as exc:
                raise CommandError(f"Archive media cleanup output failed. {progress()}") from exc

        try:
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
                    write_receipt(
                        self.stdout, "Delete mode: removing orphaned archive media objects."
                    )
                else:
                    write_receipt(
                        self.stdout, "Dry-run mode: reporting orphaned archive media objects only."
                    )
                objects = inventory.execute(
                    "SELECT objects.name FROM objects LEFT JOIN refs ON objects.name = refs.name "
                    "WHERE refs.name IS NULL ORDER BY objects.name"
                )
                for (path,) in objects:
                    orphaned_count += 1
                    write_receipt(self.stdout, f"ORPHAN {path}")
                    if not delete_mode:
                        continue
                    old_enough = self._is_old_enough(path, min_age)
                    if old_enough is None:
                        unknown_age_count += 1
                        continue
                    if not old_enough:
                        too_young_count += 1
                        continue
                    # Recheck reservations and references in the final transaction.
                    try:
                        delete_unreferenced_archive_media_paths((path,), on_deleted=count_delete)
                    except (OSError, BotoCoreError, ClientError):
                        # Supported filesystem/S3 storage failures are independent.
                        # Database and programming errors must still abort the run.
                        failed_count += 1
                        write_receipt(
                            self.stderr,
                            f"Storage deletion failed for candidate {orphaned_count} "
                            "(storage_delete_failed).",
                        )
        except CommandError:
            raise
        except Exception as exc:
            if not (deleted_count or failed_count):
                raise
            raise CommandError(f"Archive media cleanup incomplete. {progress()}") from exc

        if unknown_age_count:
            write_receipt(
                self.stderr,
                f"Deferred {unknown_age_count} orphaned archive media candidates (age_unknown).",
            )
        if not orphaned_count:
            write_receipt(self.stdout, "No orphaned archive media objects found.")
        elif delete_mode and not (failed_count or unknown_age_count):
            deleted_label = "object" if deleted_count == 1 else "objects"
            write_receipt(
                self.stdout,
                self.style.SUCCESS(
                    f"Deleted {deleted_count} orphaned archive media {deleted_label}."
                ),
            )
        elif not delete_mode:
            write_receipt(
                self.stdout, "Re-run with --delete to remove the orphaned archive media objects."
            )
        write_receipt(
            self.stdout,
            "Summary: "
            f"referenced={referenced_count} "
            f"objects={object_count} "
            f"orphaned={orphaned_count} "
            f"deleted={deleted_count} "
            f"mode={mode_label} "
            f"failed={failed_count} "
            f"too_young={too_young_count} "
            f"age_unknown={unknown_age_count}",
        )
        if failed_count:
            raise CommandError(f"Archive media cleanup incomplete. {progress()}. Retry the sweep.")

    def _is_old_enough(self, path: str, min_age_seconds: int) -> bool | None:
        if min_age_seconds <= 0:
            return True
        try:
            modified = storages[ARCHIVE_MEDIA_STORAGE_ALIAS].get_modified_time(path)
            return (timezone.now() - modified).total_seconds() >= min_age_seconds
        except Exception:
            # Unknown age (unsupported backend, object already gone, backend
            # error such as an S3 404): keep the object and continue the sweep.
            return None
