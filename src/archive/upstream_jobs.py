"""Small wire-value admission shared by the two upstream job clients."""

import re

# Synthesis job IDs are stored in Item.article_audio_job_id (max_length=64), so
# admission uses the storage width: an oversized ID must fail as an invalid
# envelope before any state change, not later at save time.
MAX_WIRE_ID_LENGTH = 64


def wire_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Invalid upstream identifier")
    if isinstance(value, int) and not 0 <= value < 10**MAX_WIRE_ID_LENGTH:
        raise ValueError("Invalid upstream identifier")
    identifier = str(value)
    if not re.fullmatch(rf"[A-Za-z0-9_-]{{1,{MAX_WIRE_ID_LENGTH}}}", identifier):
        raise ValueError("Invalid upstream identifier")
    return identifier


def wire_state(value: object) -> str:
    if not isinstance(value, str) or value not in {"queued", "running", "succeeded", "failed"}:
        raise ValueError("Invalid upstream job state")
    return value


def artifact_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2048
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid upstream artifact path")
    return value.strip()
