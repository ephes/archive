from importlib import import_module

from django.db import migrations, models

# SQLite rebuilds archive_item to add NOT NULL columns with defaults, which drops
# the search triggers created in 0011. Recreate them after the new columns exist.
_search_fts = import_module("archive.migrations.0011_item_search_fts")

_TRIGGERS = (
    (_search_fts.DROP_INSERT_TRIGGER_SQL, _search_fts.CREATE_INSERT_TRIGGER_SQL),
    (_search_fts.DROP_DELETE_TRIGGER_SQL, _search_fts.CREATE_DELETE_TRIGGER_SQL),
    (_search_fts.DROP_UPDATE_TRIGGER_SQL, _search_fts.CREATE_UPDATE_TRIGGER_SQL),
)


class Migration(migrations.Migration):

    dependencies = [
        ("archive", "0013_alter_item_kind"),
    ]

    operations = [
        # Runs last when unapplying, after RemoveField has rebuilt the table again.
        *(
            migrations.RunSQL(
                sql=migrations.RunSQL.noop,
                reverse_sql=[drop_sql, create_sql],
            )
            for drop_sql, create_sql in _TRIGGERS
        ),
        migrations.AddField(
            model_name="item",
            name="article_audio_content_type",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="item",
            name="article_audio_size_bytes",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="item",
            name="article_audio_storage_path",
            field=models.CharField(blank=True, max_length=500),
        ),
        *(
            migrations.RunSQL(
                sql=[drop_sql, create_sql],
                reverse_sql=migrations.RunSQL.noop,
            )
            for drop_sql, create_sql in _TRIGGERS
        ),
    ]
