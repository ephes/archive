from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
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


@receiver(post_save, sender=Item)
def keep_capture_holder_current(
    sender, instance: Item, created: bool, raw: bool = False, **kwargs
) -> None:
    """Keep the newest item per normalised URL as the capture-dedupe reference."""
    if raw:
        return
    previous = getattr(instance, "_loaded_url_key", None)
    current = instance.url_key
    instance._loaded_url_key = current
    if getattr(instance, "_capture_managed", False):
        return
    if created or previous != current:
        if previous and previous != current:
            sync_capture_holder(previous)
        sync_capture_holder(current)
        instance.capture_key = (
            Item.objects.filter(pk=instance.pk).values_list("capture_key", flat=True).first()
        )


@receiver(post_delete, sender=Item)
def release_deleted_item_capture_key(sender, instance: Item, **kwargs) -> None:
    if instance.capture_key:
        sync_capture_holder(instance.capture_key)
