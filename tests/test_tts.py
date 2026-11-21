"""The plugin through LiveKit's TTS interface, against the behavioural fake server.

These check the invariants LiveKit's own plugin tests check (request ids,
``is_final``, PCM format, metrics, timeouts and retries, prewarm, interruption)
plus the plugin's transports: one request per sentence, reconnects, the HTTP
look-ahead, and the rule that audio is never replayed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp
import pytest
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
)
from livekit.agents import (
    tts as agents_tts,
)
from livekit.agents.metrics import TTSMetrics

from livekit.plugins import bakbak
from livekit.plugins.bakbak import _ws
from livekit.plugins.bakbak._segmenter import split_sentences

from .fake_bakbak import CAPACITY, CHUNK_SECONDS, DRAINING, BlackHole, FakeBakbak, Plan, Received

FAST = APIConnectOptions(max_retry=2, retry_interval=0.0, timeout=2)
TEXT = "नमस्ते, आप कैसे हैं? आपका पैकेज आज शाम तक पहुँच जाएगा। राहुल आपको कॉल करेगा।"
TRANSPORTS = ["websocket", "http"]


@pytest.fixture
async def fake() -> AsyncIterator[FakeBakbak]:
    server = FakeBakbak()
    await server.start()
    yield server
    await server.stop()


def make_tts(fake: FakeBakbak, session: aiohttp.ClientSession, **kwargs: Any) -> bakbak.TTS:
    return bakbak.TTS(api_key="test-key", base_url=fake.base_url, http_session=session, **kwargs)


async def run_stream(
    tts: bakbak.TTS, pieces: list[str], conn_options: APIConnectOptions = FAST
) -> list[agents_tts.SynthesizedAudio]:
    async with tts.stream(conn_options=conn_options) as stream:
        for piece in pieces:
            stream.push_text(piece)
        stream.end_input()
        return [ev async for ev in stream]


async def run_synthesize(
    tts: bakbak.TTS, text: str, conn_options: APIConnectOptions = FAST
) -> list[agents_tts.SynthesizedAudio]:
    async with tts.synthesize(text, conn_options=conn_options) as stream:
        return [ev async for ev in stream]


def request_order(events: list[agents_tts.SynthesizedAudio]) -> list[int]:
    """Which fake request each stretch of audio came from (the fake's samples say)."""
    order: list[int] = []
    for ev in events:
        for sample in ev.frame.data:
            if sample and (not order or order[-1] != sample):
                order.append(sample)
    return order


async def until(condition: Callable[[], bool], timeout: float = 3) -> None:
    async def wait() -> None:
        while not condition():  # noqa: ASYNC110 - polls state owned by the code under test
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


def collect(tts: bakbak.TTS, event: str) -> list[Any]:
    events: list[Any] = []
    tts.on(event, events.append)
    return events


# -- one request per sentence -------------------------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_stream_sends_each_sentence_as_its_own_request(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str
) -> None:
    tts = make_tts(fake, session, transport=transport)
    events = await run_stream(tts, list(TEXT))  # one code point at a time, like an LLM
    await tts.aclose()

    sentences = split_sentences(TEXT)
    assert len(sentences) == 3
    assert fake.texts() == sentences
    if transport == "websocket":
        assert fake.ws_connections == 1
        assert len({r.body["context_id"] for r in fake.received}) == 1
    # Audio arrives in sentence order, all of it, and only the last frame is final.
    assert request_order(events) == [1, 2, 3]
    assert [ev.is_final for ev in events].count(True) == 1
    assert events[-1].is_final
    duration = sum(ev.frame.duration for ev in events)
    assert duration == pytest.approx(3 * 5 * CHUNK_SECONDS, abs=1e-3)


async def test_first_sentence_leaves_before_the_input_ends(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    tts = make_tts(fake, session)
    async with tts.stream(conn_options=FAST) as stream:
        stream.push_text("नमस्ते, आप कैसे हैं? आपका")
        await until(lambda: len(fake.received) == 1)
        assert fake.texts() == ["नमस्ते, आप कैसे हैं?"]
        stream.push_text(" पैकेज आ गया।")
        stream.end_input()
        events = [ev async for ev in stream]
    await tts.aclose()
    assert fake.texts() == ["नमस्ते, आप कैसे हैं?", "आपका पैकेज आ गया।"]
    assert request_order(events) == [1, 2]


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_synthesize_sends_the_segmenter_sentences(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str
) -> None:
    tts = make_tts(fake, session, transport=transport)
    events = await run_synthesize(tts, TEXT)
    await tts.aclose()
    assert fake.texts() == split_sentences(TEXT)
    assert request_order(events) == [1, 2, 3]
    assert [ev.is_final for ev in events].count(True) == 1
    assert events[-1].is_final


# -- LiveKit invariants --------------------------------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("kind", ["stream", "synthesize"])
async def test_pcm_at_the_declared_rate(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str, kind: str
) -> None:
    tts = make_tts(fake, session, transport=transport, sample_rate=16000)
    if kind == "stream":
        events = await run_stream(tts, [TEXT])
    else:
        events = await run_synthesize(tts, TEXT)
    await tts.aclose()
    assert all(r.body["output_format"] == {
        "container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000
    } for r in fake.received)  # fmt: skip
    assert all(ev.frame.sample_rate == 16000 for ev in events)
    assert all(ev.frame.num_channels == 1 for ev in events)
    samples = sum(ev.frame.samples_per_channel for ev in events)
    assert samples == 3 * 5 * int(16000 * CHUNK_SECONDS)
    # One request id per attempt, on every frame.
    assert len({ev.request_id for ev in events}) == 1


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_stream_metrics(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str
) -> None:
    tts = make_tts(fake, session, transport=transport)
    metrics = collect(tts, "metrics_collected")
    await run_stream(tts, list(TEXT))
    await tts.aclose()
    assert len(metrics) == 1
    m: TTSMetrics = metrics[0]
    assert m.ttfb > 0
    assert m.characters_count == len(TEXT)
    assert m.audio_duration == pytest.approx(0.3, abs=1e-3)
    assert m.streamed is True
    assert m.metadata is not None
    assert m.metadata.model_provider == "Bakbak"
    assert m.metadata.model_name == "m1"
    if transport == "websocket":
        assert m.connection_reused is False  # this stream opened the socket
        assert m.acquire_time > 0


async def test_synthesize_metrics(fake: FakeBakbak, session: aiohttp.ClientSession) -> None:
    tts = make_tts(fake, session)
    metrics = collect(tts, "metrics_collected")
    await run_synthesize(tts, TEXT)
    await tts.aclose()
    assert len(metrics) == 1
    assert metrics[0].ttfb > 0
    assert metrics[0].characters_count == len(TEXT)
    assert metrics[0].streamed is False


async def test_prewarm_opens_exactly_one_socket(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    tts = make_tts(fake, session)
    metrics = collect(tts, "metrics_collected")
    tts.prewarm()
    tts.prewarm()
    await until(lambda: tts._ws._usable_socket() is not None)
    await run_stream(tts, [TEXT])
    await run_synthesize(tts, TEXT)
    await run_stream(tts, [TEXT])
    await tts.aclose()
    assert fake.ws_connections == 1
    assert [m.connection_reused for m in metrics] == [True, True, True]
    assert all(m.acquire_time == 0 for m in metrics)


async def test_prewarm_does_nothing_for_http(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    tts = make_tts(fake, session, transport="http")
    tts.prewarm()
    await asyncio.sleep(0.05)
    await tts.aclose()
    assert fake.ws_connections == 0


async def test_interruption_cancels_and_keeps_the_socket(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    fake.plan = lambda r: Plan(chunks=50, chunk_delay=0.02) if r.index <= 3 else Plan()
    tts = make_tts(fake, session)
    stream = tts.stream(conn_options=FAST)
    stream.push_text(TEXT)
    stream.end_input()
    first = await stream.__anext__()
    context_id = first.request_id
    await stream.aclose()  # the user interrupted the agent

    await until(lambda: len(fake.cancels()) == 1)
    assert fake.cancels()[0]["context_id"] == context_id
    # The next turn reuses the socket and hears none of the interrupted audio.
    events = await run_stream(tts, ["अगला वाक्य।"])
    await tts.aclose()
    assert fake.ws_connections == 1
    assert request_order(events) == [len(fake.received)]


async def test_stream_retries_a_refusal_on_a_fresh_context(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    fake.plan = lambda r: Plan(refuse=CAPACITY) if r.index == 1 else Plan()
    tts = make_tts(fake, session)
    errors = collect(tts, "error")
    events = await run_stream(tts, ["एक छोटा वाक्य।"])
    await tts.aclose()

    first, retry = fake.received
    assert first.body["text"] == retry.body["text"]
    assert first.body["context_id"] != retry.body["context_id"]
    # Request ids never interleave: every frame is the retry's.
    assert {ev.request_id for ev in events} == {retry.body["context_id"]}
    assert len(errors) == 1
    assert errors[0].recoverable is True
    assert isinstance(errors[0].error, APIStatusError)
    assert errors[0].error.status_code == 503


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_refusal_after_audio_is_not_retried(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str
) -> None:
    # The second sentence is refused after the first one's audio was delivered.
    fake.plan = lambda r: Plan(refuse=CAPACITY) if r.body["text"].startswith("आपका") else Plan()
    tts = make_tts(fake, session, transport=transport)
    errors = collect(tts, "error")
    with pytest.raises(APIStatusError) as caught:
        await run_stream(tts, [TEXT])
    await tts.aclose()
    assert caught.value.status_code == 503
    assert caught.value.retryable is False
    assert [e.recoverable for e in errors] == [False]
    assert fake.texts().count(split_sentences(TEXT)[0]) == 1  # never replayed


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_synthesize_never_replays_audio(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str
) -> None:
    # LiveKit's ChunkedStream retries any retryable error, even after audio; the
    # plugin marks errors after audio as not retryable (BEHAVIOR.md §4.1).
    fake.plan = lambda _: Plan(fail_after=2)
    tts = make_tts(fake, session, transport=transport)
    with pytest.raises(APIStatusError) as caught:
        await run_synthesize(tts, "एक छोटा वाक्य।")
    await tts.aclose()
    assert caught.value.status_code == 502
    assert caught.value.retryable is False
    assert len(fake.received) == 1


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("kind", ["stream", "synthesize"])
async def test_timeout(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str, kind: str
) -> None:
    fake.plan = lambda _: Plan(hang_after=0)
    tts = make_tts(fake, session, transport=transport)
    errors = collect(tts, "error")
    metrics = collect(tts, "metrics_collected")

    async def synthesis(conn_options: APIConnectOptions) -> None:
        if kind == "stream":
            await run_stream(tts, [TEXT], conn_options)
        else:
            await run_synthesize(tts, TEXT, conn_options)

    started = time.monotonic()
    with pytest.raises(APITimeoutError):
        await synthesis(APIConnectOptions(max_retry=0, timeout=0.5))
    assert 0.4 <= time.monotonic() - started <= 1.5

    started = time.monotonic()
    with pytest.raises(APITimeoutError):
        await synthesis(APIConnectOptions(max_retry=3, timeout=0.3, retry_interval=0.0))
    assert 1.0 <= time.monotonic() - started <= 2.5
    await tts.aclose()
    assert len(errors) == 1 + 4
    assert [e.recoverable for e in errors[1:]] == [True, True, True, False]
    assert metrics == []


async def test_handshake_timeout() -> None:
    hole = BlackHole()
    base = await hole.start()
    async with aiohttp.ClientSession() as session:
        tts = bakbak.TTS(api_key="test-key", base_url=base, http_session=session)
        errors = collect(tts, "error")
        started = time.monotonic()
        with pytest.raises(APITimeoutError):
            await run_stream(
                tts, [TEXT], APIConnectOptions(max_retry=2, timeout=0.3, retry_interval=0)
            )
        elapsed = time.monotonic() - started
        await tts.aclose()
    await hole.stop()
    assert 0.8 <= elapsed <= 2.0
    assert len(errors) == 3
    assert hole.connections == 3


async def test_handshake_refusal_is_not_retried(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    fake.reject_ws = (401, {"message": "Unauthorized", "request_id": "gw_1"})
    tts = make_tts(fake, session)
    with pytest.raises(APIStatusError) as caught:
        await run_stream(tts, [TEXT])
    await tts.aclose()
    assert caught.value.status_code == 401
    assert caught.value.retryable is False
    assert caught.value.message == "Unauthorized"
    assert fake.ws_connections == 1


# -- reconnects ----------------------------------------------------------------


async def test_draining_server_moves_the_rest_of_the_reply(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    sentences = split_sentences(TEXT)
    refused: set[str] = set()

    def plan(r: Received) -> Plan:
        # The server starts draining after the first sentence: it refuses the
        # rest on this connection and then closes it with 1001.
        text = r.body["text"]
        if r.index > 1 and text not in refused:
            refused.add(text)
            last = text == sentences[-1]
            return Plan(refuse=DRAINING, close_after_refusal=1001 if last else None)
        return Plan()

    fake.plan = plan
    tts = make_tts(fake, session)
    errors = collect(tts, "error")
    events = await run_stream(tts, [TEXT])
    await tts.aclose()
    assert errors == []
    assert fake.ws_connections == 2
    texts = {r.index: r.body["text"] for r in fake.received}
    assert [texts[i] for i in request_order(events)] == sentences


BILLING_DOWN = (
    503,
    {
        "message": "The service is temporarily not accepting requests. Retry shortly.",
        "error_code": "service_unavailable",
        "retryable": True,
    },
)


async def test_service_unavailable_is_an_ordinary_refusal(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    """`service_unavailable` without the reconnect phrase is not a drain (review r8b):
    no reconnect storm, and LiveKit sees the 503 and retries it."""
    fake.plan = lambda _: Plan(refuse=BILLING_DOWN)
    tts = make_tts(fake, session)
    errors = collect(tts, "error")
    started = time.monotonic()
    with pytest.raises(APIStatusError) as caught:
        await run_stream(tts, [TEXT], APIConnectOptions(max_retry=3, retry_interval=0, timeout=2))
    assert time.monotonic() - started < 1.0
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert [e.recoverable for e in errors] == [True, True, True, False]
    assert all(isinstance(e.error, APIStatusError) for e in errors)
    assert fake.ws_connections == 1
    assert len(fake.sockets) == 1

    fake.plan = lambda _: Plan()  # the outage is over
    events = await run_stream(tts, ["अगला वाक्य।"])
    await tts.aclose()
    assert events[-1].is_final
    assert fake.ws_connections == 1


async def test_a_draining_socket_is_closed_once_unused(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    # After the first sentence the server drains: like the Router it refuses
    # everything sent to it from then on, but it never closes the socket.
    refused: set[str] = set()

    def plan(r: Received) -> Plan:
        if r.index > 1 and r.body["text"] not in refused:
            refused.add(r.body["text"])
            return Plan(refuse=DRAINING)
        return Plan()

    fake.plan = plan
    tts = make_tts(fake, session)
    events = await run_stream(tts, [TEXT])
    await until(lambda: len(fake.sockets) == 1)  # the client closed the draining one
    assert len(tts._ws._sockets) == 1
    await tts.aclose()
    assert fake.ws_connections == 2
    texts = {r.index: r.body["text"] for r in fake.received}
    assert [texts[i] for i in request_order(events)] == split_sentences(TEXT)


async def test_dropped_socket_resends_requests_without_audio(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    # The connection drops while the second sentence waits for its first audio.
    fake.plan = lambda r: Plan(close_after=(0, 1011)) if r.index == 2 else Plan()
    tts = make_tts(fake, session)
    errors = collect(tts, "error")
    events = await run_stream(tts, [TEXT])
    await tts.aclose()
    assert errors == []
    assert fake.ws_connections == 2
    texts = {r.index: r.body["text"] for r in fake.received}
    assert [texts[i] for i in request_order(events)] == split_sentences(TEXT)


async def test_dropped_socket_mid_audio_fails_the_stream(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    fake.plan = lambda r: Plan(close_after=(2, 1011)) if r.index == 1 else Plan()
    tts = make_tts(fake, session)
    with pytest.raises(APIStatusError) as caught:
        await run_stream(tts, [TEXT])
    await tts.aclose()
    assert caught.value.status_code == -1
    assert caught.value.request_id == "req_1"
    assert caught.value.retryable is False


async def test_idle_close_reconnects_on_the_next_turn(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    tts = make_tts(fake, session)
    await run_stream(tts, [TEXT])
    await fake.sockets[0].close(code=1000, message=b"idle timeout")
    await until(lambda: tts._ws._socket is None)
    events = await run_stream(tts, [TEXT])
    await tts.aclose()
    assert fake.ws_connections == 2
    assert request_order(events) == [4, 5, 6]


async def test_pings_are_answered(fake: FakeBakbak, session: aiohttp.ClientSession) -> None:
    fake.heartbeat = 0.1  # the server closes a client that does not answer in time
    tts = make_tts(fake, session)
    tts.prewarm()
    await until(lambda: tts._ws._usable_socket() is not None)
    await asyncio.sleep(0.5)
    assert len(fake.sockets) == 1
    await run_stream(tts, [TEXT])
    await tts.aclose()
    assert fake.ws_connections == 1


async def test_a_socket_that_went_quiet_is_replaced(
    fake: FakeBakbak, session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_ws, "STALE_AFTER", 0.2)  # this fake does not ping
    tts = make_tts(fake, session)
    tts.prewarm()
    await until(lambda: tts._ws._usable_socket() is not None)
    await asyncio.sleep(0.3)
    await run_stream(tts, [TEXT])
    await until(lambda: len(fake.sockets) == 1)  # the quiet one was closed
    await tts.aclose()
    assert fake.ws_connections == 2


# -- the HTTP transport --------------------------------------------------------


async def test_http_look_ahead_keeps_audio_in_order(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    # The first sentence is slow; the second one's request runs alongside it.
    fake.plan = lambda r: Plan(chunk_delay=0.03) if r.index == 1 else Plan()
    tts = make_tts(fake, session, transport="http")
    events = await run_synthesize(tts, TEXT)
    await tts.aclose()
    assert fake.http_max_active == 2
    assert request_order(events) == [1, 2, 3]


async def test_http_look_ahead_refusal_is_sent_again_in_turn(
    fake: FakeBakbak, session: aiohttp.ClientSession
) -> None:
    sentences = split_sentences(TEXT)
    # A server with room for one request at a time refuses the look-ahead.
    fake.plan = lambda r: Plan(refuse=CAPACITY) if fake.http_active > 1 else Plan(chunk_delay=0.02)
    tts = make_tts(fake, session, transport="http")
    errors = collect(tts, "error")
    events = await run_synthesize(tts, TEXT)
    await tts.aclose()
    assert errors == []
    texts = {r.index: r.body["text"] for r in fake.received}
    assert [texts[i] for i in request_order(events)] == sentences


# -- text with nothing to say --------------------------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_text_with_nothing_speakable_is_silence(
    fake: FakeBakbak, session: aiohttp.ClientSession, transport: str
) -> None:
    tts = make_tts(fake, session, transport=transport)
    errors = collect(tts, "error")
    events = await run_stream(tts, ["...", " ।"])
    synthesized = await run_synthesize(tts, "…")
    await tts.aclose()
    assert fake.received == []
    assert errors == []
    for frames in (events, synthesized):
        assert sum(ev.frame.duration for ev in frames) == pytest.approx(0.02)
        assert frames[-1].is_final


async def test_empty_stream(fake: FakeBakbak, session: aiohttp.ClientSession) -> None:
    tts = make_tts(fake, session)
    events = await run_stream(tts, [])
    await tts.aclose()
    assert events == []
    assert fake.received == []


async def test_text_pacing(fake: FakeBakbak, session: aiohttp.ClientSession) -> None:
    tts = make_tts(fake, session, text_pacing=True)
    events = await run_stream(tts, list(TEXT))
    await tts.aclose()
    assert " ".join(fake.texts()) == " ".join(split_sentences(TEXT))
    assert events[-1].is_final


# -- options and requests ------------------------------------------------------


async def test_request_body_and_headers(fake: FakeBakbak, session: aiohttp.ClientSession) -> None:
    tts = make_tts(
        fake,
        session,
        voice_id="v1",
        language="hi-IN",
        sample_rate=8000,
        extra_body={"pronunciation": "x", "text": "never", "output_format": "never"},
    )
    await run_stream(tts, ["एक।"])
    tts.update_options(voice_id="v2", language="en-US", model="standard", speed=1.2)
    await run_stream(tts, ["Two."])
    await tts.aclose()
    first, second = fake.received
    assert first.body == {
        "pronunciation": "x",
        "text": "एक।",
        "voice_id": "v1",
        "language": "hi",
        "model": "m1",
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 8000},
        "context_id": first.body["context_id"],
    }
    assert second.body["voice_id"] == "v2"
    assert second.body["language"] == "en-us"
    assert second.body["model"] == "standard"
    assert second.body["speed"] == 1.2
    headers = fake.ws_headers[0]
    assert headers["X-API-Key"] == "test-key"
    assert headers["User-Agent"].startswith(f"livekit-plugins-bakbak/{bakbak.__version__} python/")
    assert headers["X-Bakbak-Client"] == f"livekit-plugins-bakbak/{bakbak.__version__}"


async def test_http_request_headers(fake: FakeBakbak, session: aiohttp.ClientSession) -> None:
    tts = make_tts(fake, session, transport="http")
    await run_synthesize(tts, "एक।")
    await tts.aclose()
    headers = fake.received[0].headers
    assert headers["X-API-Key"] == "test-key"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Bakbak-Client"] == f"livekit-plugins-bakbak/{bakbak.__version__}"


async def test_list_voices(fake: FakeBakbak, session: aiohttp.ClientSession) -> None:
    tts = make_tts(fake, session)
    assert len(await tts.list_voices()) == 4
    assert [v.id for v in await tts.list_voices(model="m1", language="hi-IN")] == [
        "009ae8b5-4a03-492d-af1c-3055989ac734"
    ]
    assert [v.name for v in await tts.list_voices(language="en-IN")] == ["Standard EN-IN"]
    bad = bakbak.TTS(api_key="wrong", base_url=fake.base_url, http_session=session)
    with pytest.raises(APIStatusError) as caught:
        await bad.list_voices()
    assert caught.value.status_code == 401
    await tts.aclose()


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("hi", "hi"),
        ("hi-IN", "hi"),
        ("hi_IN", "hi"),
        ("Hindi", "hi"),
        ("marathi", "mr"),
        ("en", "en"),
        ("en-IN", "en-in"),
        ("en-us", "en-us"),
        ("en-GB", "en"),
        ("ta-IN", "ta"),
        ("or", "or"),
    ],
)
def test_language_codes(given: str, sent: str) -> None:
    assert bakbak.TTS(api_key="k", language=given)._opts.language == sent


def test_model_and_provider() -> None:
    tts = bakbak.TTS(api_key="k", model="standard")
    assert tts.model == "standard"
    assert tts.provider == "Bakbak"
    assert tts.sample_rate == 24000
    assert tts.num_channels == 1
    assert tts.capabilities.streaming is True


def test_api_key_and_base_url_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ValueError, match=r"api_key.*BAKBAK_API_KEY"):
        bakbak.TTS()

    monkeypatch.setenv("BAKBAK_API_KEY", "from-env")
    monkeypatch.setenv("BAKBAK_BASE_URL", "https://staging.example/")
    tts = bakbak.TTS()
    assert tts._opts.api_key == "from-env"
    assert tts._opts.base_url == "https://staging.example"
    assert tts._ws._url == "wss://staging.example/v1/text-to-speech/ws"
    assert bakbak.TTS(api_key="explicit")._opts.api_key == "explicit"

    monkeypatch.delenv("BAKBAK_API_KEY")
    monkeypatch.delenv("BAKBAK_BASE_URL")
    monkeypatch.setenv("RAYA_API_KEY", "legacy")
    monkeypatch.setattr("livekit.plugins.bakbak.tts._warned_legacy_env", set())
    with caplog.at_level(logging.WARNING, logger="livekit.plugins.bakbak"):
        assert bakbak.TTS()._opts.api_key == "legacy"
        assert bakbak.TTS()._opts.api_key == "legacy"
    warnings = [r for r in caplog.records if "RAYA_API_KEY is deprecated" in r.getMessage()]
    assert len(warnings) == 1
    assert bakbak.TTS()._opts.base_url == "https://hub.getraya.app"


def test_invalid_options() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        bakbak.TTS(api_key="k", sample_rate=44100)
    with pytest.raises(ValueError, match="transport"):
        bakbak.TTS(api_key="k", transport="grpc")  # type: ignore[arg-type]


async def test_notices_are_logged(
    fake: FakeBakbak, session: aiohttp.ClientSession, caplog: pytest.LogCaptureFixture
) -> None:
    tts = make_tts(fake, session)
    tts.prewarm()
    await until(lambda: len(fake.sockets) == 1)
    with caplog.at_level(logging.WARNING, logger="livekit.plugins.bakbak"):
        await fake.sockets[0].send_json(
            {"type": "error", "status_code": 400, "done": True, "message": "binary frame"}
        )
        await until(lambda: any("notice" in r.getMessage() for r in caplog.records))
    await run_stream(tts, [TEXT])  # the socket is unaffected
    await tts.aclose()
    assert fake.ws_connections == 1


# -- cleanup under cancellation (review r10, r2) ------------------------------

NO_CREDITS = (
    402,
    {"message": "Insufficient credits.", "error_code": "insufficient_credits", "retryable": False},
)


@pytest.mark.parametrize("delay", range(8))
async def test_interruption_while_an_error_is_handled_still_cancels(
    fake: FakeBakbak,
    session: aiohttp.ClientSession,
    monkeypatch: pytest.MonkeyPatch,
    delay: int,
) -> None:
    """The user barges in `delay` loop iterations after sentence 2 was refused,
    while sentence 3 is streaming: the cancel must still be sent."""

    def plan(r: Received) -> Plan:
        if r.index == 2:
            return Plan(refuse=NO_CREDITS)
        return Plan(chunks=300, chunk_delay=0.005) if r.index == 3 else Plan(chunks=3)

    fake.plan = plan
    tts = make_tts(fake, session)
    stream = tts.stream(conn_options=APIConnectOptions(max_retry=0, timeout=2))
    emit = _ws.WSContext._emit
    interruptions: list[asyncio.Future[None]] = []

    def emit_then_interrupt(ctx: _ws.WSContext, event: _ws.WSEvent) -> None:
        emit(ctx, event)
        if isinstance(event, _ws.WSFailed):

            async def interrupt() -> None:
                for _ in range(delay):
                    await asyncio.sleep(0)
                await stream.aclose()

            interruptions.append(asyncio.ensure_future(interrupt()))

    monkeypatch.setattr(_ws.WSContext, "_emit", emit_then_interrupt)
    stream.push_text(TEXT + " फिर")  # no end_input: the LLM is still writing
    with contextlib.suppress(APIStatusError):
        async for _ in stream:
            pass
    await asyncio.gather(*interruptions)
    await until(lambda: len(fake.cancels()) == 1)
    await until(lambda: not tts._ws._contexts)
    await tts.aclose()


@pytest.mark.parametrize("delay", range(6))
async def test_a_second_cancellation_during_cleanup_still_cancels(
    fake: FakeBakbak, session: aiohttp.ClientSession, delay: int
) -> None:
    fake.plan = lambda _: Plan(chunks=200, chunk_delay=0.01)
    tts = make_tts(fake, session, text_pacing=True)
    stream = tts.stream(conn_options=FAST)
    stream.push_text(TEXT)
    stream.end_input()
    await stream.__anext__()
    closing = asyncio.ensure_future(stream.aclose())  # e.g. the agent's interruption
    for _ in range(delay):
        await asyncio.sleep(0)
    stream._task.cancel()  # e.g. a second aclose() from a session shutdown
    await closing
    await until(lambda: len(fake.cancels()) == 1)
    await until(lambda: not tts._ws._contexts)
    await tts.aclose()


async def test_a_reconnect_that_fails_with_any_error_fails_the_stream(fake: FakeBakbak) -> None:
    """The session is closed while a dropped request waits to be resent (review r5):
    the stream fails promptly instead of hanging."""
    session = aiohttp.ClientSession()
    fake.plan = lambda r: Plan(first_delay=0.3, close_after=(0, 1011)) if r.index == 1 else Plan()
    tts = make_tts(fake, session)
    stream = tts.stream(conn_options=APIConnectOptions(max_retry=0, timeout=2))
    stream.push_text("नमस्ते, आप कैसे हैं?")
    stream.end_input()

    async def consume() -> None:
        async for _ in stream:
            pass

    task = asyncio.ensure_future(consume())
    await until(lambda: len(fake.received) == 1)
    await session.close()  # e.g. the job's shared session closes
    started = time.monotonic()
    with pytest.raises(APIConnectionError):
        await asyncio.wait_for(task, 3)
    assert time.monotonic() - started < 1.5
    await stream.aclose()
    await tts.aclose()
