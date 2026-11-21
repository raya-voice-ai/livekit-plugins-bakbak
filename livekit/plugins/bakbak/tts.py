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

"""Bakbak text-to-speech for LiveKit Agents."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import weakref
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIError,
    tokenize,
    tts,
    utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given

from . import _errors, _http
from ._ws import WS_PATH, WSAudio, WSClient, WSDone, WSFailed
from .log import logger
from .models import (
    DEFAULT_BASE_URL,
    DEFAULT_LANGUAGE,
    DEFAULT_MODEL,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOICE_ID,
    SUPPORTED_SAMPLE_RATES,
    TTSLanguages,
    TTSModels,
    TTSTransport,
    normalize_language,
)
from .tokenizer import SentenceTokenizer
from .version import __version__

__all__ = ["TTS", "ChunkedStream", "SynthesizeStream", "Voice"]

NUM_CHANNELS = 1
_MIME_TYPE = "audio/pcm"
_CLIENT = f"livekit-plugins-bakbak/{__version__}"
_USER_AGENT = (
    f"{_CLIENT} python/{platform.python_version()} "
    f"{platform.system().lower()}/{platform.machine().lower()}"
)
_LEGACY_ENV = {"BAKBAK_API_KEY": "RAYA_API_KEY", "BAKBAK_BASE_URL": "RAYA_BASE_URL"}
_warned_legacy_env: set[str] = set()


@dataclass
class Voice:
    """A voice from ``GET /v1/voices``. A voice works only with its own ``model``."""

    id: str
    name: str
    language: str
    model: str


@dataclass
class _TTSOptions:
    api_key: str
    base_url: str
    voice_id: str
    language: str
    model: str
    speed: NotGivenOr[float]
    sample_rate: int
    transport: TTSTransport
    extra_body: dict[str, Any] = field(default_factory=dict)

    def request_body(self, text: str) -> dict[str, Any]:
        """One synthesis request. `extra_body` adds fields; it cannot replace these."""
        body: dict[str, Any] = {
            **self.extra_body,
            "text": text,
            "voice_id": self.voice_id,
            "language": self.language,
            "model": self.model,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.sample_rate,
            },
        }
        if is_given(self.speed):
            body["speed"] = self.speed
        return body


class TTS(tts.TTS):  # pyright: ignore[reportMissingTypeArgument]  # no events of its own
    def __init__(
        self,
        *,
        api_key: str | None = None,
        voice_id: str = DEFAULT_VOICE_ID,
        language: TTSLanguages | str = DEFAULT_LANGUAGE,
        model: TTSModels | str = DEFAULT_MODEL,
        speed: NotGivenOr[float] = NOT_GIVEN,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        base_url: str | None = None,
        transport: TTSTransport = "websocket",
        tokenizer: NotGivenOr[tokenize.SentenceTokenizer] = NOT_GIVEN,
        text_pacing: tts.SentenceStreamPacer | bool = False,
        http_session: aiohttp.ClientSession | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        """Bakbak text-to-speech: natural speech for Indian languages, streamed sentence by
        sentence with the lowest latency the API offers.

        Args:
            api_key: Your Bakbak API key. Defaults to the ``BAKBAK_API_KEY`` environment
                variable (``RAYA_API_KEY`` is still read, with a deprecation warning).
            voice_id: The voice. A voice belongs to one model; see :meth:`list_voices`.
                Defaults to Anika M1, an m1 Hindi voice.
            language: The language of the text, e.g. ``"hi"``. BCP-47 tags and names are
                accepted: ``"hi-IN"`` and ``"hindi"`` become ``"hi"``. ``en-IN`` and ``en-US``
                stay distinct; the m1 English voices use ``"en"``.
            model: ``"m1"`` (default) or ``"standard"``; it must match the voice's model.
            speed: Speaking rate, 0.5 to 1.5. The server's default, 1.0, when not given.
            sample_rate: 8000, 16000, 22050 or 24000 Hz (default). Audio is 16-bit mono PCM.
            base_url: The API's base URL. Defaults to ``BAKBAK_BASE_URL``, then the
                production API.
            transport: ``"websocket"`` (default) keeps one connection open and sends each
                sentence on it the moment it is complete; the server queues them, so audio
                flows without a gap. ``"http"`` sends one Server-Sent Events request per
                sentence, overlapping the next request with the current one.
            tokenizer: How text is split into requests. Defaults to
                :class:`SentenceTokenizer`, the Bakbak segmenter.
            text_pacing: Batch sentences according to how much audio is still queued
                (``True`` for LiveKit's default ``SentenceStreamPacer``). Off by default.
            http_session: An aiohttp session to use instead of LiveKit's shared one.
            extra_body: Extra request fields for API features this plugin does not model
                yet. They cannot replace the fields above.
        """
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=NUM_CHANNELS,
        )
        resolved_key = api_key or _env("BAKBAK_API_KEY")
        if not resolved_key:
            raise ValueError(
                "A Bakbak API key is required: pass api_key=... or set the BAKBAK_API_KEY "
                "environment variable"
            )
        if sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(
                f"sample_rate must be one of {sorted(SUPPORTED_SAMPLE_RATES)}, got {sample_rate}"
            )
        if transport not in ("websocket", "http"):
            raise ValueError(f"transport must be 'websocket' or 'http', got {transport!r}")

        resolved_url = (base_url or _env("BAKBAK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._opts = _TTSOptions(
            api_key=resolved_key,
            base_url=resolved_url,
            voice_id=voice_id,
            language=normalize_language(language),
            model=model,
            speed=speed,
            sample_rate=sample_rate,
            transport=transport,
            extra_body=dict(extra_body or {}),
        )
        self._session = http_session
        self._tokenizer: tokenize.SentenceTokenizer = (
            tokenizer if is_given(tokenizer) else SentenceTokenizer()
        )
        self._pacer: tts.SentenceStreamPacer | None = (
            tts.SentenceStreamPacer() if text_pacing is True else text_pacing or None
        )
        self._streams = weakref.WeakSet[SynthesizeStream]()
        identity = {
            "X-API-Key": resolved_key,
            "User-Agent": _USER_AGENT,
            "X-Bakbak-Client": _CLIENT,
        }
        self._sse_headers = {
            **identity,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        self._voices_headers = identity
        self._ws = WSClient(
            url=_ws_url(resolved_url),
            headers=identity,
            session=self._ensure_session,
            connect_timeout=DEFAULT_API_CONNECT_OPTIONS.timeout,
        )

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Bakbak"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = utils.http_context.http_session()
        return self._session

    def update_options(
        self,
        *,
        voice_id: NotGivenOr[str] = NOT_GIVEN,
        language: NotGivenOr[TTSLanguages | str] = NOT_GIVEN,
        model: NotGivenOr[TTSModels | str] = NOT_GIVEN,
        speed: NotGivenOr[float] = NOT_GIVEN,
    ) -> None:
        """Change the voice settings. They apply from the next ``synthesize()``/``stream()``."""
        if is_given(voice_id):
            self._opts.voice_id = voice_id
        if is_given(language):
            self._opts.language = normalize_language(language)
        if is_given(model):
            self._opts.model = model
        if is_given(speed):
            self._opts.speed = speed

    async def list_voices(
        self, *, model: TTSModels | str | None = None, language: TTSLanguages | str | None = None
    ) -> list[Voice]:
        """The voices you can use, optionally only those of one model and/or language."""
        entries = await _http.fetch_voices(
            self._ensure_session(),
            f"{self._opts.base_url}{_http.VOICES_PATH}",
            headers=self._voices_headers,
            timeout=DEFAULT_API_CONNECT_OPTIONS.timeout,
        )
        wanted_language = normalize_language(language) if language else None
        voices = [
            Voice(
                id=str(v.get("id", "")),
                name=str(v.get("name", "")),
                language=str(v.get("language", "")),
                model=str(v.get("model", "")),
            )
            for v in entries
        ]
        return [
            v
            for v in voices
            if (model is None or v.model == model)
            and (wanted_language is None or v.language == wanted_language)
        ]

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> SynthesizeStream:
        stream = SynthesizeStream(tts=self, conn_options=conn_options)
        self._streams.add(stream)
        return stream

    def prewarm(self) -> None:
        """Open the WebSocket now, so the first sentence pays no handshake."""
        if self._opts.transport == "websocket":
            self._ws.prewarm()

    async def aclose(self) -> None:
        for stream in list(self._streams):
            await stream.aclose()
        self._streams.clear()
        await self._ws.aclose()


class ChunkedStream(tts.ChunkedStream):
    """``synthesize()``: one text, split into sentences that are synthesized in order."""

    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        context_id = _new_context_id()
        output_emitter.initialize(
            request_id=context_id,
            sample_rate=self._opts.sample_rate,
            num_channels=NUM_CHANNELS,
            mime_type=_MIME_TYPE,
        )
        attempt = _Attempt(self._tts, self._opts, self._conn_options, output_emitter)
        if self._opts.transport == "websocket":
            self._acquire_time, self._connection_reused = await attempt.connect()
        sentences = self._tts._tokenizer.tokenize(self._input_text)
        await attempt.run(_aiter(sentences), context_id=context_id)
        if not attempt.sent_any and self._input_text.strip():
            output_emitter.push(_silence(self._opts.sample_rate))
        # No flush(): LiveKit ends the input next, which releases the tail as the
        # final frame (a flush first would add a 10 ms silent marker frame).


class SynthesizeStream(tts.SynthesizeStream):
    """``stream()``: text arrives token by token (from an LLM) and each sentence is sent
    the moment it is complete."""

    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        # Every attempt has its own context, so audio from a failed attempt can never
        # mix with its retry's, and LiveKit sees a new request id.
        context_id = _new_context_id()
        output_emitter.initialize(
            request_id=context_id,
            sample_rate=self._opts.sample_rate,
            num_channels=NUM_CHANNELS,
            mime_type=_MIME_TYPE,
            stream=True,
        )
        output_emitter.start_segment(segment_id=context_id)

        sent_stream = self._tts._tokenizer.stream()
        if self._tts._pacer is not None:
            sent_stream = self._tts._pacer.wrap(
                sent_stream=sent_stream, audio_emitter=output_emitter
            )
        attempt = _Attempt(
            self._tts, self._opts, self._conn_options, output_emitter, on_send=self._mark_started
        )
        has_speech = False

        async def forward_input() -> None:
            nonlocal has_speech
            seen_text = False
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    # A stream carries one segment: LiveKit drops text after its flush.
                    if seen_text:
                        break
                    continue
                seen_text = True
                has_speech = has_speech or bool(data.strip())
                sent_stream.push_text(data)
            sent_stream.end_input()

        async def sentences() -> AsyncIterator[str]:
            async for ev in sent_stream:
                yield ev.token

        input_task = asyncio.create_task(forward_input())
        try:
            if self._opts.transport == "websocket":
                self._acquire_time, self._connection_reused = await attempt.connect()
            await attempt.run(sentences(), context_id=context_id)
            await input_task
        finally:
            await utils.aio.gracefully_cancel(input_task)
            await sent_stream.aclose()

        if not attempt.sent_any and has_speech:
            output_emitter.push(_silence(self._opts.sample_rate))
        output_emitter.end_segment()


class _Attempt:
    """One attempt at synthesizing a series of sentences, on either transport."""

    def __init__(
        self,
        tts: TTS,
        opts: _TTSOptions,
        conn_options: APIConnectOptions,
        emitter: tts.AudioEmitter,
        *,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        self._tts = tts
        self._opts = opts
        self._conn_options = conn_options
        self._emitter = emitter
        self._on_send = on_send
        self.sent_any = False
        self.pushed_audio = False
        self._noted: str | None = None

    async def connect(self) -> tuple[float, bool]:
        """Make sure the WebSocket is open; returns (acquire time, connection reused)."""
        return await self._tts._ws.connect(self._conn_options.timeout)

    async def run(self, sentences: AsyncIterable[str], *, context_id: str) -> None:
        try:
            if self._opts.transport == "websocket":
                await self._run_websocket(sentences, context_id)
            else:
                await self._run_http(sentences)
        except APIError as e:
            # LiveKit retries a retryable error; once this attempt has delivered
            # audio, a retry would play it twice (BEHAVIOR.md §4.1).
            if self.pushed_audio:
                _errors.after_audio(e)
            raise
        except aiohttp.ClientError as e:
            raise APIConnectionError(retryable=not self.pushed_audio) from e

    def _sent(self) -> None:
        if not self.sent_any:
            self.sent_any = True
            if self._on_send is not None:
                self._on_send()

    def _push(self, audio: bytes) -> None:
        self._emitter.push(audio)
        self.pushed_audio = True

    def _note(self, request_id: str | None) -> None:
        if request_id and request_id != self._noted:
            self._noted = request_id
            self._emitter._note_provider_request_id(request_id)

    async def _run_websocket(self, sentences: AsyncIterable[str], context_id: str) -> None:
        ctx = self._tts._ws.context(context_id, timeout=self._conn_options.timeout)

        async def send() -> None:
            async for sentence in sentences:
                ctx.send(self._opts.request_body(sentence))
                self._sent()
            ctx.end_input()

        async def receive() -> None:
            async for ev in ctx.events():
                if isinstance(ev, WSAudio):
                    self._note(ev.request.request_id)
                    self._push(ev.data)
                elif isinstance(ev, WSDone):
                    self._note(ev.request.request_id)
                elif isinstance(ev, WSFailed):
                    raise ev.error
                # WSCancelled only follows a cancel, and this attempt cancels only
                # when it is over.

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            await asyncio.gather(*tasks)
        finally:
            await utils.aio.gracefully_cancel(*tasks)
            # Stops whatever the server is still doing for this attempt (an
            # interruption, an error) and drops its late audio; the socket stays.
            ctx.close()

    async def _run_http(self, sentences: AsyncIterable[str]) -> None:
        url = f"{self._opts.base_url}{_http.STREAM_PATH}"
        session = self._tts._ensure_session()

        async def start(sentence: str, sink: _http.AudioSink) -> _http.SSEResult:
            body = json.dumps(self._opts.request_body(sentence), ensure_ascii=False).encode()
            return await _http.stream_sse(
                session,
                url,
                headers=self._tts._sse_headers,
                body=body,
                timeout=self._conn_options.timeout,
                on_audio=sink,
            )

        pipeline = _http.SentencePipeline(
            start=start, on_audio=self._push, on_result=lambda r: self._note(r.request_id)
        )

        async def feed() -> None:
            async for sentence in sentences:
                pipeline.push(sentence)
                self._sent()
            pipeline.end_input()

        tasks = [asyncio.create_task(feed()), asyncio.create_task(pipeline.run())]
        try:
            await asyncio.gather(*tasks)
        finally:
            await utils.aio.gracefully_cancel(*tasks)


def _env(name: str) -> str | None:
    """Read a setting, falling back to its ``livekit-plugins-raya`` name with a warning."""
    value = os.environ.get(name)
    if value:
        return value
    legacy = _LEGACY_ENV[name]
    value = os.environ.get(legacy)
    if value and legacy not in _warned_legacy_env:
        _warned_legacy_env.add(legacy)
        logger.warning("%s is deprecated; set %s instead", legacy, name)
    return value or None


def _ws_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + WS_PATH
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :] + WS_PATH
    return base_url + WS_PATH


def _new_context_id() -> str:
    return f"ctx_{secrets.token_hex(16)}"


def _silence(sample_rate: int) -> bytes:
    """20 ms of silence: what a text with nothing speakable in it sounds like."""
    return b"\x00\x00" * (sample_rate // 50)


async def _aiter(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item
