"""Minimal loopback HTTP server implementing Raft's wake-channel endpoint."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .activity import ActivityQueue


WakeCallback = Callable[[dict[str, Any]], Awaitable[None]]


class WakeServer:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        runtime_session: str,
        callback: WakeCallback,
        activity: ActivityQueue | None = None,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.token = token
        self.runtime_session = runtime_session
        self.callback = callback
        self.activity = activity or ActivityQueue()
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            return self.requested_port
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def wake_endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/wake"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host=self.host, port=self.requested_port
        )

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=5
            )
            header_text = raw_headers.decode("iso-8859-1")
            lines = header_text.split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" not in line:
                    continue
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""

            if self.token and headers.get("x-raft-bridge-token") != self.token:
                await self._respond(writer, 401, {"ok": False, "reason": "bad token"})
                return
            url = urlsplit(path)
            if method == "GET" and url.path == "/activity/drain":
                maximum = self._activity_drain_max(parse_qs(url.query).get("max"))
                await self._respond(writer, 200, self.activity.drain(maximum))
                return
            if method == "GET" and path == "/health":
                await self._respond(writer, 200, {"ok": True})
                return
            if method != "POST" or path != "/wake":
                await self._respond(writer, 404, {"ok": False, "reason": "not found"})
                return
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                await self._respond(writer, 400, {"ok": False, "reason": "invalid json"})
                return
            if not isinstance(payload, dict) or payload.get("schema") != "raft-channel-wake.v1":
                await self._respond(
                    writer, 426, {"ok": False, "reason": "unsupported wake schema"}
                )
                return
            await self.callback(payload)
            await self._respond(
                writer,
                200,
                {"ok": True, "runtimeSession": self.runtime_session},
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            await self._respond(writer, 400, {"ok": False, "reason": "bad request"})
        except TimeoutError:
            await self._respond(writer, 408, {"ok": False, "reason": "timeout"})
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    def _activity_drain_max(values: list[str] | None) -> int:
        try:
            return max(1, int(values[0])) if values else 200
        except (TypeError, ValueError):
            return 200

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]
    ) -> None:
        labels = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            408: "Request Timeout",
            426: "Upgrade Required",
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        writer.write(
            (
                f"HTTP/1.1 {status} {labels.get(status, 'Error')}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + body
        )
        await writer.drain()
