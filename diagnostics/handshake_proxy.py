from __future__ import annotations

import asyncio
import hashlib
import struct
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HandshakeEvent:
    timestamp: float
    direction: str
    length: int
    opcode: str | None
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            "length": self.length,
            "opcode": self.opcode,
            "sha256": self.sha256,
        }


@dataclass
class ProxyState:
    running: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: int = 16900
    upstream_host: str = "88.214.58.232"
    upstream_port: int = 6900
    connections: int = 0
    events: list[HandshakeEvent] = field(default_factory=list)


class HandshakeProxy:
    """Transparent TCP proxy for protocol discovery.

    It deliberately does NOT persist or expose packet payloads because login
    traffic may contain credentials. Only metadata, the first 16-bit opcode,
    packet length and SHA-256 are retained.
    """

    def __init__(self) -> None:
        self.state = ProxyState()
        self._server: asyncio.AbstractServer | None = None

    @staticmethod
    def _opcode(data: bytes) -> str | None:
        if len(data) < 2:
            return None
        value = struct.unpack_from("<H", data, 0)[0]
        return f"0x{value:04X}"

    def _record(self, direction: str, data: bytes) -> None:
        if not data:
            return

        event = HandshakeEvent(
            timestamp=time.time(),
            direction=direction,
            length=len(data),
            opcode=self._opcode(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        self.state.events.append(event)

        # Keep diagnostics compact. Initial login/char handshake is what we
        # care about at this stage.
        self.state.events = self.state.events[-200:]

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        direction: str,
    ) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self._record(direction, data)
                writer.write(data)
                await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _client(
        self,
        local_reader: asyncio.StreamReader,
        local_writer: asyncio.StreamWriter,
    ) -> None:
        self.state.connections += 1
        try:
            remote_reader, remote_writer = await asyncio.open_connection(
                self.state.upstream_host,
                self.state.upstream_port,
            )

            await asyncio.gather(
                self._pipe(local_reader, remote_writer, "client_to_server"),
                self._pipe(remote_reader, local_writer, "server_to_client"),
            )
        except Exception as exc:
            marker = f"proxy-error:{type(exc).__name__}:{exc}".encode(
                "utf-8", errors="replace"
            )
            self._record("proxy_error", marker)
            try:
                local_writer.close()
                await local_writer.wait_closed()
            except Exception:
                pass

    async def start(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 16900,
        upstream_host: str = "88.214.58.232",
        upstream_port: int = 6900,
    ) -> dict:
        if self._server is not None:
            return self.snapshot()

        self.state = ProxyState(
            running=True,
            bind_host=bind_host,
            bind_port=bind_port,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )

        self._server = await asyncio.start_server(
            self._client,
            bind_host,
            bind_port,
        )
        return self.snapshot()

    async def stop(self) -> dict:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.state.running = False
        return self.snapshot()

    def clear(self) -> None:
        self.state.events.clear()
        self.state.connections = 0

    def snapshot(self) -> dict:
        return {
            "running": self.state.running,
            "bind": f"{self.state.bind_host}:{self.state.bind_port}",
            "upstream": (
                f"{self.state.upstream_host}:{self.state.upstream_port}"
            ),
            "connections": self.state.connections,
            "events": [e.as_dict() for e in self.state.events],
            "privacy": (
                "Raw packet payloads are neither stored nor returned. "
                "Only opcode, length, timestamp and SHA-256 are retained."
            ),
        }


handshake_proxy = HandshakeProxy()
