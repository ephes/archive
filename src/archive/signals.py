from __future__ import annotations

from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from archive.media_storage import (
    delete_unreferenced_archive_media_paths,
    item_archive_media_paths,
)
from archive.models import Item


@receiver(post_delete, sender=Item)
def cleanup_deleted_item_archive_media(sender, instance: Item, using: str, **kwargs) -> None:
    paths = item_archive_media_paths(instance)
    if not paths:
        return

    transaction.on_commit(
        partial(delete_unreferenced_archive_media_paths, paths, using=using),
        using=using,
    )
