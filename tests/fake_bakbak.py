"""A behavioural fake of the Bakbak API: SSE, the WebSocket and the voice list.

Unlike the scripted conformance server it synthesizes: every request gets
deterministic 16-bit PCM at the requested sample rate, and a `plan` hook can
make any request fail, hang, or close the socket. The audio encodes which
request produced it (every sample of request ``n`` is ``n``), so tests can check
the order of the audio LiveKit delivers.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohttp import WSMsgType, web

CHUNK_SECONDS = 0.02


@dataclass
class Plan:
    """How the fake answers one request."""

    refuse: tuple[int, dict[str, Any]] | None = None  # (status, error body) before any audio
    close_after_refusal: int | None = None  # close code to send right after the refusal
    chunks: int = 5  # 20 ms each
    fail_after: int | None = None  # chunks before an in-band error event
    close_after: tuple[int, int] | None = None  # (chunks, close code); WebSocket only
    hang_after: int | None = None  # chunks before going silent for good
    chunk_delay: float = 0.0
    first_delay: float = 0.0  # before the first chunk


@dataclass
class Received:
    """A request the fake received."""

    index: int
    transport: str
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


VOICES = [
    {"id": "009ae8b5-4a03-492d-af1c-3055989ac734", "name": "Anika M1", "language": "hi",
     "model": "m1"},
    {"id": "1cbc539d-c456-4045-a99e-57afd746235e", "name": "Avani", "language": "en",
     "model": "m1"},
    {"id": "standard-hi-1", "name": "Standard Hindi", "language": "hi", "model": "standard"},
    {"id": "standard-en-in-1", "name": "Standard EN-IN", "language": "en-in", "model": "standard"},
]  # fmt: skip


class FakeBakbak:
    def __init__(self, plan: Callable[[Received], Plan] | None = None) -> None:
        self.plan: Callable[[Received], Plan] = plan or (lambda _: Plan())
        self.received: list[Received] = []
        self.ws_messages: list[dict[str, Any]] = []
        self.ws_connections = 0
        self.ws_headers: list[dict[str, str]] = []
        self.reject_ws: tuple[int, dict[str, Any]] | None = None
        self.heartbeat: float | None = None  # the real server pings every 30 s
        self.sockets: list[web.WebSocketResponse] = []
        self.http_active = 0
        self.http_max_active = 0
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/v1/text-to-speech/ws", self._ws)
        app.router.add_post("/v1/text-to-speech/stream", self._sse)
        app.router.add_get("/v1/voices", self._voices)
        self._runner = web.AppRunner(app, handle_signals=False, shutdown_timeout=0.1)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert isinstance(server, asyncio.Server)
        self.base_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        return self.base_url

    async def stop(self) -> None:
        for ws in list(self.sockets):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    def texts(self) -> list[str]:
        return [r.body["text"] for r in self.received]

    def cancels(self) -> list[dict[str, Any]]:
        return [m for m in self.ws_messages if m.get("cancel")]

    def _receive(self, transport: str, body: dict[str, Any], headers: dict[str, str]) -> Received:
        received = Received(len(self.received) + 1, transport, body, headers)
        self.received.append(received)
        return received

    @staticmethod
    def chunk(received: Received) -> bytes:
        """One 20 ms chunk of the request's audio: every sample is its index."""
        rate = received.body.get("output_format", {}).get("sample_rate", 24000)
        samples = int(rate * CHUNK_SECONDS)
        return received.index.to_bytes(2, "little", signed=True) * samples

    # -- HTTP ---------------------------------------------------------------

    async def _voices(self, request: web.Request) -> web.Response:
        if request.headers.get("X-API-Key") != "test-key":
            return web.json_response({"message": "Unauthorized"}, status=401)
        return web.json_response({"voices": VOICES, "count": len(VOICES)})

    async def _sse(self, request: web.Request) -> web.StreamResponse:
        self.http_active += 1
        self.http_max_active = max(self.http_max_active, self.http_active)
        try:
            return await self._serve_sse(request)
        finally:
            self.http_active -= 1

    async def _serve_sse(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        received = self._receive("http", body, dict(request.headers))
        plan = self.plan(received)
        request_id = f"req_{received.index}"
        if plan.refuse is not None:
            status, error = plan.refuse
            return web.json_response(error, status=status, headers={"X-Request-Id": request_id})
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "X-Request-Id": request_id}
        )
        await resp.prepare(request)
        await asyncio.sleep(plan.first_delay)
        for i in range(plan.chunks):
            if plan.hang_after == i:
                await asyncio.sleep(3600)
            if plan.fail_after == i:
                event = _stream_interrupted(request_id)
                await resp.write(f"event: error\ndata: {json.dumps(event)}\n\n".encode())
                return resp
            data = base64.b64encode(self.chunk(received)).decode()
            event = {"type": "chunk", "status_code": 206, "done": False, "data": data}
            await resp.write(f"event: chunk\ndata: {json.dumps(event)}\n\n".encode())
            await asyncio.sleep(plan.chunk_delay)
        done = {"type": "done", "status_code": 200, "done": True}
        await resp.write(f"event: done\ndata: {json.dumps(done)}\n\n".encode())
        return resp

    # -- WebSocket ----------------------------------------------------------

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        self.ws_connections += 1
        self.ws_headers.append(dict(request.headers))
        if self.reject_ws is not None:
            status, body = self.reject_ws
            return web.json_response(body, status=status)
        ws = web.WebSocketResponse(heartbeat=self.heartbeat)
        await ws.prepare(request)
        self.sockets.append(ws)
        contexts: dict[str, _Context] = {}
        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                self.ws_messages.append(data)
                context_id = data.get("context_id", "")
                ctx = contexts.setdefault(context_id, _Context(self, ws, context_id))
                if data.get("cancel"):
                    await ctx.cancel()
                else:
                    ctx.add(self._receive("ws", data, dict(request.headers)))
        finally:
            for ctx in contexts.values():
                await ctx.stop()
            self.sockets.remove(ws)
        return ws


class _Context:
    """One context on the fake's WebSocket: requests run one after another."""

    def __init__(self, fake: FakeBakbak, ws: web.WebSocketResponse, context_id: str) -> None:
        self.fake = fake
        self.ws = ws
        self.context_id = context_id
        self.queue: asyncio.Queue[Received] = asyncio.Queue()
        self.worker = asyncio.create_task(self._work())
        self.current: asyncio.Task[None] | None = None

    def add(self, received: Received) -> None:
        self.queue.put_nowait(received)

    async def cancel(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.current is not None and not self.current.done():
            self.current.cancel()
            await asyncio.gather(self.current, return_exceptions=True)
        await self._send({"type": "cancelled", "done": True})

    async def stop(self) -> None:
        self.worker.cancel()
        if self.current is not None:
            self.current.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)

    async def _work(self) -> None:
        while True:
            received = await self.queue.get()
            self.current = asyncio.create_task(self._serve(received))
            await asyncio.gather(self.current, return_exceptions=True)

    async def _send(self, msg: dict[str, Any], request_id: str | None = None) -> None:
        if request_id is not None:
            msg = {**msg, "request_id": request_id}
        if not self.ws.closed:
            await self.ws.send_str(json.dumps({**msg, "context_id": self.context_id}))

    async def _serve(self, received: Received) -> None:
        plan = self.fake.plan(received)
        request_id = f"req_{received.index}"
        if plan.refuse is not None:
            status, error = plan.refuse
            await self._send(
                {
                    "type": "error",
                    "status_code": status,
                    "done": True,
                    **error,
                    "request_id": request_id,
                }
            )
            if plan.close_after_refusal is not None:
                await self.ws.close(code=plan.close_after_refusal)
            return
        await asyncio.sleep(plan.first_delay)
        for i in range(plan.chunks):
            if plan.hang_after == i:
                await asyncio.sleep(3600)
            if plan.close_after is not None and plan.close_after[0] == i:
                await self.ws.close(code=plan.close_after[1])
                return
            if plan.fail_after == i:
                await self._send(_stream_interrupted(request_id))
                return
            data = base64.b64encode(self.fake.chunk(received)).decode()
            await self._send(
                {
                    "type": "chunk",
                    "status_code": 206,
                    "done": False,
                    "data": data,
                    "request_id": request_id,
                }
            )
            await asyncio.sleep(plan.chunk_delay)
        await self._send(
            {"type": "done", "status_code": 200, "done": True, "request_id": request_id}
        )


def _stream_interrupted(request_id: str) -> dict[str, Any]:
    return {
        "type": "error",
        "status_code": 502,
        "done": True,
        "message": "The audio stream ended before it was complete.",
        "error_code": "stream_interrupted",
        "retryable": True,
        "request_id": request_id,
    }


DRAINING = (
    503,
    {
        "message": "The service is restarting. Reconnect and send the request again.",
        "error_code": "service_unavailable",
        "retryable": True,
    },
)

CAPACITY = (
    503,
    {
        "message": "All servers for model 'm1' are busy. Retry in a moment.",
        "error_code": "capacity_exhausted",
        "retryable": True,
    },
)


class BlackHole:
    """Accepts TCP connections and never answers: a handshake that never completes."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.connections = 0
        self.base_url = ""

    async def start(self) -> str:
        async def on_connect(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self.connections += 1
            self._writers.append(writer)

        self._server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        self.base_url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self.base_url

    async def stop(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
