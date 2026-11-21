"""conformance/ws/*.json against the plugin's WebSocket transport (`_ws.WSClient`).

The plugin's transport has the contexts of BEHAVIOR.md §6 (``context``,
``send``, ``cancel``, ``end_input``, ``events()``), so the scenarios run on it
directly with their exact request bodies. Errors are mapped onto LiveKit's
classes as described in conformance_support.py.

One rule is LiveKit's by design (§9): a retryable refusal before any audio is
delivered as an error rather than resent on the same context, because LiveKit's
base class retries the whole attempt on a fresh context (tested in
test_tts.py::test_stream_retries_a_refusal_on_a_fresh_context).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Callable
from typing import Any

import aiohttp
import pytest
from livekit.agents import APIError

from livekit.plugins import bakbak
from livekit.plugins.bakbak import _ws
from livekit.plugins.bakbak.tts import _ws_url

from .conformance_support import assert_error, assert_sleeps, load
from .scripted_server import ScriptedServer

SCENARIOS = load("ws")
IDS = [s["id"] for s in SCENARIOS]


# A retryable refusal that the SDKs resend themselves (after the backoff of
# §4.3) reaches the plugin's caller as a retryable error, at once and without a
# wait, and LiveKit retries the attempt on a fresh context (§9). Each entry: the
# connection whose script ends at the refusal, and how many steps to keep.
_LIVEKIT_RETRIES = {
    "ws_capacity_503_retried": (0, 2),
    "ws_service_unavailable_is_not_draining": (0, 2),
    # The first draining refusal is resent on a new connection by the transport
    # itself (that part is the plugin's too); the second is the ordinary error.
    "ws_drain_refused_twice": (1, 2),
}


def _adapt(scenario: dict[str, Any]) -> dict[str, Any]:
    """Map the scenarios whose retries LiveKit owns (§9) onto the plugin's."""
    if scenario["id"] not in _LIVEKIT_RETRIES:
        return scenario
    connection, steps = _LIVEKIT_RETRIES[scenario["id"]]
    scenario = copy.deepcopy(scenario)
    scenario["connections"] = scenario["connections"][: connection + 1]
    script = scenario["connections"][connection]["script"]
    script[steps:] = [{"expect_no_more": True}]
    refusal = script[steps - 1]["send"]
    (context_id,) = scenario["expect"]["contexts"]
    scenario["expect"]["contexts"][context_id] = [
        {
            "outcome": "error",
            "audio_bytes": 0,
            "error": {
                "class": "InternalServerError",
                "status": refusal["status_code"],
                "error_code": refusal.get("error_code"),
                "retryable": True,
                "request_id": refusal["request_id"],
            },
        }
    ]
    scenario["expect"]["connections"] = connection + 1
    scenario["expect"]["sleeps"] = []
    return scenario


class _Collector:
    """Reads a context's events and records each request's outcome."""

    def __init__(self, ctx: _ws.WSContext, changed: asyncio.Event) -> None:
        self.ctx = ctx
        self.sent: list[_ws.WSRequest] = []
        self.audio: dict[int, bytearray] = {}
        self.outcomes: dict[int, tuple[str, APIError | None]] = {}
        self._changed = changed
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        async for ev in self.ctx.events():
            key = id(ev.request)
            if isinstance(ev, _ws.WSAudio):
                self.audio.setdefault(key, bytearray()).extend(ev.data)
            elif isinstance(ev, _ws.WSDone):
                self.outcomes[key] = ("done", None)
            elif isinstance(ev, _ws.WSFailed):
                self.outcomes[key] = ("error", ev.error)
            else:
                self.outcomes[key] = ("cancelled", None)
            self._changed.set()

    def has_audio(self) -> bool:
        return any(self.audio.values())

    def all_settled(self) -> bool:
        return all(id(r) in self.outcomes for r in self.sent)


async def _until(changed: asyncio.Event, condition: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not condition():
            changed.clear()
            await changed.wait()

    await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
async def test_scenario(scenario: dict[str, Any], session: aiohttp.ClientSession) -> None:
    scenario = _adapt(scenario)
    expect = scenario["expect"]
    server = ScriptedServer(scenario)
    base = await server.start()
    sleeps: list[float] = []
    notices: list[dict[str, Any]] = []
    changed = asyncio.Event()

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    def on_notice(msg: dict[str, Any]) -> None:
        notices.append(msg)
        changed.set()

    headers = bakbak.TTS(api_key="test-key", http_session=session)._ws._headers
    client = _ws.WSClient(
        url=_ws_url(base),
        headers=headers,
        session=lambda: session,
        max_retries=2,
        sleep=record_sleep,
        on_notice=on_notice,
    )
    original_on_closed = client._on_closed

    def on_closed(sock: Any) -> None:
        original_on_closed(sock)
        changed.set()

    client._on_closed = on_closed  # type: ignore[method-assign]
    collectors: dict[str, _Collector] = {}
    connect_error: APIError | None = None

    def collector(context_id: str) -> _Collector:
        if context_id not in collectors:
            collectors[context_id] = _Collector(client.context(context_id, timeout=5), changed)
        return collectors[context_id]

    try:
        for op in scenario["client"]:
            kind = op["op"]
            if kind == "connect":
                try:
                    await client.connect(timeout=5)
                except APIError as e:
                    connect_error = e
            elif kind == "send":
                c = collector(op["context"])
                c.sent.append(c.ctx.send(op["request"]))
            elif kind == "wait_audio":
                c = collector(op["context"])
                await _until(changed, c.has_audio)
            elif kind == "wait_done":
                await _until(changed, collector(op["context"]).all_settled)
            elif kind == "cancel":
                await asyncio.wait_for(collector(op["context"]).ctx.cancel(), 5)
            elif kind == "wait_closed":
                await _until(changed, lambda: client.closes > 0)
            else:
                raise AssertionError(f"unknown client op {kind}")
        for c in collectors.values():
            c.ctx.end_input()
        await asyncio.wait_for(asyncio.gather(*(c.task for c in collectors.values())), 5)
    finally:
        await client.aclose()
        await server.stop()

    assert server.failures == []
    if "connect_error" in expect:
        assert connect_error is not None
        wanted = dict(expect["connect_error"])
        # aiohttp drops the body of a refused upgrade, so the gateway's request
        # id (which is only in that body) cannot be read; everything else is.
        if connect_error.body is None:
            wanted.pop("request_id", None)
        assert_error(connect_error, wanted, audio_bytes=0)
    else:
        assert connect_error is None
    for context_id, outcomes in expect.get("contexts", {}).items():
        c = collectors[context_id]
        assert len(c.sent) == len(outcomes)
        for request, wanted in zip(c.sent, outcomes, strict=True):
            kind, error = c.outcomes[id(request)]
            audio = bytes(c.audio.get(id(request), b""))
            assert kind == wanted["outcome"], (kind, error)
            assert len(audio) == wanted.get("audio_bytes", len(audio))
            if "audio_sha256" in wanted:
                assert hashlib.sha256(audio).hexdigest() == wanted["audio_sha256"]
            if kind == "done" and "request_id" in wanted:
                assert request.request_id == wanted["request_id"]
            if kind == "error":
                assert error is not None
                assert_error(error, wanted["error"], audio_bytes=len(audio))
    assert server.ws_connections == expect["connections"]
    if "connection_notices" in expect:
        assert len(notices) == expect["connection_notices"]
    assert_sleeps(sleeps, expect.get("sleeps", []))
