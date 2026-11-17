"""Short SQLite read snapshots shared by public feeds and processing health."""

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction


@contextmanager
def snapshot_read_transaction() -> Iterator[None]:
    """Use a SQLite read snapshot without the configured IMMEDIATE writer lock."""
    if connection.in_atomic_block:
        # An existing transaction owns its snapshot and its commit/rollback lifecycle.
        yield
        return
    if not connection.get_autocommit():
        with transaction.atomic():
            yield
        return
    connection.set_autocommit(False)
    try:
        with connection.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.set_autocommit(True)
