from importlib import import_module

from django.db import migrations, models

fts = import_module("archive.migrations.0011_item_search_fts")
guarded_fts = import_module("archive.migrations.0019_fts_content_update_guard")
# SQLite rebuilds archive_item for these columns, which drops its search triggers.
RESTORE_FTS_TRIGGERS = [
    fts.DROP_INSERT_TRIGGER_SQL,
    fts.DROP_DELETE_TRIGGER_SQL,
    fts.DROP_UPDATE_TRIGGER_SQL,
    fts.CREATE_INSERT_TRIGGER_SQL,
    fts.CREATE_DELETE_TRIGGER_SQL,
    guarded_fts.CREATE_UPDATE_TRIGGER_SQL,
]


class Migration(migrations.Migration):
    dependencies = [
        ("archive", "0021_pendingarchivemediaupload_owner"),
    ]

    operations = [
        # Restore after a reverse table rebuild as well as the forward AddFields.
        migrations.RunSQL(sql=migrations.RunSQL.noop, reverse_sql=RESTORE_FTS_TRIGGERS),
        migrations.AddField(
            model_name="item",
            name="transcript_job_id",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="item",
            name="transcript_job_ref",
            field=models.CharField(blank=True, db_default="", default="", max_length=128),
        ),
        migrations.AddField(
            model_name="item",
            name="transcript_job_submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="item",
            name="transcript_poll_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunSQL(sql=RESTORE_FTS_TRIGGERS, reverse_sql=migrations.RunSQL.noop),
    ]
