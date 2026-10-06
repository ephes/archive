from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from archive.capture import sync_capture_holder
from archive.media_storage import (
    archive_media_path_is_referenced,
    delete_archive_media_paths,
    item_archive_media_paths,
)
from archive.models import Item


@receiver(post_delete, sender=Item)
def cleanup_deleted_item_archive_media(sender, instance: Item, using: str, **kwargs) -> None:
    paths = item_archive_media_paths(instance)
    if not paths:
        return

    def delete_unreferenced_paths() -> None:
        paths_to_delete = [
            path
            for path in paths
            if not archive_media_path_is_referenced(path, using=using)
        ]
        delete_archive_media_paths(paths_to_delete)

    transaction.on_commit(delete_unreferenced_paths, using=using)


@receiver(pre_delete, sender=Item)
def remember_deleted_item_url_key(sender, instance: Item, **kwargs) -> None:
    # Read the persisted fingerprint: the instance may be stale or have deferred fields.
    instance._persisted_url_key = (
        Item.objects.filter(pk=instance.pk).values_list("url_key", flat=True).first()
    )


@receiver(post_delete, sender=Item)
def release_deleted_item_capture_key(sender, instance: Item, **kwargs) -> None:
    """Hand the capture reference to the newest remaining copy of the deleted item's URL."""
    url_key = getattr(instance, "_persisted_url_key", None)
    if url_key:
        sync_capture_holder(url_key)
