"""Capture configuration admission, shared by settings and direct overrides."""

import re

from django.core.exceptions import ImproperlyConfigured

# A year bounds both historical lookup and datetime subtraction in normal operation.
MAX_CAPTURE_DEDUPE_SECONDS = 365 * 24 * 60 * 60


def capture_dedupe_seconds(value: object) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_CAPTURE_DEDUPE_SECONDS:
        raise ImproperlyConfigured(
            "ARCHIVE_CAPTURE_DEDUPE_SECONDS must be an integer from 0 to 31536000."
        )
    return value
