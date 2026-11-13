import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from test_capture_dedupe import share

from archive.capture import capture_item
from archive.models import Item


@pytest.mark.parametrize("value", ["-1", "31536001", "999999999999999999999", "invalid", ""])
def test_capture_window_rejected_at_startup(value):
    result = subprocess.run(
        [sys.executable, "-c", "from config.settings.base import ARCHIVE_CAPTURE_DEDUPE_SECONDS"],
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        env={**os.environ, "ARCHIVE_CAPTURE_DEDUPE_SECONDS": value},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "ARCHIVE_CAPTURE_DEDUPE_SECONDS must be an integer from 0 to 31536000" in result.stderr


@pytest.mark.django_db
@pytest.mark.parametrize("value", [-1, 31536001, 10**50, True, 1.5, None, "secret-invalid"])
def test_override_rejected_before_transaction(value, settings, django_assert_num_queries):
    settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS = value
    with django_assert_num_queries(0), pytest.raises(ImproperlyConfigured):
        capture_item(Item(original_url="https://example.com/x"))


@pytest.mark.django_db
@pytest.mark.parametrize("channel", ["api", "web"])
@pytest.mark.parametrize("value", [-1, 31536001])
def test_invalid_window_does_not_insert_or_enqueue(
    client, editor_user, settings, monkeypatch, channel, value
):
    client.force_login(editor_user)
    settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS = value
    prepare = Mock(side_effect=AssertionError("must refuse before enrichment"))
    monkeypatch.setattr("archive.capture.prepare_item_for_enrichment", prepare)
    with pytest.raises(ImproperlyConfigured):
        share(client, channel, "https://example.com/x")
    prepare.assert_not_called()
    assert not Item.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("channel", ["api", "web"])
@pytest.mark.parametrize("value", [0, 31536000])
def test_window_boundaries_on_both_capture_paths(client, editor_user, settings, channel, value):
    client.force_login(editor_user)
    settings.ARCHIVE_CAPTURE_DEDUPE_SECONDS = value
    first, _ = share(client, channel, "https://example.com/x")
    second, _ = share(client, channel, "https://example.com/x")
    assert (first == second) is (value > 0)
