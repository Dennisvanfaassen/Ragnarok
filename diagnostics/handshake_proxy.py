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
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            "length": self.length,
            "opcode": self.opcode,
            "sha256": self.sha256,
            "details": self.details,
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


# Fixed lengths for the small set of login-stage packets we can safely
# recognize without needing the full game packet table.
LOGIN_STAGE_FIXED_LENGTHS = {
    0x006A: 23,
    0x006C: 3,
    0x0081: 3,
    0x0205: 26,
    0x02CA: 3,
    0x083E: 26,
    0x0ACD: 3,
    0x0AE0: 30,
}


def _safe_details(opcode: int | None, data: bytes) -> dict[str, Any]:
    if opcode == 0x083E and len(data) >= 26:
        # OpenKore: AC_REFUSE_LOGIN_R2 => V Z20
        error_type = struct.unpack_from("<I", data, 2)[0]
        raw_date = data[6:26].split(b"\x00", 1)[0]
        return {
            "name": "AC_REFUSE_LOGIN_R2",
            "login_error_type": error_type,
            "date": raw_date.decode("latin-1", errors="replace"),
        }
    return {}


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

        opcode_value = (
            struct.unpack_from("<H", data, 0)[0]
            if len(data) >= 2
            else None
        )
        event = HandshakeEvent(
            timestamp=time.time(),
            direction=direction,
            length=len(data),
            opcode=self._opcode(data),
            sha256=hashlib.sha256(data).hexdigest(),
            details=_safe_details(opcode_value, data),
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
                # Record the raw TCP chunk, then also record recognizable
                # fixed-length login-stage packets when a chunk contains them.
                self._record(direction, data)

                offset = 0
                while len(data) - offset >= 2:
                    opcode = struct.unpack_from("<H", data, offset)[0]
                    packet_len = LOGIN_STAGE_FIXED_LENGTHS.get(opcode)
                    if not packet_len or offset + packet_len > len(data):
                        break

                    packet = data[offset:offset + packet_len]
                    if offset != 0 or len(packet) != len(data):
                        self._record(direction + "_parsed", packet)
                    offset += packet_len

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
