from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from archive.models import Item


class Command(BaseCommand):
    help = "Rebuild the SQLite FTS search index for Archive items."

    def handle(self, *args, **options) -> None:
        if connection.vendor != "sqlite":
            raise CommandError("rebuild_search_index currently supports SQLite only.")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = 'archive_item_fts'
                """
            )
            if cursor.fetchone() is None:
                raise CommandError("archive_item_fts does not exist. Run migrations first.")

            cursor.execute("INSERT INTO archive_item_fts(archive_item_fts) VALUES ('rebuild')")

        item_count = Item.objects.count()
        item_label = "item" if item_count == 1 else "items"
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt Archive search index for {item_count} {item_label}."
            )
        )
