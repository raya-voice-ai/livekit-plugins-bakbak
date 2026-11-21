from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture(autouse=True)
def _no_ambient_settings(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never pick up a developer's real key or base URL."""
    if request.node.get_closest_marker("live") is None:
        for name in ("BAKBAK_API_KEY", "BAKBAK_BASE_URL", "RAYA_API_KEY", "RAYA_BASE_URL"):
            monkeypatch.delenv(name, raising=False)


class _RecordingAsyncio:
    """Stands in for ``asyncio`` inside LiveKit's TTS module: records retry sleeps."""

    def __init__(self, sleeps: list[float]) -> None:
        self._sleeps = sleeps

    async def sleep(self, delay: float, result: Any = None) -> Any:
        self._sleeps.append(delay)
        await asyncio.sleep(0)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


@pytest.fixture
def livekit_retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits LiveKit's base classes ask for between attempts, without waiting."""
    sleeps: list[float] = []
    module = importlib.import_module("livekit.agents.tts.tts")
    monkeypatch.setattr(module, "asyncio", _RecordingAsyncio(sleeps))
    return sleeps
