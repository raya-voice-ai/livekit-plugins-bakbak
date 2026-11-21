"""Live tests against a real Bakbak deployment. They spend credits.

Run them on purpose, one at a time:

    BAKBAK_API_KEY=... BAKBAK_BASE_URL=https://... pytest -m live -s

They are skipped when either variable is unset. Keep texts short: a deployment
may serve one request per model at a time, and overlapping requests are refused
with 503 (which LiveKit then retries; that is part of what is tested).
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import AsyncIterator

import aiohttp
import pytest
from livekit.agents import APIConnectOptions
from livekit.agents import tts as agents_tts

from livekit.plugins import bakbak
from livekit.plugins.bakbak._segmenter import split_sentences

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("BAKBAK_API_KEY") and os.environ.get("BAKBAK_BASE_URL")),
        reason="needs BAKBAK_API_KEY and BAKBAK_BASE_URL",
    ),
]

SAMPLE_RATE = 24000
SHORT = "नमस्ते, आप कैसे हैं?"
REPLY = "नमस्ते! आपका पैकेज आज शाम तक पहुँच जाएगा। राहुल आपको पहले कॉल करेगा।"
CONN = APIConnectOptions(max_retry=3, retry_interval=1.0, timeout=15)


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


def live_tts(session: aiohttp.ClientSession, **kwargs: object) -> bakbak.TTS:
    return bakbak.TTS(http_session=session, sample_rate=SAMPLE_RATE, **kwargs)  # type: ignore[arg-type]


def duration(events: list[agents_tts.SynthesizedAudio]) -> float:
    return sum(ev.frame.duration for ev in events)


def check_frames(events: list[agents_tts.SynthesizedAudio]) -> None:
    assert events, "no audio"
    assert all(ev.frame.sample_rate == SAMPLE_RATE for ev in events)
    assert all(ev.frame.num_channels == 1 for ev in events)
    assert [ev.is_final for ev in events].count(True) == 1
    assert events[-1].is_final
    assert len({ev.request_id for ev in events}) == 1


async def test_list_voices(session: aiohttp.ClientSession) -> None:
    tts = live_tts(session)
    voices = await tts.list_voices()
    hindi_m1 = await tts.list_voices(model="m1", language="hi-IN")
    await tts.aclose()
    assert len(voices) > 0
    assert any(v.id == tts._opts.voice_id for v in hindi_m1)
    assert all(v.model == "m1" and v.language == "hi" for v in hindi_m1)


@pytest.mark.parametrize("transport", ["websocket", "http"])
async def test_synthesize(session: aiohttp.ClientSession, transport: str) -> None:
    tts = live_tts(session, transport=transport)
    metrics: list[object] = []
    tts.on("metrics_collected", metrics.append)
    async with tts.synthesize(REPLY, conn_options=CONN) as stream:
        events = [ev async for ev in stream]
    await tts.aclose()
    check_frames(events)
    # Three sentences of Hindi: a few seconds of speech, not a truncated stub.
    assert 2.0 < duration(events) < 20.0
    assert len(metrics) == 1


@pytest.mark.parametrize("transport", ["websocket", "http"])
async def test_stream_token_by_token(session: aiohttp.ClientSession, transport: str) -> None:
    """Text arrives like LLM output; audio must start before the text ends."""
    tts = live_tts(session, transport=transport)
    tts.prewarm()
    tokens = [REPLY[i : i + 3] for i in range(0, len(REPLY), 3)]
    first_frame_at: float | None = None
    text_done_at = 0.0
    events: list[agents_tts.SynthesizedAudio] = []
    async with tts.stream(conn_options=CONN) as stream:

        async def feed() -> None:
            nonlocal text_done_at
            for token in tokens:
                stream.push_text(token)
                await asyncio.sleep(0.04)  # ~75 tokens/s, a fast LLM
            text_done_at = time.perf_counter()
            stream.end_input()

        feeder = asyncio.create_task(feed())
        started = time.perf_counter()
        async for ev in stream:
            if first_frame_at is None:
                first_frame_at = time.perf_counter()
            events.append(ev)
        await feeder
    await tts.aclose()

    check_frames(events)
    assert first_frame_at is not None
    assert first_frame_at < text_done_at, "audio should start while text is still arriving"
    assert 2.0 < duration(events) < 20.0
    print(
        f"\n{transport}: first frame {1000 * (first_frame_at - started):.0f} ms after the first "
        f"token; text took {1000 * (text_done_at - started):.0f} ms; "
        f"{duration(events):.2f} s of audio in {len(split_sentences(REPLY))} sentences"
    )


async def test_interruption_mid_utterance(session: aiohttp.ClientSession) -> None:
    tts = live_tts(session)
    stream = tts.stream(conn_options=CONN)
    stream.push_text(REPLY)
    stream.end_input()
    first = await stream.__anext__()
    socket = tts._ws._socket
    await stream.aclose()  # the caller interrupted

    # The next turn reuses the socket, and none of the interrupted audio leaks in.
    async with tts.stream(conn_options=CONN) as stream:
        stream.push_text(SHORT)
        stream.end_input()
        events = [ev async for ev in stream]
    reused = tts._ws._socket is socket
    await tts.aclose()
    check_frames(events)
    assert first.request_id not in {ev.request_id for ev in events}
    assert reused


async def test_socket_stays_open_between_turns(session: aiohttp.ClientSession) -> None:
    """The server pings every 30 s; an idle socket must answer and stay usable."""
    tts = live_tts(session)
    tts.prewarm()
    await asyncio.sleep(1)
    socket = tts._ws._usable_socket()
    assert socket is not None
    await asyncio.sleep(40)
    assert time.monotonic() - socket.last_heard < 35, "no ping heard from the server"
    async with tts.stream(conn_options=CONN) as stream:
        stream.push_text(SHORT)
        stream.end_input()
        events = [ev async for ev in stream]
    reused = tts._ws._socket is socket
    await tts.aclose()
    check_frames(events)
    assert reused


@pytest.mark.parametrize("transport", ["websocket", "http"])
async def test_time_to_first_audio(session: aiohttp.ClientSession, transport: str) -> None:
    """TTFA: from the first push_text to the first frame, warm connection, N=15."""
    tts = live_tts(session, transport=transport)
    tts.prewarm()
    samples: list[float] = []
    for i in range(16):  # the first warms up the connection and is not counted
        async with tts.stream(conn_options=CONN) as stream:
            started = time.perf_counter()
            stream.push_text(SHORT)
            stream.end_input()
            first = await stream.__anext__()
            ttfa = time.perf_counter() - started
            async for _ in stream:
                pass
        assert first.frame.sample_rate == SAMPLE_RATE
        if i:
            samples.append(ttfa * 1000)
        await asyncio.sleep(0.2)  # one request at a time on the server
    await tts.aclose()
    samples.sort()
    p50 = statistics.median(samples)
    p90 = samples[int(0.9 * (len(samples) - 1))]
    print(
        f"\nTTFA {transport}: p50 {p50:.1f} ms, min {samples[0]:.1f}, p90 {p90:.1f}, "
        f"max {samples[-1]:.1f} (n={len(samples)})"
    )
    assert p50 < 2000
