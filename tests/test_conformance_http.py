"""conformance/http/*.json against the plugin.

Each scenario runs at two levels:

1. Transport: every exchange is one attempt of the plugin's SSE transport
   (`_http.stream_sse`) with the scenario's exact request body. An attempt the
   scenario retries must fail with a retryable error; the last one must give
   the expected result or error.
2. LiveKit: the whole scenario through ``tts.synthesize()`` with
   ``transport="http"``, so LiveKit's ChunkedStream does the retrying (§9). The
   plugin always asks for ``raw``/``pcm_s16le`` at its sample rate (§9), so the
   expected request body is the scenario's with that ``output_format``.
   Scenarios whose request the plugin cannot produce are skipped with the reason.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import aiohttp
import pytest
from livekit.agents import APIConnectOptions, APIError

from livekit.plugins import bakbak
from livekit.plugins.bakbak import _http
from livekit.plugins.bakbak._segmenter import split_sentences

from .conformance_support import assert_error, load
from .scripted_server import ScriptedServer

SCENARIOS = load("http")
IDS = [s["id"] for s in SCENARIOS]


def _whole_file_reason(scenario: dict[str, Any]) -> str | None:
    if scenario["call"]["method"] != "generate":
        return None
    if any("body_base64" in ex["response"] for ex in scenario["exchanges"]):
        return "the plugin has no whole-file call: synthesize() streams (BEHAVIOR.md §2, §9)"
    return None


def _as_stream(exchange: dict[str, Any]) -> dict[str, Any]:
    """A whole-file exchange that ends in a JSON error, sent to the stream endpoint.

    The plugin has no whole-file call, but both endpoints share one error
    contract (BEHAVIOR.md §3.4), so the refusal is checked on the endpoint the
    plugin uses."""
    exchange = copy.deepcopy(exchange)
    exchange["expect_request"]["path"] = _http.STREAM_PATH
    return exchange


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
async def test_transport(scenario: dict[str, Any], session: aiohttp.ClientSession) -> None:
    if reason := _whole_file_reason(scenario):
        pytest.skip(reason)
    call = scenario["call"]
    expect = scenario["expect"]

    if call["method"] == "voices.list":
        server = ScriptedServer(scenario)
        base = await server.start()
        try:
            tts = bakbak.TTS(api_key="test-key", base_url=base, http_session=session)
            voices = await tts.list_voices(**call["params"])
        finally:
            await server.stop()
        assert server.failures == []
        assert server.http_attempts == len(scenario["exchanges"])
        assert len(voices) == expect["result"]["count"]
        assert [v.id for v in voices] == expect["result"]["ids"]
        assert expect["sleeps"] == []
        return

    exchanges = scenario["exchanges"]
    if call["method"] == "generate":
        exchanges = [_as_stream(ex) for ex in exchanges]
    for index, exchange in enumerate(exchanges):
        last = index == len(exchanges) - 1
        server = ScriptedServer({"exchanges": [exchange]})
        base = await server.start()
        chunks: list[bytes] = []
        error: APIError | None = None
        result: _http.SSEResult | None = None
        try:
            tts = bakbak.TTS(api_key="test-key", base_url=base, http_session=session)
            result = await _http.stream_sse(
                session,
                base + _http.STREAM_PATH,
                headers=tts._sse_headers,
                body=json.dumps(call["params"]).encode(),
                timeout=5,
                on_audio=chunks.append,
            )
        except APIError as e:
            error = e
        finally:
            await server.stop()
        assert server.failures == []
        assert server.http_attempts == 1
        audio = b"".join(chunks)

        if not last:
            assert error is not None, "the scenario retries this attempt, so it must fail"
            assert error.retryable, f"the scenario retries this attempt: {error!r}"
            continue
        if "error" in expect:
            assert error is not None, f"expected {expect['error']['class']}"
            assert_error(error, expect["error"], audio_bytes=len(audio))
        else:
            assert error is None, repr(error)
            assert result is not None
            _assert_result(expect["result"], audio, result)


def _assert_result(expected: dict[str, Any], audio: bytes, result: _http.SSEResult) -> None:
    if "audio_bytes" in expected:
        assert len(audio) == expected["audio_bytes"]
        assert result.audio_bytes == expected["audio_bytes"]
    if "audio_sha256" in expected:
        assert hashlib.sha256(audio).hexdigest() == expected["audio_sha256"]
    if "chunks" in expected:
        assert result.chunks == expected["chunks"]
    if "request_id" in expected:
        assert result.request_id == expected["request_id"]


def _plugin_reason(scenario: dict[str, Any]) -> str | None:
    """Why the plugin cannot send this scenario's request, or None if it can."""
    if reason := _whole_file_reason(scenario):
        return reason
    call = scenario["call"]
    if call["method"] == "voices.list":
        return "test_transport runs it through the plugin's own list_voices()"
    if call["method"] == "generate":
        return "the plugin has no whole-file call; test_transport checks the refusal"
    params = call["params"]
    if "voice_id" not in params:
        return "the plugin always sends a voice_id"
    if params.get("output_format", {}).get("container") != "raw":
        return "the plugin always streams raw pcm_s16le (BEHAVIOR.md §9)"
    if split_sentences(params["text"]) != [params["text"]]:
        return "the plugin sends the segmenter's sentences, and this text is not one sentence"
    unknown = set(params) - {"text", "voice_id", "model", "language", "output_format", "speed"}
    if unknown:
        return f"the plugin does not send {sorted(unknown)}"
    return None


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
async def test_through_livekit(
    scenario: dict[str, Any], session: aiohttp.ClientSession, livekit_retry_sleeps: list[float]
) -> None:
    if reason := _plugin_reason(scenario):
        pytest.skip(reason)
    params = scenario["call"]["params"]
    sample_rate = params["output_format"].get("sample_rate", 24000)
    scenario = copy.deepcopy(scenario)
    for exchange in scenario["exchanges"]:
        exchange["expect_request"]["json"]["output_format"] = {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": sample_rate,
        }

    server = ScriptedServer(scenario)
    base = await server.start()
    conn_options = APIConnectOptions(max_retry=2, retry_interval=1.0, timeout=5)
    tts = bakbak.TTS(
        api_key="test-key",
        base_url=base,
        transport="http",
        voice_id=params["voice_id"],
        language=params["language"],
        model=params["model"],
        sample_rate=sample_rate,
        http_session=session,
    )
    frames: list[bytes] = []
    error: APIError | None = None
    try:
        async with tts.synthesize(params["text"], conn_options=conn_options) as stream:
            async for ev in stream:
                assert ev.frame.sample_rate == sample_rate
                frames.append(ev.frame.data.tobytes())
    except APIError as e:
        error = e
    finally:
        await tts.aclose()
        await server.stop()

    assert server.failures == []
    attempts = len(scenario["exchanges"])
    assert server.http_attempts == attempts
    # LiveKit decides the waits between attempts (§9): its own schedule, not
    # Retry-After or the backoff of §4.3 that the fixture's `sleeps` describe.
    assert livekit_retry_sleeps == [
        conn_options._interval_for_retry(i) for i in range(attempts - 1)
    ]

    expect = scenario["expect"]
    if "error" in expect:
        assert error is not None, f"expected {expect['error']['class']}"
        # Frames already delivered before a failure are LiveKit's to drop, so
        # the delivered audio is not compared here (test_transport does); the
        # scenario's own count still decides whether the error may be retried.
        wanted = {k: v for k, v in expect["error"].items() if k != "audio_bytes"}
        assert_error(error, wanted, audio_bytes=expect["error"].get("audio_bytes", 0))
    else:
        assert error is None, repr(error)
        audio = b"".join(frames)
        result = expect["result"]
        assert len(audio) == result["audio_bytes"]
        if "audio_sha256" in result:
            assert hashlib.sha256(audio).hexdigest() == result["audio_sha256"]


async def test_whole_file_refusals_before_audio(session: aiohttp.ClientSession) -> None:
    """err_502_legacy_backoff_then_ok ends with a whole-file response the plugin
    cannot request, but its refusals (legacy bodies, no ``retryable`` field) are
    the stream endpoint's too: each must be retryable by its status (§3.3)."""
    scenario = next(s for s in SCENARIOS if s["id"] == "err_502_legacy_backoff_then_ok")
    refusals = [_as_stream(ex) for ex in scenario["exchanges"] if "json" in ex["response"]]
    assert [ex["response"]["status"] for ex in refusals] == [502, 504]
    for exchange in refusals:
        server = ScriptedServer({"exchanges": [exchange]})
        base = await server.start()
        tts = bakbak.TTS(api_key="test-key", base_url=base, http_session=session)
        try:
            with pytest.raises(APIError) as caught:
                await _http.stream_sse(
                    session,
                    base + _http.STREAM_PATH,
                    headers=tts._sse_headers,
                    body=json.dumps(scenario["call"]["params"]).encode(),
                    timeout=5,
                    on_audio=lambda _: None,
                )
        finally:
            await server.stop()
        assert server.failures == []
        status = exchange["response"]["status"]
        assert_error(
            caught.value,
            {
                "class": "InternalServerError",
                "status": status,
                "error_code": None,
                "retryable": True,
                "request_id": exchange["response"]["headers"]["x-request-id"],
                "message": exchange["response"]["json"]["detail"],
            },
            audio_bytes=0,
        )
