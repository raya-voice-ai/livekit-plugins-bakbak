"""Shared helpers for running conformance/ scenarios against the plugin.

BEHAVIOR.md §9: the plugin raises LiveKit's error classes, and LiveKit's base
classes do the retrying. `assert_error` therefore checks a §3.1 class by the
LiveKit class it maps to:

| BEHAVIOR.md §3.1 class                         | the plugin raises                          |
|------------------------------------------------|--------------------------------------------|
| APIStatusError and its subclasses (by status)  | ``APIStatusError(status_code=status)``     |
| StreamInterruptedError, after audio            | ``APIStatusError(status_code=status or -1, |
|                                                |   retryable=False)``                       |
| StreamInterruptedError, no audio (§5.1)        | ``APIConnectionError(retryable=True)``     |
| APITimeoutError                                | ``APITimeoutError``                        |
| APIConnectionError                             | ``APIConnectionError``                     |
| WebSocketClosedError                           | ``APIConnectionError(retryable=False)``    |

``error_code`` is read from the error's ``body``, which is the parsed JSON.
After audio a StreamInterruptedError is ``retryable=False`` here: in LiveKit the
flag decides whether the base class retries, and after audio it must not
(BEHAVIOR.md §4.1; LiveKit's ChunkedStream would otherwise replay the audio).
With no audio it is a connection failure, retryable; LiveKit's
``APIConnectionError`` has no request id field, so the id is in its message.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from livekit.agents import APIConnectionError, APIError, APIStatusError, APITimeoutError

CONFORMANCE = Path(__file__).parent.parent / "conformance"

STATUS_CLASSES = {
    "APIStatusError": None,
    "BadRequestError": 400,
    "AuthenticationError": 401,
    "InsufficientCreditsError": 402,
    "PermissionDeniedError": 403,
    "NotFoundError": 404,
    "RequestTooLargeError": 413,
    "UnprocessableEntityError": 422,
    "RateLimitError": 429,
    "InternalServerError": 500,  # any status >= 500
}


def load(kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((CONFORMANCE / kind).glob("*.json"))
    ]


def subset(expected: Mapping[str, Any], actual: object) -> bool:
    if not isinstance(actual, Mapping):
        return False
    return all(key in actual and actual[key] == value for key, value in expected.items())


def assert_error(error: BaseException, expected: Mapping[str, Any], audio_bytes: int) -> None:
    """Match an error against a fixture's ``error`` object, mapped as described above."""
    cls = expected["class"]
    assert isinstance(error, APIError), f"expected {cls}, got {error!r}"
    status = expected.get("status")
    if cls in STATUS_CLASSES:
        assert isinstance(error, APIStatusError), f"expected {cls}, got {error!r}"
        assert error.status_code == status
        floor = STATUS_CLASSES[cls]
        if floor == 500:
            assert isinstance(status, int)
            assert status >= 500
        elif floor is not None:
            assert status == floor
    elif cls == "StreamInterruptedError" and audio_bytes == 0:
        assert status is None
        assert isinstance(error, APIConnectionError), f"expected {cls}, got {error!r}"
    elif cls == "StreamInterruptedError":
        assert isinstance(error, APIStatusError), f"expected {cls}, got {error!r}"
        assert error.status_code == (status if status is not None else -1)
    elif cls == "APITimeoutError":
        assert isinstance(error, APITimeoutError), f"expected {cls}, got {error!r}"
    elif cls in ("APIConnectionError", "WebSocketClosedError"):
        assert isinstance(error, APIConnectionError), f"expected {cls}, got {error!r}"
        if cls == "WebSocketClosedError":
            assert error.retryable is False
    else:
        raise AssertionError(f"unknown error class in fixture: {cls}")

    body = error.body
    if "error_code" in expected:
        code = body.get("error_code") if isinstance(body, Mapping) else None
        assert code == expected["error_code"]
    if "retryable" in expected:
        after_audio = cls == "StreamInterruptedError" and audio_bytes > 0
        wanted = False if after_audio else expected["retryable"]
        assert error.retryable is wanted, f"retryable {error.retryable} != {wanted}"
    if "request_id" in expected:
        if isinstance(error, APIConnectionError) and expected["request_id"] is not None:
            assert f"(request {expected['request_id']})" in error.message, error.message
        else:
            assert getattr(error, "request_id", None) == expected["request_id"]
    if "message" in expected:
        assert error.message == expected["message"]
    if "message_contains" in expected:
        assert expected["message_contains"] in error.message, error.message
    if "audio_bytes" in expected:
        assert audio_bytes == expected["audio_bytes"]
    if "body_fields" in expected:
        assert subset(expected["body_fields"], body), body


def assert_sleeps(sleeps: list[float], expected: list[list[float]]) -> None:
    assert len(sleeps) == len(expected), f"slept {sleeps}, expected {expected}"
    for got, (low, high) in zip(sleeps, expected, strict=True):
        assert low <= got <= high, f"slept {got}, expected [{low}, {high}]"
