"""Unit tests for the error rules (BEHAVIOR.md §3), the SSE parser (§5.1) and cancel (§6.3)."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from livekit.agents import APIConnectionError, APIStatusError

from livekit.plugins.bakbak import _errors, _ws
from livekit.plugins.bakbak._http import SSEParser
from livekit.plugins.bakbak.tts import _ws_url

from .fake_bakbak import FakeBakbak, Plan

DETAIL = [
    {"type": "missing", "loc": ["body", "voice_id"], "msg": "Field required"},
    {"loc": ["body", "output_format", "encoding"], "msg": "Value error, bad"},
]
RENDERED = "voice_id: Field required; output_format.encoding: Value error, bad"


@pytest.mark.parametrize(
    ("body", "status", "reason", "message"),
    [
        ({"message": "Shown.", "detail": "hidden", "error": "x"}, 400, None, "Shown."),
        ({"message": "Generic.", "detail": DETAIL}, 422, None, f"Generic. {RENDERED}"),
        ({"message": "Really?", "detail": DETAIL}, 422, None, f"Really? {RENDERED}"),
        ({"message": "Invalid", "detail": DETAIL}, 422, None, f"Invalid: {RENDERED}"),
        ({"message": f"Bad: {RENDERED}.", "detail": DETAIL}, 422, None, f"Bad: {RENDERED}."),
        ({"message": "  ", "detail": "From detail"}, 404, None, "From detail"),
        ({"detail": DETAIL}, 422, None, RENDERED),
        ({"detail": ["plain entry", {"msg": "no loc"}, {"loc": ["body"], "msg": "only body"}, {}]},
         422, None, "plain entry; no loc; only body"),
        ({"error": "Insufficient credits"}, 402, None, "Insufficient credits"),
        ({}, 401, "Unauthorized", "Unauthorized"),
        ("<html>bad gateway</html>", 502, None, "Bad Gateway"),
        (None, 599, None, "HTTP 599"),
    ],
)  # fmt: skip
def test_error_message(body: Any, status: int, reason: str | None, message: str) -> None:
    assert _errors.error_message(body, status, reason) == message


@pytest.mark.parametrize(
    ("body", "status", "retryable"),
    [
        ({"retryable": False}, 503, False),
        ({"retryable": True}, 500, True),
        ({"retryable": "yes"}, 400, False),
        ({}, 408, True),
        ({}, 429, True),
        ({}, 500, True),
        ({}, 502, True),
        ({}, 503, True),
        ({}, 504, True),
        ({}, 501, False),
        ({}, 422, False),
        (None, None, False),
    ],
)
def test_retryable(body: Any, status: int | None, retryable: bool) -> None:
    assert _errors.is_retryable(body, status) is retryable


def test_status_error_carries_the_body_and_request_id() -> None:
    body = {"message": "Low balance.", "error_code": "insufficient_credits", "required_inr": 1.5}
    error = _errors.status_error(402, body, request_id="req_header")
    assert isinstance(error, APIStatusError)
    assert error.status_code == 402
    assert error.request_id == "req_header"
    assert error.body == body
    assert error.retryable is False
    assert _errors.status_error(402, {"request_id": "req_body"}).request_id == "req_body"
    assert "test-key" not in repr(error)


def test_interrupted_streams() -> None:
    after = _errors.interrupted("Broke off.", request_id="req_1", audio_delivered=True)
    assert isinstance(after, APIStatusError)
    assert (after.status_code, after.request_id, after.retryable) == (-1, "req_1", False)
    empty = _errors.interrupted("Ended early.", request_id="req_2", audio_delivered=False)
    assert isinstance(empty, APIConnectionError)
    assert empty.retryable is True
    assert empty.message == "Ended early (request req_2)."


@pytest.mark.parametrize(
    ("event", "draining"),
    [
        # error_code alone is not a drain: it also means "cannot check balances".
        ({"status_code": 503, "error_code": "service_unavailable"}, False),
        ({"status_code": 503, "error_code": "service_unavailable", "message": "Retry."}, False),
        (
            {"status_code": 503, "message": "Restarting. Reconnect and send the request again."},
            True,
        ),
        ({"status_code": 503, "error_code": "capacity_exhausted", "message": "Busy."}, False),
        ({"status_code": 429, "error_code": "service_unavailable"}, False),
    ],
)
def test_draining_signal(event: dict[str, Any], draining: bool) -> None:
    assert _errors.is_draining_signal(event) is draining


def test_sse_parser() -> None:
    parser = SSEParser()
    lines = [
        b": keep-alive\r\n",
        b"\r\n",
        b"id: 7\n",
        b"retry: 100\n",
        b"event: chunk\r\n",
        b'data: {"a":\n',
        b"data:1}\n",
        b"\n",
        b"data: tail",
    ]
    assert [parser.feed_line(line) for line in lines] == [
        None, None, None, None, None, None, None, '{"a":\n1}', None
    ]  # fmt: skip
    assert parser.finish() == "tail"
    assert parser.finish() is None


async def test_cancel_drops_audio_not_yet_taken(session: aiohttp.ClientSession) -> None:
    fake = FakeBakbak(lambda _: Plan(chunks=100, chunk_delay=0.01))
    base = await fake.start()
    client = _ws.WSClient(
        url=_ws_url(base), headers={"X-API-Key": "test-key"}, session=lambda: session
    )
    ctx = client.context("c1", timeout=5)
    request = ctx.send({"text": "एक लंबा वाक्य।", "voice_id": "v", "language": "hi"})

    async def buffered() -> None:
        while request.audio_bytes == 0:  # noqa: ASYNC110 - waits on the transport's state
            await asyncio.sleep(0.01)

    await asyncio.wait_for(buffered(), 5)
    await asyncio.wait_for(ctx.cancel(), 5)  # nothing was taken from events() yet
    ctx.end_input()
    events = [ev async for ev in ctx.events()]
    await client.aclose()
    await fake.stop()
    assert request.audio_bytes > 0
    assert events == [_ws.WSCancelled(request)]
    assert len(fake.cancels()) == 1


async def test_context_ids_are_validated() -> None:
    client = _ws.WSClient(url="ws://unused", headers={}, session=aiohttp.ClientSession)
    with pytest.raises(ValueError, match="context_id"):
        client.context("has space", timeout=1)
    with pytest.raises(ValueError, match="context_id"):
        client.context("x" * 129, timeout=1)
    client.context("ok.id-1_2", timeout=1)
    with pytest.raises(ValueError, match="already in use"):
        client.context("ok.id-1_2", timeout=1)
    await client.aclose()


def test_backoff_follows_the_spec() -> None:
    for retry, (low, high) in enumerate([(0.375, 0.5), (0.75, 1.0), (1.5, 2.0)], start=1):
        for _ in range(50):
            assert low <= _ws.backoff(retry) <= high
    assert _ws.backoff(10) <= 8.0


async def test_malformed_audio_fails_only_its_own_request(session: aiohttp.ClientSession) -> None:
    """The server goes on streaming the failed request; none of that may reach the
    next request on the context, and the connection stays open."""
    fake = FakeBakbak(lambda r: Plan(bad_chunk_at=1) if r.index == 1 else Plan())
    base = await fake.start()
    client = _ws.WSClient(
        url=_ws_url(base), headers={"X-API-Key": "test-key"}, session=lambda: session
    )
    ctx = client.context("c1", timeout=5)
    body = {"voice_id": "v", "language": "hi"}
    first = ctx.send({**body, "text": "पहला वाक्य।"})
    second = ctx.send({**body, "text": "दूसरा वाक्य।"})
    ctx.end_input()
    events = [ev async for ev in ctx.events()]
    await client.aclose()
    await fake.stop()
    failed = [ev for ev in events if isinstance(ev, _ws.WSFailed)]
    assert [ev.request for ev in failed] == [first]
    assert isinstance(failed[0].error, APIStatusError)
    assert failed[0].error.status_code == -1  # after audio: never retried
    audio = [ev for ev in events if isinstance(ev, _ws.WSAudio)]
    assert [ev.request for ev in audio].count(first) == 1  # only the chunk before the bad one
    assert (
        b"".join(ev.data for ev in audio if ev.request is second)
        == FakeBakbak.chunk(fake.received[1]) * 5
    )
    assert isinstance(events[-1], _ws.WSDone)
    assert events[-1].request is second
    assert fake.ws_connections == 1
