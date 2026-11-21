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

"""The HTTP side of the API: Server-Sent Events synthesis and the voice list.

`stream_sse` makes one attempt and raises LiveKit errors; LiveKit's base
classes decide whether to retry. `SentencePipeline` runs one SSE request per
sentence with one request of look-ahead and delivers the audio strictly in
sentence order.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
from livekit.agents import APIConnectionError, APIError, APITimeoutError

from . import _errors
from .log import logger

STREAM_PATH = "/v1/text-to-speech/stream"
VOICES_PATH = "/v1/voices"

# One chunk's base64 can be large; aiohttp refuses a line longer than its buffer.
_READ_BUFSIZE = 16 * 1024 * 1024

AudioSink = Callable[[bytes], None]


@dataclass
class SSEResult:
    """What one completed SSE request delivered."""

    request_id: str | None
    audio_bytes: int
    chunks: int


async def stream_sse(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
    on_audio: AudioSink,
) -> SSEResult:
    """POST one request to the streaming endpoint and hand each audio chunk to `on_audio`.

    Returns once the ``done`` event arrives. Raises ``APIStatusError`` for a
    refusal, an ``error`` event or a stream that breaks off (``status_code``
    -1), ``APITimeoutError`` when the server goes quiet for `timeout` seconds,
    and ``APIConnectionError`` when no response arrives at all. Any error after
    audio was delivered is not retryable.
    """
    result = SSEResult(request_id=None, audio_bytes=0, chunks=0)
    responded = False
    failure: APIError
    try:
        async with session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=timeout),
            read_bufsize=_READ_BUFSIZE,
        ) as resp:
            responded = True
            result.request_id = resp.headers.get("X-Request-Id")
            if resp.status != 200:
                raw = await resp.read()
                raise _errors.status_error(
                    resp.status, _parse_body(raw), request_id=result.request_id, reason=resp.reason
                )
            parser = SSEParser()
            async for line in resp.content:
                payload = parser.feed_line(line)
                if payload is not None and _on_event(payload, result, on_audio):
                    return result
            # The body may end without the blank line after its last event.
            tail = parser.finish()
            if tail is not None and _on_event(tail, result, on_audio):
                return result
            raise _errors.interrupted(
                "The audio stream ended before it was complete.",
                request_id=result.request_id,
                audio_delivered=result.audio_bytes > 0,
            )
    except APIError:
        raise
    except asyncio.TimeoutError as e:
        raise APITimeoutError(retryable=result.audio_bytes == 0) from e
    except (aiohttp.ClientError, OSError) as e:
        broken = (
            _errors.interrupted(
                "The audio stream broke off.",
                request_id=result.request_id,
                audio_delivered=result.audio_bytes > 0,
            )
            if responded
            else APIConnectionError("Could not reach the Bakbak API.")
        )
        failure = _errors.detached(broken, e)
    raise failure  # outside the except block, so the cause is all it carries


def _on_event(payload: str, result: SSEResult, on_audio: AudioSink) -> bool:
    """Handle one event's data. True once the stream is done; raises for ``error``."""
    try:
        event = json.loads(payload)
    except ValueError:
        logger.warning("ignoring an SSE event that is not JSON", extra={"size": len(payload)})
        return False
    if not isinstance(event, dict):
        return False
    kind = event.get("type")
    if kind == "chunk":
        data = event.get("data")
        if isinstance(data, str) and data:
            audio = base64.b64decode(data)
            result.audio_bytes += len(audio)
            result.chunks += 1
            on_audio(audio)
        return False
    if kind == "done":
        return True
    if kind == "error":
        error = _errors.event_error(event, request_id=result.request_id)
        raise _errors.after_audio(error) if result.audio_bytes else error
    return False  # a newer server's event type: ignored (BEHAVIOR.md §10)


class SSEParser:
    """Incremental Server-Sent Events parser (BEHAVIOR.md §5.1).

    Accepts LF and CRLF, skips comments and the ``id:``/``retry:``/``event:``
    fields (the payload's ``type`` is authoritative), joins multi-line ``data:``
    with ``\\n`` and dispatches on a blank line.
    """

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed_line(self, raw: bytes) -> str | None:
        """Feed one line; returns an event's data when the line completes one."""
        line = raw.decode("utf-8")
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            return self.finish()
        if line.startswith("data:"):
            value = line[5:]
            self._data.append(value[1:] if value.startswith(" ") else value)
        return None

    def finish(self) -> str | None:
        """The pending event's data, if any (also used at the end of the body)."""
        if not self._data:
            return None
        data = "\n".join(self._data)
        self._data.clear()
        return data


def _parse_body(raw: bytes) -> object:
    """The parsed JSON error body, or its text when it is not JSON."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


async def fetch_voices(
    session: aiohttp.ClientSession, url: str, *, headers: Mapping[str, str], timeout: float
) -> list[dict[str, Any]]:
    """GET the voice catalog; returns its ``voices`` entries."""
    body: object = None
    failure: APIError | None = None
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=timeout),
        ) as resp:
            raw = await resp.read()
            body = _parse_body(raw)
            if resp.status != 200:
                raise _errors.status_error(
                    resp.status,
                    body,
                    request_id=resp.headers.get("X-Request-Id"),
                    reason=resp.reason,
                )
    except APIError:
        raise
    except asyncio.TimeoutError as e:
        raise APITimeoutError() from e
    except (aiohttp.ClientError, OSError) as e:
        failure = _errors.detached(APIConnectionError("Could not reach the Bakbak API."), e)
    if failure is not None:
        raise failure  # outside the except block, so the cause is all it carries

    voices = body.get("voices") if isinstance(body, dict) else None
    if not isinstance(voices, list):
        raise APIConnectionError("The voice list response had no voices.", retryable=False)
    return [v for v in voices if isinstance(v, dict)]


StartRequest = Callable[[str, AudioSink], Coroutine[Any, Any, SSEResult]]


class _Job:
    """One sentence's request. Audio is buffered until the job is the one playing."""

    def __init__(self, sentence: str, start: StartRequest, sink: AudioSink, *, live: bool) -> None:
        self.sentence = sentence
        self.speculative = not live
        self._sink = sink
        self._buffer: list[bytes] | None = None if live else []
        self.has_audio = asyncio.Event()
        self.task: asyncio.Task[SSEResult] = asyncio.create_task(start(sentence, self._deliver))

    def _deliver(self, audio: bytes) -> None:
        if self._buffer is None:
            self._sink(audio)
        else:
            self._buffer.append(audio)
        self.has_audio.set()

    def go_live(self) -> None:
        """Release the buffered audio, then deliver directly."""
        if self._buffer is None:
            return
        buffered, self._buffer = self._buffer, None
        for audio in buffered:
            self._sink(audio)

    @property
    def buffered_nothing(self) -> bool:
        return not self.has_audio.is_set()


class SentencePipeline:
    """Synthesize sentences with one SSE request each, overlapping the next request
    with the current one and delivering the audio strictly in order.

    The next sentence's request starts once the current one has started
    producing audio, so it is usually finished, or nearly, by the time it is
    needed. A look-ahead request refused before any audio with a retryable
    error (typically a capacity or concurrency refusal caused by the overlap
    itself) is sent again once the current sentence is done.
    """

    def __init__(
        self,
        *,
        start: StartRequest,
        on_audio: AudioSink,
        on_result: Callable[[SSEResult], None],
    ) -> None:
        self._start = start
        self._on_audio = on_audio
        self._on_result = on_result
        self._sentences: deque[str] = deque()
        self._input_ended = False
        self._wakeup = asyncio.Event()

    def push(self, sentence: str) -> None:
        self._sentences.append(sentence)
        self._wakeup.set()

    def end_input(self) -> None:
        self._input_ended = True
        self._wakeup.set()

    async def _wait_for_sentence(self) -> bool:
        """Wait until a sentence is queued (True) or the input has ended (False)."""
        while not self._sentences:
            if self._input_ended:
                return False
            self._wakeup.clear()
            await self._wakeup.wait()
        return True

    def _job(self, *, live: bool) -> _Job:
        # Called with no await between the pop and the request starting, so a
        # cancelled waiter can never lose a sentence.
        return _Job(self._sentences.popleft(), self._start, self._on_audio, live=live)

    async def _look_ahead(self, current: _Job) -> _Job | None:
        await current.has_audio.wait()
        if not await self._wait_for_sentence():
            return None
        return self._job(live=False)

    async def run(self) -> None:
        """Run until the input has ended and every sentence is done; raises the first error."""
        jobs: list[_Job] = []
        upcoming: _Job | None = None
        try:
            while True:
                if upcoming is None:
                    if not await self._wait_for_sentence():
                        return
                    job = self._job(live=True)
                else:
                    job, upcoming = upcoming, None
                    job.go_live()
                jobs.append(job)

                ahead = asyncio.create_task(self._look_ahead(job))
                try:
                    result = await self._finish(job)
                finally:
                    if ahead.done() and not ahead.cancelled():
                        upcoming = ahead.result()
                        if upcoming is not None:
                            jobs.append(upcoming)
                    else:
                        ahead.cancel()
                self._on_result(result)
        finally:
            for job in jobs:
                job.task.cancel()
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)

    async def _finish(self, job: _Job) -> SSEResult:
        try:
            return await job.task
        except APIError as e:
            if not (job.speculative and e.retryable and job.buffered_nothing):
                raise
        # The look-ahead request was refused before any audio, most likely because
        # it overlapped the previous one: send it again now that it is its turn.
        logger.debug("look-ahead request refused; sending it again in turn")
        return await self._start(job.sentence, self._on_audio)
