from unittest.mock import MagicMock, Mock, call

import pytest

from archive import read_snapshot


@pytest.mark.parametrize(
    ("stage", "rollback_fails", "reset_fails"),
    [
        ("body", True, False),
        ("body", False, True),
        ("body", True, True),
        ("commit", False, False),
        ("commit", True, False),
        ("reset", False, True),
    ],
)
def test_owned_snapshot_preserves_primary_failure_and_settles_connection(
    monkeypatch, stage, rollback_fails, reset_fails
):
    db = MagicMock(in_atomic_block=False)
    db.get_autocommit.return_value = True
    monkeypatch.setattr(read_snapshot, "connections", {"default": db})
    primary = KeyboardInterrupt("cancelled") if stage == "body" else RuntimeError(stage)
    if stage == "commit":
        db.commit.side_effect = primary
    if rollback_fails:
        db.rollback.side_effect = RuntimeError("rollback failed")

    def set_autocommit(enabled):
        if enabled and reset_fails:
            raise primary if stage == "reset" else RuntimeError("reset failed")

    db.set_autocommit.side_effect = set_autocommit
    if rollback_fails and reset_fails:
        db.close.side_effect = RuntimeError("close failed")
    with pytest.raises(type(primary)) as caught:
        with read_snapshot.snapshot_read_transaction():
            if stage == "body":
                raise primary

    assert caught.value is primary
    expected = [call.set_autocommit(False), call.cursor()]
    if stage != "body":
        expected.append(call.commit())
    if stage != "reset":
        expected.append(call.rollback())
    if not rollback_fails:
        expected.append(call.set_autocommit(True))
    if rollback_fails or reset_fails:
        expected.append(call.close())
    assert db.method_calls[1:] == expected
    db.cursor.return_value.__enter__.return_value.execute.assert_called_once_with("BEGIN DEFERRED")

    db.reset_mock(side_effect=True)
    next_request = Mock()
    with read_snapshot.snapshot_read_transaction():
        next_request()
    next_request.assert_called_once_with()
    db.commit.assert_called_once_with()
    db.set_autocommit.assert_has_calls([call(False), call(True)])
    db.rollback.assert_not_called()
    db.close.assert_not_called()
