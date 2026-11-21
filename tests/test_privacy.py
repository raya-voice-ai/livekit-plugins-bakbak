"""BEHAVIOR.md §8: the API key is never reachable from errors, reprs or logs."""

from __future__ import annotations

import logging
import socket
import traceback
from typing import Any

import aiohttp
import pytest
from livekit.agents import APIConnectOptions, APIError

from livekit.plugins import bakbak
from livekit.plugins.bakbak import TTSTransport

from .fake_bakbak import FakeBakbak, Plan

KEY = "SUPERSECRETKEY123"
ONCE = APIConnectOptions(max_retry=0, timeout=2)


def reachable(obj: object, depth: int = 0, seen: set[int] | None = None) -> list[str]:
    """Where KEY shows up in anything reachable from `obj` (as review r4 walks it)."""
    seen = seen if seen is not None else set()
    if id(obj) in seen or depth > 6:
        return []
    seen.add(id(obj))
    hits: list[str] = []
    try:
        text = repr(obj)
    except Exception:
        text = ""
    if KEY in text:
        hits.append(f"{type(obj).__name__} at depth {depth}")
    for name in ("__cause__", "__context__", "request_info", "headers", "body", "args"):
        child = getattr(obj, name, None)
        if child is not None:
            hits += reachable(child, depth + 1, seen)
    return hits


def closed_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


async def failure(tts: bakbak.TTS) -> APIError:
    async def speak() -> None:
        async with tts.stream(conn_options=ONCE) as stream:
            stream.push_text("नमस्ते।")
            stream.end_input()
            async for _ in stream:
                pass

    with pytest.raises(APIError) as caught:
        await speak()
    return caught.value


@pytest.mark.parametrize("transport", ["websocket", "http"])
@pytest.mark.parametrize("server", ["refuses", "unreachable"])
async def test_the_key_is_not_reachable_from_errors(
    session: aiohttp.ClientSession,
    caplog: pytest.LogCaptureFixture,
    transport: TTSTransport,
    server: str,
) -> None:
    fake = FakeBakbak()
    await fake.start()
    fake.reject_ws = (401, {"message": "Unauthorized"})
    fake.plan = lambda _: Plan(refuse=(401, {"message": "Unauthorized"}))
    base = fake.base_url if server == "refuses" else closed_port()
    caplog.set_level(logging.DEBUG)
    tts = bakbak.TTS(api_key=KEY, base_url=base, transport=transport, http_session=session)
    error = await failure(tts)
    await tts.aclose()
    await fake.stop()

    assert reachable(error) == []
    assert KEY not in "".join(traceback.format_exception(error))
    assert KEY not in caplog.text


async def test_the_key_is_not_in_any_repr() -> None:
    tts = bakbak.TTS(api_key=KEY)
    objects: list[Any] = [tts, tts._opts, tts._ws, tts._tokenizer]
    assert all(KEY not in repr(o) for o in objects)


@pytest.mark.parametrize("key", [" ", "secret key", "secret\n", "secret\x00", "\tsecret"])
def test_malformed_keys_are_refused_without_echo(key: str) -> None:
    with pytest.raises(ValueError, match="whitespace or control characters") as caught:
        bakbak.TTS(api_key=key)
    assert "secret" not in str(caught.value)
