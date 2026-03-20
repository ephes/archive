from __future__ import annotations

from django.core.management.base import BaseCommand

from archive.media_storage import (
    delete_archive_media_paths,
    iter_archive_media_object_names,
    referenced_archive_media_paths,
)


class Command(BaseCommand):
    help = "Find and optionally delete orphaned archive_media storage objects."

    def add_arguments(self, parser) -> None:
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
            deleted_paths = delete_archive_media_paths(orphaned_paths)
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
