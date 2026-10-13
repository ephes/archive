"""Bound reads from operator-configured upstream APIs."""

from typing import Protocol


class ReadableResponse(Protocol):
    def read(self, size: int, /) -> bytes: ...

    def close(self) -> None: ...


MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_API_ERROR_BYTES = 64 * 1024


def read_api_response(response: ReadableResponse) -> bytes:
    payload = response.read(MAX_API_RESPONSE_BYTES + 1)
    if len(payload) > MAX_API_RESPONSE_BYTES:
        raise OSError("API response exceeded the 8 MiB limit")
    return payload


def read_api_error(response: ReadableResponse) -> str:
    try:
        return response.read(MAX_API_ERROR_BYTES).decode("utf-8", errors="replace")
    finally:
        response.close()
