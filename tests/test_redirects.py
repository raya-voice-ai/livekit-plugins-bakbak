"""BEHAVIOR.md §8: redirects are never followed, so the key never reaches another origin."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web
from livekit.agents import APIConnectOptions, APIStatusError

from livekit.plugins import bakbak
from livekit.plugins.bakbak import TTSTransport


async def serve(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app, handle_signals=False, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert isinstance(server, asyncio.Server)
    return runner, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


@pytest.fixture
async def origins() -> AsyncIterator[tuple[str, list[dict[str, str]]]]:
    """The API's origin, which redirects everything, and what reached the other one."""
    reached: list[dict[str, str]] = []

    async def elsewhere(request: web.Request) -> web.StreamResponse:
        reached.append(dict(request.headers))
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close()
            return ws
        return web.Response(status=200, text="")

    other = web.Application()
    other.router.add_route("*", "/{tail:.*}", elsewhere)
    other_runner, other_url = await serve(other)

    async def redirect(request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(f"{other_url}{request.path}", headers={"X-Request-Id": "req_302"})

    api = web.Application()
    api.router.add_route("*", "/{tail:.*}", redirect)
    api_runner, api_url = await serve(api)
    yield api_url, reached
    await api_runner.cleanup()
    await other_runner.cleanup()


@pytest.mark.parametrize("transport", ["websocket", "http"])
async def test_synthesis_does_not_follow_a_redirect(
    origins: tuple[str, list[dict[str, str]]], transport: TTSTransport
) -> None:
    api_url, reached = origins
    async with aiohttp.ClientSession() as session:
        tts = bakbak.TTS(
            api_key="test-key", base_url=api_url, transport=transport, http_session=session
        )

        async def speak() -> None:
            async with tts.stream(conn_options=APIConnectOptions(max_retry=2)) as stream:
                stream.push_text("नमस्ते।")
                stream.end_input()
                async for _ in stream:
                    pass

        with pytest.raises(APIStatusError) as caught:
            await speak()
        await tts.aclose()
    assert caught.value.status_code == 302
    assert caught.value.retryable is False
    assert caught.value.request_id == "req_302"
    assert reached == []


async def test_the_voice_list_does_not_follow_a_redirect(
    origins: tuple[str, list[dict[str, str]]],
) -> None:
    api_url, reached = origins
    async with aiohttp.ClientSession() as session:
        tts = bakbak.TTS(api_key="test-key", base_url=api_url, http_session=session)
        with pytest.raises(APIStatusError) as caught:
            await tts.list_voices()
        await tts.aclose()
    assert caught.value.status_code == 302
    assert reached == []
