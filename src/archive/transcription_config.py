"""Loss-guard configuration shared by settings and direct overrides."""

import math

from django.core.exceptions import ImproperlyConfigured


def _finite_seconds(value: object, *, name: str, positive: bool = False) -> float:
    message = f"{name} must be a finite {'positive ' if positive else ''}number."
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ImproperlyConfigured(message)
    try:
        seconds = float(value)
    except (ValueError, OverflowError):
        raise ImproperlyConfigured(message) from None
    if not math.isfinite(seconds) or (positive and seconds <= 0):
        raise ImproperlyConfigured(message)
    return seconds


def transcription_max_gap_seconds(value: object) -> float:
    return _finite_seconds(value, name="ARCHIVE_TRANSCRIPTION_MAX_GAP_SECONDS")


def transcription_max_job_age_seconds(value: object) -> float:
    return _finite_seconds(value, name="ARCHIVE_TRANSCRIPTION_MAX_JOB_AGE_SECONDS", positive=True)
