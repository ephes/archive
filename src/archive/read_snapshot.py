"""Short SQLite read snapshots shared by public feeds and processing health."""

from collections.abc import Iterator
from contextlib import contextmanager, suppress

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
    try:
        connection.set_autocommit(False)
        with connection.cursor() as cursor:
            cursor.execute("BEGIN DEFERRED")
        yield
        connection.commit()
    except BaseException:
        try:
            connection.rollback()
        except BaseException:
            # Restoring autocommit could commit a transaction that failed to roll back.
            _retire_connection()
        else:
            with suppress(BaseException):
                _restore_autocommit()
        raise
    else:
        _restore_autocommit()


def _restore_autocommit() -> None:
    try:
        connection.set_autocommit(True)
    except BaseException:
        _retire_connection()
        raise


def _retire_connection() -> None:
    # Django reconnects on the next use. Closing must preserve the primary error.
    with suppress(BaseException):
        connection.close()
