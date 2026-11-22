# Copyright 2026 Raya Voice AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Bakbak error bodies and events (BEHAVIOR.md §3) mapped onto LiveKit's errors.

Every refusal becomes an ``APIStatusError`` whose ``body`` is the parsed JSON
(so ``error_code`` and fields such as ``required_inr`` stay readable), whose
``request_id`` is the server's, and whose ``retryable`` follows §3.3. LiveKit's
base classes do the retrying, and only while ``retryable`` is true.
"""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import aiohttp
from livekit.agents import APIConnectionError, APIError, APIStatusError

_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_DRAINING_PHRASE = "Reconnect and send the request again"
# pydantic wraps a validator's ValueError as "Value error, <text>". The server's
# own `message` quotes the text without it, so it is dropped before comparing.
_PYDANTIC_VALUE_ERROR = "Value error, "


def error_message(body: object, status: int | None, reason: str | None = None) -> str:
    """The sentence to show a person, by the precedence of §3.2."""
    if isinstance(body, Mapping):
        fields: Mapping[str, Any] = body
        detail = fields.get("detail")
        rendered = ""
        if isinstance(detail, list):
            rendered = "; ".join(filter(None, (_render_detail(entry) for entry in detail)))
        message = fields.get("message")
        if isinstance(message, str) and message.strip():
            # A generic sentence ("The request is malformed or invalid.") would hide
            # which field is wrong, so the list is added unless the message has it.
            if rendered and rendered not in message:
                message = message.rstrip()
                joiner = " " if message.endswith((".", "!", "?")) else ": "
                return f"{message}{joiner}{rendered}"
            return message
        if isinstance(detail, str) and detail.strip():
            return detail
        if rendered:
            return rendered
        error = fields.get("error")
        if isinstance(error, str) and error.strip():
            return error
    if reason:
        return reason
    if status is not None:
        try:
            return HTTPStatus(status).phrase
        except ValueError:
            pass
    return f"HTTP {status}"


def _render_detail(entry: object) -> str:
    """One pydantic validation entry as ``"output_format.encoding: <msg>"``."""
    if not isinstance(entry, Mapping):
        return str(entry) if entry else ""
    fields: Mapping[str, Any] = entry
    msg = fields.get("msg")
    if isinstance(msg, str) and msg.startswith(_PYDANTIC_VALUE_ERROR):
        msg = msg[len(_PYDANTIC_VALUE_ERROR) :]
    if not isinstance(msg, str) or not msg:
        return ""
    loc = fields.get("loc")
    parts = [str(p) for p in loc] if isinstance(loc, list) else []
    if parts and parts[0] == "body":
        parts = parts[1:]
    return f"{'.'.join(parts)}: {msg}" if parts else msg


def is_retryable(body: object, status: int | None) -> bool:
    """§3.3: the body's ``retryable`` when it is a boolean, else by status."""
    if isinstance(body, Mapping):
        fields: Mapping[str, Any] = body
        flag = fields.get("retryable")
        if isinstance(flag, bool):
            return flag
    return status in _RETRYABLE_STATUSES


def request_id_of(body: object, header: str | None = None) -> str | None:
    """``X-Request-Id`` when present, else the body's (or event's) ``request_id``."""
    if header:
        return header
    if isinstance(body, Mapping):
        fields: Mapping[str, Any] = body
        rid = fields.get("request_id")
        if isinstance(rid, str) and rid:
            return rid
    return None


def status_error(
    status: int,
    body: object,
    *,
    request_id: str | None = None,
    reason: str | None = None,
) -> APIStatusError:
    """A refusal (HTTP status, SSE ``error`` event or WebSocket ``error`` message)."""
    return APIStatusError(
        error_message(body, status, reason),
        status_code=status,
        request_id=request_id_of(body, request_id),
        body=body,
        retryable=is_retryable(body, status),
    )


def event_error(event: Mapping[str, Any], *, request_id: str | None = None) -> APIStatusError:
    """An in-band ``error`` event; its ``status_code`` is the HTTP status it stands for."""
    status = event.get("status_code")
    return status_error(
        status if isinstance(status, int) else 500,
        dict(event),
        request_id=request_id,
    )


def after_audio(error: APIError) -> APIError:
    """Mark an error that ended a request after audio was delivered.

    The request is billed once audio has left the service, and a retry would
    play its start twice, so the error must never be retried (BEHAVIOR.md §4.1).
    LiveKit's ``ChunkedStream`` retries any retryable error, so the flag is what
    keeps it from replaying audio.
    """
    error.retryable = False
    return error


def interrupted(message: str, *, request_id: str | None, audio_delivered: bool) -> APIError:
    """A stream that broke off with no status to report: it ended with neither
    ``done`` nor ``error``, or its connection closed.

    With audio delivered it is an interrupted stream: ``APIStatusError`` with
    LiveKit's -1 for "no status", the server's request id, never retried. With no
    audio it is a connection failure (BEHAVIOR.md §5.1) that LiveKit may retry;
    ``APIConnectionError`` has no request id field, so the id is in its message.
    """
    if audio_delivered:
        return APIStatusError(message, status_code=-1, request_id=request_id, retryable=False)
    suffix = f" (request {request_id})" if request_id else ""
    return APIConnectionError(f"{message.rstrip('.')}{suffix}.", retryable=True)


def detached(error: APIError, cause: BaseException) -> APIError:
    """`error`, caused by `cause`, with nothing reachable from it that holds the key.

    aiohttp's response errors keep the request they answer (``request_info``),
    headers and all, so ``X-API-Key`` would be reachable from the error's
    ``__cause__``. Such a cause is dropped; any other is kept for debugging.
    Raise the result outside the ``except`` block that caught `cause`: inside it,
    Python records the handled exception as ``__context__`` regardless.
    """
    error.__cause__ = None if _holds_request(cause) else cause
    error.__suppress_context__ = True
    return error


def _holds_request(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, aiohttp.ClientResponseError) or hasattr(current, "request_info"):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def is_draining_signal(event: Mapping[str, Any]) -> bool:
    """A WebSocket refusal saying the connection is draining (BEHAVIOR.md §6.4).

    Only status 503 with the reconnect phrase: ``error_code``
    ``service_unavailable`` alone also means the service cannot check balances,
    an ordinary retryable refusal that a reconnect would not cure."""
    message = event.get("message")
    return (
        event.get("status_code") == 503 and isinstance(message, str) and _DRAINING_PHRASE in message
    )
