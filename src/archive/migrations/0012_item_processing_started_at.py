from importlib import import_module

from django.db import migrations, models


fts = import_module("archive.migrations.0011_item_search_fts")


class Migration(migrations.Migration):

    dependencies = [
        ("archive", "0011_item_search_fts"),
    ]

    operations = [
        # SQLite remakes the Item table when removing the indexed column on rollback,
        # dropping its triggers. Restore them after that reverse schema operation.
        migrations.RunSQL(
            sql=migrations.RunSQL.noop,
            reverse_sql=[
                fts.DROP_INSERT_TRIGGER_SQL, fts.DROP_DELETE_TRIGGER_SQL,
                fts.DROP_UPDATE_TRIGGER_SQL, fts.CREATE_INSERT_TRIGGER_SQL,
                fts.CREATE_DELETE_TRIGGER_SQL, fts.CREATE_UPDATE_TRIGGER_SQL,
            ],
        ),
        migrations.AddField(
            model_name="item",
            name="processing_started_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
