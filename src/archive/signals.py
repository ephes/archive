from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

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
