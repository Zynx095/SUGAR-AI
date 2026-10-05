"""Local UI bridge: static files + a token-protected WebSocket.

The core publishes everything on the event bus; this server forwards events
to connected UIs (batched every 30 ms) and turns UI commands into calls on
the application. It listens on 127.0.0.1 only, requires a random per-launch
token on the WebSocket, and rejects foreign ``Origin`` headers, so a web page
in the user's browser cannot drive Sugar.
"""

from __future__ import annotations

import asyncio
import http
import json
import logging
import mimetypes
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from sugar.core.events import Event, EventBus

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).resolve().parent / "web"
CommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
SnapshotProvider = Callable[[], dict[str, Any]]


class UIServer:
    def __init__(self, bus: EventBus, host: str, port: int, on_command: CommandHandler,
                 snapshot: SnapshotProvider) -> None:
        self._bus = bus
        self._host = host
        self._port = port
        self._on_command = on_command
        self._snapshot = snapshot
        # A fixed token can be supplied for automation/tests; normally it is random per launch.
        self.token = os.environ.get("SUGAR_UI_TOKEN") or secrets.token_urlsafe(24)
        self._clients: set[ServerConnection] = set()
        self._queue: list[dict[str, Any]] = []
        self._server: Server | None = None
        self._flusher: asyncio.Task | None = None
        self.port = port

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}/?token={self.token}"

    async def start(self) -> None:
        self._server = await serve(self._handle, self._host, self._port, process_request=self._process_request,
                                   max_size=4 * 1024 * 1024)
        self.port = self._server.sockets[0].getsockname()[1]
        self._bus.subscribe("*", self._on_event)
        self._flusher = asyncio.create_task(self._flush_loop())
        log.info("UI server on %s", f"http://{self._host}:{self.port}/")

    async def stop(self) -> None:
        if self._flusher:
            self._flusher.cancel()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------ HTTP

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        parsed = urlparse(request.path)
        if parsed.path == "/ws":
            origin = request.headers.get("Origin")
            allowed = {f"http://{self._host}:{self.port}", f"http://localhost:{self.port}"}
            token = parse_qs(parsed.query).get("token", [""])[0]
            if origin not in allowed and origin is not None:
                return self._plain(http.HTTPStatus.FORBIDDEN, "bad origin")
            if not secrets.compare_digest(token, self.token):
                return self._plain(http.HTTPStatus.FORBIDDEN, "bad token")
            return None  # proceed with the WebSocket handshake
        relative = parsed.path.lstrip("/") or "index.html"
        target = (WEB_ROOT / relative).resolve()
        inside = WEB_ROOT in target.parents  # blocks ../ traversal out of the web folder
        if not inside or not target.is_file():
            return self._plain(http.HTTPStatus.NOT_FOUND, "not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        headers = Headers([("Content-Type", content_type), ("Cache-Control", "no-store"),
                           ("X-Content-Type-Options", "nosniff"),
                           ("Content-Security-Policy", "default-src 'self'; connect-src 'self' ws://127.0.0.1:* "
                            "ws://localhost:*; img-src 'self' data:; style-src 'self' 'unsafe-inline'")])
        return Response(200, "OK", headers, target.read_bytes())

    @staticmethod
    def _plain(status: http.HTTPStatus, text: str) -> Response:
        return Response(status.value, status.phrase, Headers([("Content-Type", "text/plain")]), text.encode())

    # ------------------------------------------------------------------ WebSocket

    async def _handle(self, connection: ServerConnection) -> None:
        self._clients.add(connection)
        try:
            await connection.send(json.dumps({"type": "snapshot", "data": self._snapshot()}, default=str))
            async for raw in connection:
                try:
                    message = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict) or "cmd" not in message:
                    continue
                try:
                    reply = await self._on_command(message)
                except Exception as exc:
                    log.exception("UI command failed: %s", message.get("cmd"))
                    reply = {"ok": False, "error": str(exc)}
                if message.get("id") is not None:
                    await connection.send(json.dumps({"type": "reply", "id": message["id"], "data": reply},
                                                     default=str))
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(connection)

    def _on_event(self, event: Event) -> None:
        if self._clients:
            self._queue.append(event.to_dict())
            if len(self._queue) > 2000:
                del self._queue[:1000]

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.03)
            if not self._queue or not self._clients:
                continue
            batch, self._queue = self._queue, []
            payload = json.dumps({"type": "events", "events": batch}, default=str)
            for client in list(self._clients):
                try:
                    await client.send(payload)
                except ConnectionClosed:
                    self._clients.discard(client)

    @property
    def connected(self) -> int:
        return len(self._clients)
