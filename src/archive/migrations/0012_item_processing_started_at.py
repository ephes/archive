from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("archive", "0011_item_search_fts"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="processing_started_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
