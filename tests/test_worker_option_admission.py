import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from test_attempt_ownership import idle_item

from archive.management.commands import run_metadata_worker as worker
from archive.models import EnrichmentStatus as Status

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "option",
    [
        "limit",
        "interval",
        "timeout",
        "summary_timeout",
        "transcription_timeout",
        "media_archive_timeout",
        "stale_processing_after",
    ],
)
@pytest.mark.parametrize("value", [0, -1, 2**100])
def test_invalid_options_fail_before_signals_recovery_or_claim(monkeypatch, option, value):
    item = idle_item(enrichment_status=Status.PENDING)
    calls = []

    def unexpected_handler(*args):
        calls.append("handler")
        raise AssertionError("installed handler before validating options")

    monkeypatch.setattr(worker.signal, "signal", unexpected_handler)
    monkeypatch.setattr(worker.signal, "setitimer", lambda *a: calls.append("timer"))
    monkeypatch.setattr(worker, "claim_pending_item", lambda **k: calls.append("claim"))
    monkeypatch.setattr(
        worker, "recover_stale_processing_items", lambda **k: calls.append("recover")
    )
    with pytest.raises(CommandError, match=option.replace("_", "-")):
        call_command("run_metadata_worker", **{option: value})
    item.refresh_from_db()
    assert item.enrichment_status == Status.PENDING
    assert item.processing_started_at is None
    assert not calls


def test_derived_alarm_overflow_is_rejected_before_handlers(monkeypatch):
    calls = []

    def unexpected_handler(*args):
        calls.append(args)
        raise AssertionError("installed handler before validating derived alarm")

    monkeypatch.setattr(worker.signal, "signal", unexpected_handler)
    with pytest.raises(CommandError, match="stale-processing-after"):
        call_command("run_metadata_worker", once=True, timeout=2**31 - 1)
    assert not calls


def test_once_ignores_idle_interval_and_accepts_timer_boundary(monkeypatch):
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    call_command(
        "run_metadata_worker", once=True, limit=1, interval=0, stale_processing_after=2**31 - 1
    )


def test_continuous_accepts_positive_interval(monkeypatch):
    command = worker.Command()
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)

    def claim(**kwargs):
        command._shutdown_requested.set()
        return None

    monkeypatch.setattr(worker, "claim_pending_item", claim)
    call_command(command, interval=1, limit=1, stale_processing_after=1)
