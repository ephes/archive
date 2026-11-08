from __future__ import annotations

import logging
from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from archive.media_storage import (
    delete_unreferenced_archive_media_paths,
    item_archive_media_paths,
)
from archive.models import Item

logger = logging.getLogger(__name__)


def _cleanup_after_delete(paths: tuple[str, ...], *, using: str) -> None:
    try:
        delete_unreferenced_archive_media_paths(paths, using=using)
    except Exception:
        # The row deletion has committed. Leave objects for reference-safe recovery;
        # backend exception text and object keys may contain private information.
        logger.warning("Archive media cleanup failed after deletion; retry the orphan sweep.")


@receiver(post_delete, sender=Item)
def cleanup_deleted_item_archive_media(sender, instance: Item, using: str, **kwargs) -> None:
    paths = item_archive_media_paths(instance)
    if not paths:
        return

    transaction.on_commit(
        partial(_cleanup_after_delete, paths, using=using),
        using=using,
    )
