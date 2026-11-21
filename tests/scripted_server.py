"""A mock Bakbak server that plays the scenarios of conformance/ (see conformance/README.md)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from typing import Any

from aiohttp import WSMsgType, web


def subset_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    """Every key of `expected` is in `actual` with an equal value."""
    return all(key in actual and actual[key] == value for key, value in expected.items())


class ScriptedServer:
    """Serves one scenario's ``exchanges`` (HTTP) or ``connections`` (WebSocket) in order.

    Anything the client does that the scenario does not expect is recorded in
    `failures`; the test asserts it is empty.
    """

    def __init__(self, scenario: Mapping[str, Any]) -> None:
        self.exchanges: list[dict[str, Any]] = list(scenario.get("exchanges", []))
        self.connections: list[dict[str, Any]] = list(scenario.get("connections", []))
        self.failures: list[str] = []
        self.http_attempts = 0
        self.ws_connections = 0
        self.client_done = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._open: set[web.WebSocketResponse] = set()
        self.base_url = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/v1/text-to-speech/ws", self._ws)
        app.router.add_route("*", "/{tail:.*}", self._http)
        self._runner = web.AppRunner(app, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert isinstance(server, asyncio.Server)
        port = server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        self.client_done.set()
        for ws in list(self._open):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    def fail(self, message: str) -> None:
        self.failures.append(message)

    # -- HTTP -----------------------------------------------------------------

    async def _http(self, request: web.Request) -> web.StreamResponse:
        index = self.http_attempts
        self.http_attempts += 1
        if index >= len(self.exchanges):
            self.fail(f"unexpected HTTP attempt #{index + 1}: {request.method} {request.path}")
            return web.Response(status=599)
        exchange = self.exchanges[index]
        expected = exchange["expect_request"]
        if request.method != expected["method"] or request.path != expected["path"]:
            self.fail(f"attempt {index + 1}: {request.method} {request.path} != {expected}")
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not subset_match(expected.get("headers", {}), headers):
            self.fail(f"attempt {index + 1}: headers {headers} lack {expected.get('headers')}")
        if "json" in expected:
            body = await request.json()
            if body != expected["json"]:
                self.fail(f"attempt {index + 1}: body {body} != {expected['json']}")

        response = exchange["response"]
        if response.get("network_error"):
            assert request.transport is not None
            request.transport.abort()  # drop the connection without a response
            raise asyncio.CancelledError
        status = response["status"]
        resp_headers = dict(response.get("headers", {}))
        if "json" in response:
            return web.Response(
                status=status, headers=resp_headers, body=json.dumps(response["json"]).encode()
            )
        if "sse" in response:
            return web.Response(status=status, headers=resp_headers, body=response["sse"].encode())
        return web.Response(
            status=status, headers=resp_headers, body=base64.b64decode(response["body_base64"])
        )

    # -- WebSocket ------------------------------------------------------------

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        index = self.ws_connections
        self.ws_connections += 1
        if index >= len(self.connections):
            self.fail(f"unexpected WebSocket connection #{index + 1}")
            return web.Response(status=599)
        connection = self.connections[index]
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not subset_match(connection.get("expect_headers", {}), headers):
            self.fail(f"connection {index + 1}: headers lack {connection['expect_headers']}")
        if "reject" in connection:
            reject = connection["reject"]
            return web.Response(
                status=reject["status"],
                headers=reject.get("headers", {}),
                body=json.dumps(reject["json"]).encode(),
            )

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._open.add(ws)
        try:
            for step in connection["script"]:
                if not await self._step(index, ws, step):
                    break
        finally:
            self._open.discard(ws)
        return ws

    async def _step(self, index: int, ws: web.WebSocketResponse, step: Mapping[str, Any]) -> bool:
        if "recv" in step:
            try:
                msg = await asyncio.wait_for(ws.receive(), 5)
            except asyncio.TimeoutError:
                self.fail(f"connection {index + 1}: no message, expected {step['recv']}")
                return False
            if msg.type is not WSMsgType.TEXT:
                self.fail(f"connection {index + 1}: got {msg.type}, expected {step['recv']}")
                return False
            got = json.loads(msg.data)
            if not isinstance(got, dict) or not subset_match(step["recv"], got):
                self.fail(f"connection {index + 1}: got {got}, expected {step['recv']}")
                return False
        elif "send" in step:
            await ws.send_str(json.dumps(step["send"]))
        elif "close" in step:
            close = step["close"]
            await ws.close(code=close["code"], message=close.get("reason", "").encode())
            return False
        elif step.get("expect_no_more"):
            try:
                msg = await asyncio.wait_for(ws.receive(), 0.3)
            except asyncio.TimeoutError:
                pass
            else:
                if msg.type is WSMsgType.TEXT:
                    self.fail(f"connection {index + 1}: unexpected message {msg.data}")
                return msg.type not in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)
            # Keep the connection open (and answering) until the client is done.
            reader = asyncio.ensure_future(self._drain(index, ws))
            await self.client_done.wait()
            reader.cancel()
        return True

    async def _drain(self, index: int, ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                self.fail(f"connection {index + 1}: unexpected message {msg.data}")
