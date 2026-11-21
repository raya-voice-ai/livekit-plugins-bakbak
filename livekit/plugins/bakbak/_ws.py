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

"""The synthesis WebSocket (BEHAVIOR.md §6): many requests on one connection.

One `WSClient` per TTS instance holds the current socket. Each socket has a
reader that is always reading, so the server's pings are answered and every
message is routed to its context, and a writer fed by a queue, so a request is
sent without waiting. A `WSContext` is an ordered series of requests: the
server runs them one after another, so a reply sent sentence by sentence plays
in order and nothing waits for a round trip between sentences.

The client survives the socket: an idle close (1000) reconnects on the next
request, a restart (1001) or a draining refusal moves the waiting requests to a
new socket, and a dropped socket resends every request that had no audio yet.
Requests that had audio fail instead, because audio cannot be taken back.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import random
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
from livekit.agents import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    utils,
)

from . import _errors
from .log import logger

WS_PATH = "/v1/text-to-speech/ws"

# Per-connection limits of the server (BEHAVIOR.md §1). The client waits rather
# than overrun them.
MAX_UNFINISHED_REQUESTS = 64
MAX_ACTIVE_CONTEXTS = 16

# The server pings every 30 s. A socket that has heard nothing for longer than
# this is presumed dead and replaced before it is used, rather than discovered
# dead by a request that times out.
STALE_AFTER = 45.0

_MAX_MESSAGE_SIZE = 16 * 1024 * 1024
_CLOSE_TIMEOUT = 1.0  # longest wait for the server to answer our close (BEHAVIOR.md §6.4)
_CONTEXT_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_FATAL_CLOSE_CODES = frozenset({1008, 1009})

Sleep = Callable[[float], Awaitable[None]]


def backoff(retry: int) -> float:
    """The wait before retry `retry` (1-based), BEHAVIOR.md §4.3."""
    return min(0.5 * 2 ** (retry - 1), 8.0) * (1 - 0.25 * random.random())


class WSRequest:
    """One request on a context: its outcome is one of the events below."""

    __slots__ = ("abandoned", "audio_bytes", "drained", "message", "request_id", "seq")

    def __init__(self, message: str, seq: int) -> None:
        self.message = message  # the JSON text sent, kept for resending
        self.seq = seq  # send order across contexts
        self.request_id: str | None = None
        self.audio_bytes = 0
        self.drained = False  # already refused once by a draining server
        # Failed on our side (bad audio) while the server still streams it: its
        # remaining messages are dropped until its terminal one.
        self.abandoned = False


@dataclass(slots=True)
class WSAudio:
    request: WSRequest
    data: bytes


@dataclass(slots=True)
class WSDone:
    request: WSRequest


@dataclass(slots=True)
class WSFailed:
    request: WSRequest
    error: APIError


@dataclass(slots=True)
class WSCancelled:
    request: WSRequest


WSEvent = WSAudio | WSDone | WSFailed | WSCancelled


class _Signal:
    """Wakes every waiter when the client's state changes."""

    def __init__(self) -> None:
        self._waiters: list[asyncio.Future[None]] = []

    async def wait(self) -> None:
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    def fire(self) -> None:
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)


class _Socket:
    """One WebSocket connection, its reader and its writer."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse[Any]) -> None:
        self.ws = ws
        self.outbox = utils.aio.Chan[str | bytes]()
        self.unfinished = 0  # requests sent and not yet answered, cancelled ones included
        self.contexts: set[WSContext] = set()  # contexts bound to this socket
        self.draining = False  # the server is restarting: send nothing more here
        self.retired = False  # suspect (a context timed out on it): send nothing more here
        self.closed = False
        self.last_heard = time.monotonic()
        self.reader: asyncio.Task[None] | None = None
        self.writer: asyncio.Task[None] | None = None

    @property
    def accepts_requests(self) -> bool:
        return not (self.closed or self.draining or self.retired)

    def has_room_for(self, ctx: WSContext) -> bool:
        if self.unfinished >= MAX_UNFINISHED_REQUESTS:
            return False
        return ctx in self.contexts or len(self.contexts) < MAX_ACTIVE_CONTEXTS

    def send(self, item: str | bytes) -> None:
        if not self.outbox.closed:
            self.outbox.send_nowait(item)


class WSClient:
    """Owns the WebSocket of one TTS instance and routes its messages to contexts.

    Args:
        url: The ``wss://…/v1/text-to-speech/ws`` URL.
        headers: Sent on the upgrade request (``X-API-Key`` and identity).
        session: Returns the aiohttp session to connect with, called lazily.
        max_retries: Reconnects after an abnormal close that may back off.
        sleep: Waits between reconnects; injectable so tests can record them.
        on_notice: Receives server messages about no request at all.
        connect_timeout: For connections nobody waits on (prewarm, reconnect after 1001).
    """

    def __init__(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        session: Callable[[], aiohttp.ClientSession],
        max_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
        on_notice: Callable[[dict[str, Any]], None] | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._headers = dict(headers)
        self._session = session
        self._max_retries = max_retries
        self._sleep = sleep
        self._on_notice = on_notice
        self._connect_timeout = connect_timeout
        self._socket: _Socket | None = None  # where new contexts start
        self._sockets: set[_Socket] = set()  # every open socket, old ones included
        self._contexts: dict[str, WSContext] = {}
        self._opening: asyncio.Task[_Socket] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        # Consecutive abnormal closes (with requests to resend) and failed
        # reconnects. Reset by any message from the server.
        self._failures = 0
        self._last_error: APIError | None = None
        self._seq = 0
        self.changed = _Signal()
        self.closes = 0  # sockets the server or the network has closed, for tests
        self._closed = False

    # -- public API ---------------------------------------------------------

    def context(self, context_id: str, *, timeout: float) -> WSContext:
        """A new context. `timeout` bounds the connect and the wait for each message."""
        if not _CONTEXT_ID.fullmatch(context_id):
            raise ValueError("context_id must be 1-128 characters of A-Z a-z 0-9 . _ -")
        if context_id in self._contexts:
            raise ValueError(f"context {context_id!r} is already in use")
        ctx = WSContext(self, context_id, timeout=timeout)
        self._contexts[context_id] = ctx
        return ctx

    async def connect(self, timeout: float) -> tuple[float, bool]:
        """Make sure a socket is open. Returns (seconds spent, whether one was already open)."""
        if self._usable_socket() is not None:
            return 0.0, True
        started = time.perf_counter()
        await self._acquire(timeout)
        return time.perf_counter() - started, False

    def prewarm(self) -> None:
        """Open the socket in the background, so the first request pays no handshake."""
        if self._closed or self._usable_socket() is not None or self._opening is not None:
            return
        self._spawn(self._prewarm())

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for ctx in list(self._contexts.values()):
            ctx._abort(APIConnectionError("The TTS was closed.", retryable=False))
        if self._opening is not None:
            self._opening.cancel()
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(self._close_socket(s) for s in list(self._sockets)))
        self._socket = None

    # -- sockets ------------------------------------------------------------

    def _usable_socket(self) -> _Socket | None:
        sock = self._socket
        if sock is None:
            return None
        if not sock.accepts_requests:
            self._socket = None
            return None
        if time.monotonic() - sock.last_heard > STALE_AFTER:
            logger.debug("replacing a WebSocket that stopped hearing from the server")
            self._retire(sock)
            return None
        return sock

    async def _acquire(self, timeout: float) -> _Socket:
        """The current socket, opening one if needed (single flight)."""
        while True:
            sock = self._usable_socket()
            if sock is not None:
                return sock
            if self._closed:
                raise APIConnectionError("The TTS was closed.", retryable=False)
            task = self._opening
            if task is None:
                task = self._opening = asyncio.create_task(self._open(timeout))
                task.add_done_callback(_consume_exception)
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout)
            except asyncio.TimeoutError as e:
                raise APITimeoutError("Timed out connecting to the Bakbak API.") from e
            except APIError:
                if self._failures == 0:
                    raise
                # Reconnecting after an abnormal close: the next attempt backs off.

    async def _open(self, timeout: float) -> _Socket:
        try:
            if self._failures > self._max_retries + 1:
                self._failures = 0
                error = self._last_error or APIConnectionError("Could not reconnect.")
                error.retryable = True
                raise error
            if self._failures >= 2:
                await self._sleep(backoff(self._failures - 1))
            try:
                ws = await asyncio.wait_for(self._handshake(), timeout)
            except asyncio.TimeoutError as e:
                error: APIError = APITimeoutError("Timed out connecting to the Bakbak API.")
                error.__cause__ = e
                self._note_failure(error)
                raise error from e
            except APIError as e:
                self._note_failure(e)
                raise
            sock = _Socket(ws)
            sock.reader = asyncio.create_task(self._read(sock))
            sock.writer = asyncio.create_task(self._write(sock))
            self._sockets.add(sock)
            self._socket = sock
            self.changed.fire()
            return sock
        finally:
            self._opening = None

    def _flush(self) -> None:
        """Send every request that can go now, oldest first across contexts.

        Requests that waited for a socket go out in the order they were sent,
        whichever context they belong to."""
        while True:
            ready = [ctx for ctx in self._contexts.values() if ctx._can_send()]
            if not ready:
                return
            min(ready, key=WSContext._next_seq)._send_next()

    def _note_failure(self, error: APIError) -> None:
        self._last_error = error
        if self._failures:
            self._failures += 1

    async def _handshake(self) -> aiohttp.ClientWebSocketResponse[Any]:
        failure: APIError
        try:
            return await self._session().ws_connect(
                self._url,
                headers=self._headers,
                autoping=False,  # pings are answered by the reader, which also notes them
                compress=0,
                max_msg_size=_MAX_MESSAGE_SIZE,
            )
        except aiohttp.WSServerHandshakeError as e:
            # aiohttp drops the refusal's body, so only its status and headers are known.
            request_id = e.headers.get("X-Request-Id") if e.headers else None
            failure = _errors.detached(
                _errors.status_error(e.status, None, request_id=request_id), e
            )
        except Exception as e:
            # aiohttp and OS errors, and anything else (the RuntimeError of a closed
            # session): the requests waiting for this socket must get an outcome.
            failure = _errors.detached(
                APIConnectionError("Could not connect to the Bakbak API."), e
            )
        raise failure  # outside the except block: see _errors.detached

    async def _prewarm(self) -> None:
        try:
            await self._acquire(self._connect_timeout)
        except APIError as e:
            logger.warning(
                "could not open the Bakbak WebSocket ahead of time",
                extra={"status_code": getattr(e, "status_code", None), "lk.pii.error": str(e)},
            )

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _retire(self, sock: _Socket) -> None:
        """Stop starting work on `sock`; close it once nothing is bound to it."""
        sock.retired = True
        if self._socket is sock:
            self._socket = None
        if not sock.contexts and not sock.closed:
            self._spawn(self._close_socket(sock))

    async def _close_socket(self, sock: _Socket) -> None:
        """Close a socket this client no longer wants (nothing is bound to it)."""
        sock.closed = True
        sock.outbox.close()
        self._sockets.discard(sock)
        if self._socket is sock:
            self._socket = None
        if not sock.ws.closed:
            # A peer that stopped answering never sends its close frame back; aiohttp
            # would wait 10 s for it. Cancelling the wait drops the connection.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(sock.ws.close(), _CLOSE_TIMEOUT)
        tasks = [t for t in (sock.reader, sock.writer) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _write(self, sock: _Socket) -> None:
        try:
            async for item in sock.outbox:
                if isinstance(item, bytes):
                    await sock.ws.pong(item)
                else:
                    await sock.ws.send_str(item)
        except (aiohttp.ClientError, OSError, RuntimeError) as e:
            # The connection is broken: treat it as dropped, which resends what can be.
            logger.debug("Bakbak WebSocket write failed: %s", e)
            self._on_closed(sock)
            self._spawn(self._close_socket(sock))

    async def _read(self, sock: _Socket) -> None:
        ws = sock.ws
        try:
            while True:
                msg = await ws.receive()
                sock.last_heard = time.monotonic()
                if msg.type is aiohttp.WSMsgType.TEXT:
                    self._on_text(sock, msg.data)
                elif msg.type is aiohttp.WSMsgType.PING:
                    sock.send(msg.data if isinstance(msg.data, bytes) else b"")
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Bakbak WebSocket read failed: %s", e)
        self._on_closed(sock)

    def _on_text(self, sock: _Socket, data: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            logger.warning("ignoring a WebSocket message that is not JSON")
            return
        if not isinstance(msg, dict):
            return
        self._failures = 0  # the server is answering
        context_id = msg.get("context_id")
        ctx = self._contexts.get(context_id) if isinstance(context_id, str) else None
        if ctx is None:
            if context_id is None and msg.get("request_id") is None:
                self._notice(msg)
            # else: a late message for a context that has been closed.
            return
        if ctx._socket is sock:
            ctx._on_message(sock, msg)

    def _notice(self, msg: dict[str, Any]) -> None:
        """A message about no request (BEHAVIOR.md §6.5): it closes nothing."""
        if self._on_notice is not None:
            self._on_notice(msg)
        else:
            logger.warning(
                "Bakbak WebSocket notice",
                extra={"status_code": msg.get("status_code"), "lk.pii.data": msg},
            )

    def _on_closed(self, sock: _Socket) -> None:
        if sock.closed:
            return
        sock.closed = True
        sock.outbox.close()
        self._sockets.discard(sock)
        if self._socket is sock:
            self._socket = None
        if self._closed:
            return
        self.closes += 1
        code = sock.ws.close_code or aiohttp.WSCloseCode.ABNORMAL_CLOSURE
        logger.debug("Bakbak WebSocket closed", extra={"code": code})

        fatal: APIError | None = None
        if code in _FATAL_CLOSE_CODES:
            fatal = APIConnectionError(
                f"The Bakbak API closed the WebSocket ({code}): the client broke a "
                "connection limit.",
                retryable=False,
            )
        resent = False
        for ctx in list(sock.contexts):
            resent = ctx._on_socket_lost(fatal) or resent
        sock.contexts.clear()

        if code not in (1000, 1001) and fatal is None and resent:
            # Abnormal: the first reconnect is immediate, later ones back off.
            self._failures += 1
        if code == 1001:
            # The server is being restarted or moved out of rotation; the live
            # one is a reconnect away (BEHAVIOR.md §6.4).
            self.prewarm()
        self.changed.fire()


def _live(requests: Iterable[WSRequest]) -> list[WSRequest]:
    """The requests that have no outcome yet (an abandoned one already failed)."""
    return [r for r in requests if not r.abandoned]


def _consume_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


class _Cancel:
    """A cancel sent to the server, waiting for its ``cancelled`` answer."""

    __slots__ = ("future", "in_flight", "requests")

    def __init__(self, future: asyncio.Future[None], requests: list[WSRequest], in_flight: int):
        self.future = future
        self.requests = requests
        self.in_flight = in_flight


class WSContext:
    """An ordered series of requests on the shared WebSocket (BEHAVIOR.md §6.2).

    `send` queues a request and returns at once; `events()` yields what happens
    to each, in order, and ends after `end_input` once every request has an
    outcome. `cancel` stops the server's work and drops the audio still on its
    way; `close` ends the context for good.
    """

    def __init__(self, client: WSClient, context_id: str, *, timeout: float) -> None:
        self.context_id = context_id
        self.timeout = timeout
        self._events = utils.aio.Chan[WSEvent]()
        self._queued = 0  # events put in `_events` so far
        self._silence_until = 0  # audio among the first this many events is dropped
        self._client = client
        self._socket: _Socket | None = None  # the socket its requests are in flight on
        self._pending: deque[WSRequest] = deque()  # not sent yet (or to be resent)
        self._in_flight: deque[WSRequest] = deque()  # sent, no outcome yet; oldest first
        self._held: list[WSRequest] = []  # refused by a draining server; resent elsewhere
        self._cancels: deque[_Cancel] = deque()
        self._pump: asyncio.Task[None] | None = None
        self._deadline = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._input_ended = False
        self._closed = False

    # -- public API ---------------------------------------------------------

    def send(self, body: Mapping[str, Any]) -> WSRequest:
        """Queue one request (a request body without ``context_id``)."""
        if self._closed or self._input_ended:
            raise RuntimeError("the context no longer accepts requests")
        message = json.dumps(
            {**body, "context_id": self.context_id}, ensure_ascii=False, separators=(",", ":")
        )
        self._client._seq += 1
        request = WSRequest(message, self._client._seq)
        self._pending.append(request)
        self._dispatch()
        return request

    def end_input(self) -> None:
        """No more requests: `events()` ends once every request has an outcome."""
        self._input_ended = True
        self._maybe_finish()

    async def events(self) -> AsyncIterator[WSEvent]:
        """What happens to each request, in order (BEHAVIOR.md §6.2)."""
        taken = 0
        async for event in self._events:
            taken += 1
            if taken <= self._silence_until and isinstance(event, WSAudio):
                continue  # received before a cancel, not yet handed out: dropped
            yield event

    def cancel(self) -> asyncio.Future[None]:
        """Stop every unfinished request. Resolves when the server confirms.

        A cancel means silence now: audio already received but not yet taken
        from `events()` is dropped too (BEHAVIOR.md §6.3)."""
        self._silence_until = self._queued
        dropped = [*self._held, *self._pending]
        self._held.clear()
        self._pending.clear()
        if self._in_flight and self._socket is not None:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            requests = [*_live(self._in_flight), *dropped]
            self._cancels.append(_Cancel(future, requests, len(self._in_flight)))
            self._in_flight.clear()
            self._socket.send(json.dumps({"context_id": self.context_id, "cancel": True}))
        elif self._cancels:
            # A cancel is already on its way; these were never sent and end with it.
            future = self._cancels[-1].future
            self._cancels[-1].requests.extend(dropped)
        else:
            future = asyncio.get_running_loop().create_future()
            for request in dropped:
                self._emit(WSCancelled(request))
            future.set_result(None)
            self._maybe_finish()
        return future

    def close(self) -> None:
        """Done with the context: cancel the server's work and stop delivering events."""
        if self._closed:
            return
        if self._in_flight:
            self.cancel()
        self._closed = True
        self._pending.clear()
        self._held.clear()
        self._events.close()
        if self._pump is not None:
            self._pump.cancel()
        self._maybe_unregister()

    @property
    def unfinished(self) -> bool:
        return bool(self._pending or self._in_flight or self._held or self._cancels)

    # -- sending ------------------------------------------------------------

    def _dispatch(self) -> None:
        self._client._flush()
        if self._pending and self._pump is None and not self._closed:
            self._pump = asyncio.create_task(self._run_pump())

    def _target(self) -> _Socket | None:
        """Where the next request would go now, if anywhere."""
        sock = self._socket
        if sock is None:
            return self._client._usable_socket()
        # Bound: requests are in flight there. A draining or retired socket takes
        # nothing more; the rest waits until those are answered.
        return sock if sock.accepts_requests else None

    def _can_send(self) -> bool:
        if not self._pending or self._closed:
            return False
        sock = self._target()
        return sock is not None and sock.has_room_for(self)

    def _next_seq(self) -> int:
        return self._pending[0].seq

    def _send_next(self) -> None:
        sock = self._target()
        assert sock is not None
        request = self._pending.popleft()
        if self._socket is None:
            self._socket = sock
            sock.contexts.add(self)
        self._in_flight.append(request)
        sock.unfinished += 1
        sock.send(request.message)
        self._arm_timer()

    async def _run_pump(self) -> None:
        """Wait for whatever keeps the pending requests from going out."""
        try:
            while not self._closed and self._pending:
                self._client._flush()
                if not self._pending:
                    break
                if self._socket is None and self._client._usable_socket() is None:
                    await self._client._acquire(self.timeout)
                else:
                    await self._client.changed.wait()
        except Exception as e:
            # Nothing ever waits forever (BEHAVIOR.md §6.4): whatever went wrong while
            # (re)connecting, the waiting requests get an outcome.
            error = (
                e
                if isinstance(e, APIError)
                else APIConnectionError("The connection to the Bakbak API failed.")
            )
            pending = list(self._pending)
            self._pending.clear()
            for request in pending:
                self._emit(WSFailed(request, error))
            self._maybe_finish()
        finally:
            self._pump = None

    # -- receiving ----------------------------------------------------------

    def _on_message(self, sock: _Socket, msg: dict[str, Any]) -> None:
        self._deadline = time.monotonic() + self.timeout
        kind = msg.get("type")
        if self._cancels:
            # Everything before the server's answer to a cancel is dropped.
            if kind == "cancelled":
                self._finish_cancel()
            return
        if not self._in_flight:
            return  # nothing is waiting for this message
        request = self._in_flight[0]
        if request.abandoned:
            if kind in ("done", "error"):  # its end: the next message is the next request's
                self._in_flight.popleft()
                sock.unfinished -= 1
                self._after_answer()
            return
        if request.request_id is None:
            request_id = msg.get("request_id")
            if isinstance(request_id, str):
                request.request_id = request_id
        if kind == "chunk":
            data = msg.get("data")
            if isinstance(data, str) and data:
                try:
                    audio = base64.b64decode(data)
                except ValueError:
                    # The stream broke at this point: this request fails, nothing else
                    # does, and the connection stays (BEHAVIOR.md §6.4).
                    request.abandoned = True
                    error = _errors.interrupted(
                        "The server sent audio that is not valid base64.",
                        request_id=request.request_id,
                        audio_delivered=request.audio_bytes > 0,
                    )
                    self._emit(WSFailed(request, error))
                    return
                request.audio_bytes += len(audio)
                self._emit(WSAudio(request, audio))
        elif kind == "done":
            self._settle(WSDone(request))
        elif kind == "error":
            draining = _errors.is_draining_signal(msg)
            if draining:
                sock.draining = True  # nothing more is sent here; closed once unused
            if draining and request.audio_bytes == 0 and not request.drained:
                # Not this connection: send it again on a new one (BEHAVIOR.md §6.4).
                # A second refusal is an ordinary retryable error, below.
                request.drained = True
                self._in_flight.popleft()
                sock.unfinished -= 1
                request.request_id = None  # the resend gets a new one
                self._held.append(request)
                self._after_answer()
            else:
                error = _errors.event_error(msg)
                if request.audio_bytes:
                    _errors.after_audio(error)
                self._settle(WSFailed(request, error))
        # Other types are a newer server's additions: ignored (BEHAVIOR.md §10).

    def _settle(self, event: WSDone | WSFailed) -> None:
        self._in_flight.popleft()
        if self._socket is not None:
            self._socket.unfinished -= 1
        self._emit(event)
        self._after_answer()

    def _finish_cancel(self) -> None:
        cancel = self._cancels.popleft()
        if self._socket is not None:
            self._socket.unfinished -= cancel.in_flight
        for request in cancel.requests:
            self._emit(WSCancelled(request))
        if not cancel.future.done():
            cancel.future.set_result(None)
        self._after_answer()

    def _after_answer(self) -> None:
        if not self._in_flight and not self._cancels:
            if self._held:
                self._pending.extendleft(reversed(self._held))
                self._held.clear()
            self._unbind()
        if self._pending and not self._closed:
            self._dispatch()
        self._client.changed.fire()
        self._maybe_finish()

    def _on_socket_lost(self, fatal: APIError | None) -> bool:
        """The bound socket closed. Returns whether requests are to be resent."""
        cancels, self._cancels = self._cancels, deque()
        for cancel in cancels:
            for request in cancel.requests:
                self._emit(WSCancelled(request))
            if not cancel.future.done():
                cancel.future.set_result(None)
        resend: list[WSRequest] = []
        in_flight, self._in_flight = self._in_flight, deque()
        for request in _live(in_flight):
            if fatal is not None:
                self._emit(WSFailed(request, fatal))
            elif request.audio_bytes:
                error = _errors.interrupted(
                    "The connection closed while the audio was streaming.",
                    request_id=request.request_id,
                    audio_delivered=True,
                )
                self._emit(WSFailed(request, error))
            else:
                request.request_id = None  # the resend gets a new one
                resend.append(request)
        if fatal is not None:
            for request in [*self._held, *self._pending]:
                self._emit(WSFailed(request, fatal))
            self._held.clear()
            self._pending.clear()
        else:
            # Held requests were refused before the ones still in flight were.
            self._pending.extendleft(reversed([*self._held, *resend]))
            self._held.clear()
        self._socket = None
        self._disarm_timer()
        if self._closed:
            self._pending.clear()
            self._maybe_unregister()
            return False
        if self._pending:
            self._dispatch()
        self._maybe_finish()
        return bool(resend)

    def _unbind(self) -> None:
        sock, self._socket = self._socket, None
        self._disarm_timer()
        if sock is None:
            return
        sock.contexts.discard(self)
        if (sock.retired or sock.draining) and not sock.contexts and not sock.closed:
            # Nothing will be sent on it again: close it rather than wait for the
            # server (BEHAVIOR.md §6.4).
            self._client._spawn(self._client._close_socket(sock))
        self._maybe_unregister()

    # -- timeout ------------------------------------------------------------

    def _arm_timer(self) -> None:
        self._deadline = time.monotonic() + self.timeout
        if self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(self.timeout, self._on_timer)

    def _disarm_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timer(self) -> None:
        self._timer = None
        remaining = self._deadline - time.monotonic()
        if remaining > 0:
            self._timer = asyncio.get_running_loop().call_later(remaining, self._on_timer)
            return
        self._on_timeout()

    def _on_timeout(self) -> None:
        """Nothing heard for `timeout` seconds while requests were waiting."""
        sock = self._socket
        logger.warning(
            "Bakbak request timed out",
            extra={"context_id": self.context_id, "timeout": self.timeout},
        )
        if sock is not None and self._in_flight:
            # Ask a live server to stop; a dead one is replaced below.
            sock.send(json.dumps({"context_id": self.context_id, "cancel": True}))
        cancels, self._cancels = self._cancels, deque()
        released = len(self._in_flight) + sum(cancel.in_flight for cancel in cancels)
        for cancel in cancels:
            for request in cancel.requests:
                self._emit(WSCancelled(request))
            if not cancel.future.done():
                cancel.future.set_result(None)
        for request in [*_live(self._in_flight), *self._held, *self._pending]:
            error = APITimeoutError(
                f"No answer from the Bakbak API for {self.timeout:g} s.",
                retryable=request.audio_bytes == 0,
            )
            self._emit(WSFailed(request, error))
        self._in_flight.clear()
        self._held.clear()
        self._pending.clear()
        if sock is not None:
            sock.unfinished -= released
            self._client._retire(sock)
        self._unbind()
        self._client.changed.fire()
        self._maybe_finish()

    # -- bookkeeping ----------------------------------------------------------

    def _emit(self, event: WSEvent) -> None:
        if not self._events.closed:
            self._queued += 1
            self._events.send_nowait(event)

    def _maybe_finish(self) -> None:
        if self._input_ended and not self.unfinished:
            self._events.close()

    def _maybe_unregister(self) -> None:
        contexts = self._client._contexts
        if self._closed and self._socket is None and contexts.get(self.context_id) is self:
            del contexts[self.context_id]

    def _abort(self, error: APIError) -> None:
        """The client is closing: every unfinished request fails with `error`."""
        for cancel in self._cancels:
            if not cancel.future.done():
                cancel.future.set_result(None)
        self._cancels.clear()
        for request in [*_live(self._in_flight), *self._held, *self._pending]:
            self._emit(WSFailed(request, error))
        self._in_flight.clear()
        self._held.clear()
        self._pending.clear()
        self._disarm_timer()
        self.close()
