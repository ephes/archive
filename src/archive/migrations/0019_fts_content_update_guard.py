"""Skip FTS work for worker bookkeeping updates."""

from importlib import import_module

from django.db import migrations

fts = import_module("archive.migrations.0011_item_search_fts")

CREATE_UPDATE_TRIGGER_SQL = fts.CREATE_UPDATE_TRIGGER_SQL.replace(
    "AFTER UPDATE ON archive_item BEGIN",
    """AFTER UPDATE ON archive_item
WHEN new.id IS NOT old.id
  OR new.title IS NOT old.title
  OR new.short_summary IS NOT old.short_summary
  OR new.long_summary IS NOT old.long_summary
  OR new.notes IS NOT old.notes
  OR new.transcript IS NOT old.transcript
BEGIN""",
)


class Migration(migrations.Migration):
    dependencies = [("archive", "0018_processingfailureitem")]

    operations = [
        migrations.RunSQL(
            sql=[fts.DROP_UPDATE_TRIGGER_SQL, CREATE_UPDATE_TRIGGER_SQL],
            reverse_sql=[fts.DROP_UPDATE_TRIGGER_SQL, fts.CREATE_UPDATE_TRIGGER_SQL],
        ),
    ]
