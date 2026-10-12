from __future__ import annotations

from django.core.files.storage import storages
from django.core.management.base import BaseCommand
from django.utils import timezone

from archive.media_storage import (
    ARCHIVE_MEDIA_STORAGE_ALIAS,
    delete_unreferenced_archive_media_paths,
    iter_archive_media_object_names,
    referenced_archive_media_paths,
)


class Command(BaseCommand):
    help = "Find and optionally delete orphaned archive_media storage objects."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--min-age-seconds",
            type=int,
            default=3600,
            help="Additional age grace (default: one hour); pending uploads are always protected.",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete orphaned objects instead of only reporting them.",
        )

    def handle(self, *args, **options) -> None:
        delete_mode = options["delete"]
        mode_label = "delete" if delete_mode else "dry-run"
        referenced_paths = referenced_archive_media_paths()
        object_names = sorted(set(iter_archive_media_object_names()))
        orphaned_paths = [
            object_name for object_name in object_names if object_name not in referenced_paths
        ]

        if delete_mode:
            self.stdout.write("Delete mode: removing orphaned archive media objects.")
        else:
            self.stdout.write("Dry-run mode: reporting orphaned archive media objects only.")

        if orphaned_paths:
            for orphaned_path in orphaned_paths:
                self.stdout.write(f"ORPHAN {orphaned_path}")
        else:
            self.stdout.write("No orphaned archive media objects found.")

        deleted_paths: list[str] = []
        if delete_mode and orphaned_paths:
            for path in orphaned_paths:
                if not self._is_old_enough(path, options["min_age_seconds"]):
                    continue
                # The snapshot above only selects candidates. Reservations and
                # references are verified again, transactionally, per object.
                deleted_paths.extend(delete_unreferenced_archive_media_paths((path,)))
            deleted_label = "object" if len(deleted_paths) == 1 else "objects"
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {len(deleted_paths)} orphaned archive media {deleted_label}."
                )
            )
        elif orphaned_paths:
            self.stdout.write("Re-run with --delete to remove the orphaned archive media objects.")

        self.stdout.write(
            "Summary: "
            f"referenced={len(referenced_paths)} "
            f"objects={len(object_names)} "
            f"orphaned={len(orphaned_paths)} "
            f"deleted={len(deleted_paths)} "
            f"mode={mode_label}"
        )

    def _is_old_enough(self, path: str, min_age_seconds: int) -> bool:
        if min_age_seconds <= 0:
            return True
        try:
            modified = storages[ARCHIVE_MEDIA_STORAGE_ALIAS].get_modified_time(path)
        except Exception:
            # Unknown age (unsupported backend, object already gone, backend
            # error such as an S3 404): keep the object and continue the sweep.
            return False
        return (timezone.now() - modified).total_seconds() >= min_age_seconds
