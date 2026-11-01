from urllib.parse import urlparse

from django.db import migrations

YOUTUBE_PAGE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def _is_youtube_url(url):
    try:
        return urlparse(url or "").netloc.lower() in YOUTUBE_PAGE_HOSTS
    except ValueError:
        return False


def seed_youtube_media_archive_success(apps, schema_editor):
    """Start the YouTube archival clock at the newest item with archived YouTube audio.

    Items carry no archival timestamp, so ``shared_at`` stands in for it; the real
    archival happened at or after that moment, so the seeded age is an upper bound.
    """
    Item = apps.get_model("archive", "Item")
    ProcessingStepState = apps.get_model("archive", "ProcessingStepState")
    archived = (
        Item.objects.exclude(archived_audio_path="")
        .order_by("-shared_at")
        .values_list("shared_at", "original_url", "media_url")
        .iterator()
    )
    for shared_at, original_url, media_url in archived:
        if _is_youtube_url(media_url) or _is_youtube_url(original_url):
            ProcessingStepState.objects.update_or_create(
                step="media_archive_youtube", defaults={"last_success_at": shared_at}
            )
            return


class Migration(migrations.Migration):
    dependencies = [
        ("archive", "0016_processing_health"),
    ]

    operations = [
        migrations.RunPython(seed_youtube_media_archive_success, migrations.RunPython.noop),
    ]
