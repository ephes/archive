import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.core.exceptions import ImproperlyConfigured

from archive.models import Item
from archive.services import enrich_item_transcript
from archive.transcriptions import (
    BatchTranscriptionJobUpdate,
    _check_batch_transcript_coverage,
    find_transcript_gaps,
)

GAP = "ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS"
AGE = "ARCHIVE_TRANSCRIPTION_MAX_JOB_AGE_SECONDS"
INVALID = [float("nan"), float("inf"), -float("inf"), True, False, None, "invalid", 10**1000]


@pytest.mark.parametrize("name", [GAP, AGE])
@pytest.mark.parametrize(
    "value", ["nan", "NaN", "inf", "-Infinity", "1e999", "true", "", "invalid"]
)
def test_loss_guard_invalid_at_startup(name, value):
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.base"],
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        env={**os.environ, name: value},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert f"ImproperlyConfigured: {name} must be a finite" in result.stderr
    domain = "positive number." if name == AGE else "number."
    assert result.stderr.splitlines()[-1] == (
        f"django.core.exceptions.ImproperlyConfigured: {name} must be a finite {domain}"
    )


@pytest.mark.parametrize(
    "name,value,valid",
    [
        (GAP, "0", True),
        (GAP, "-0.5", True),
        (GAP, "600.5", True),
        (AGE, "0.5", True),
        (AGE, "0", False),
        (AGE, "-1", False),
    ],
)
def test_loss_guard_startup_finite_domains(name, value, valid):
    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.base"],
        cwd=Path(__file__).resolve().parents[1] / "src" / "django",
        env={**os.environ, name: value},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is valid
    if not valid:
        assert f"{name} must be a finite positive number." in result.stderr


@pytest.mark.parametrize("value", INVALID, ids=str)
def test_gap_helper_rejects_invalid_direct_limit(value):
    with pytest.raises(ImproperlyConfigured, match=f"{GAP} must be a finite number"):
        find_transcript_gaps([{"start": 900, "end": 1000}], max_gap_seconds=value)


@pytest.mark.parametrize("value", INVALID, ids=str)
def test_coverage_override_rejected_before_artifact_request(value, settings, monkeypatch):
    setattr(settings, GAP, value)
    request = Mock(side_effect=AssertionError("must reject configuration before transport"))
    monkeypatch.setattr("archive.transcriptions._request_json", request)
    with pytest.raises(ImproperlyConfigured, match=f"{GAP} must be a finite number"):
        _check_batch_transcript_coverage(
            completed=BatchTranscriptionJobUpdate("job-1", "succeeded", "Text", "", "/v1/a.json"),
            timeout=1,
        )
    request.assert_not_called()


@pytest.mark.parametrize("name,values", [(GAP, INVALID), (AGE, [*INVALID, 0, -1])])
def test_enrichment_rejects_override_before_io(name, values, settings, monkeypatch):
    request = Mock(side_effect=AssertionError("must reject configuration before transport"))
    monkeypatch.setattr("archive.services.generate_item_transcript", request)
    item = Item(
        original_url="https://example.test/audio.mp3", audio_url="https://example.test/a.mp3"
    )
    for value in values:
        setattr(settings, name, value)
        with pytest.raises(ImproperlyConfigured, match=f"{name} must be a finite"):
            enrich_item_transcript(item)
    request.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, -0.5])
def test_gap_helper_explicit_finite_opt_out(limit):
    assert find_transcript_gaps([{"start": 900, "end": 1000}], max_gap_seconds=limit) == []


@pytest.mark.parametrize("limit,expected", [(600, [(0.0, 900.0)]), (900.0, [])])
def test_gap_helper_finite_limits(limit, expected):
    assert find_transcript_gaps([{"start": 900, "end": 1000}], max_gap_seconds=limit) == expected
