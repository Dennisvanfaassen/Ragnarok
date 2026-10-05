from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import Any

from core.models import ServerProfile


@dataclass
class ConnectionProbe:
    reachable: bool
    message: str


@dataclass
class LoginProbe:
    connected: bool
    accepted: bool
    response_opcode: str | None
    response_length: int
    message: str
    details: dict[str, Any]


def _zfield(value: str, size: int) -> bytes:
    raw = value.encode("latin-1", errors="replace")[: size - 1]
    return raw + (b"\x00" * (size - len(raw)))


class RagnarokSession:
    """Minimal direct Ragnarok protocol client.

    Phase 1 intentionally stops after the account/login server. It proves
    whether Soulbound accepts an independent, normal protocol login before
    character- and map-server support is enabled.
    """

    def __init__(self, profile: ServerProfile):
        self.profile = profile

    async def probe(self, timeout: float = 3.0) -> ConnectionProbe:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.profile.host, self.profile.port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            return ConnectionProbe(
                True,
                f"TCP connection succeeded to {self.profile.host}:{self.profile.port}.",
            )
        except Exception as exc:
            return ConnectionProbe(False, f"Connection failed: {exc}")

    @staticmethod
    def build_client_hash_packet(client_hash_hex: str) -> bytes:
        value = client_hash_hex.strip().lower().replace(" ", "")
        if len(value) != 32:
            raise ValueError("Client hash must be exactly 32 hexadecimal characters.")
        try:
            digest = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("Client hash contains non-hexadecimal characters.") from exc
        return struct.pack("<H", 0x0204) + digest

    def build_master_login_packet(self, username: str, password: str) -> bytes:
        # Successful Soulbound capture:
        # 0064 + V + Z24 + Z24 + C = 55 bytes
        login_version = (
            self.profile.login_version
            if self.profile.login_version is not None
            else 0x80000000
        )
        master_version = (
            self.profile.master_version
            if self.profile.master_version is not None
            else 1
        )
        return struct.pack(
            "<HI24s24sB",
            0x0064,
            login_version,
            _zfield(username, 24),
            _zfield(password, 24),
            master_version,
        )

    @staticmethod
    def _decode_login_response(data: bytes) -> tuple[int | None, dict[str, Any]]:
        if len(data) < 2:
            return None, {}

        opcode = struct.unpack_from("<H", data, 0)[0]
        details: dict[str, Any] = {}

        if opcode == 0x0AC4:
            details["name"] = "account_server_info"
        elif opcode == 0x083E:
            details["name"] = "AC_REFUSE_LOGIN_R2"
            if len(data) >= 6:
                details["error_type"] = struct.unpack_from("<I", data, 2)[0]
        elif opcode == 0x006A:
            details["name"] = "login_error"
            if len(data) >= 3:
                details["error_type"] = data[2]
        elif opcode == 0x0081:
            details["name"] = "connection_problem"
            if len(data) >= 3:
                details["error_type"] = data[2]

        return opcode, details

    async def login_probe(
        self,
        username: str,
        password: str,
        *,
        client_hash_hex: str | None = None,
        timeout: float = 5.0,
    ) -> LoginProbe:
        if not username:
            raise ValueError("Username is required.")
        if not password:
            raise ValueError("Password is required.")

        reader = None
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.profile.host, self.profile.port),
                timeout=timeout,
            )

            # OpenKore-style client hash packet. It is optional for the first
            # probe because we have not yet extracted Soulbound's 16-byte hash.
            if client_hash_hex:
                writer.write(self.build_client_hash_packet(client_hash_hex))
                await writer.drain()

            packet = self.build_master_login_packet(username, password)
            writer.write(packet)
            await writer.drain()

            data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            opcode, details = self._decode_login_response(data)
            opcode_text = f"0x{opcode:04X}" if opcode is not None else None

            if opcode == 0x0AC4:
                return LoginProbe(
                    connected=True,
                    accepted=True,
                    response_opcode=opcode_text,
                    response_length=len(data),
                    message=(
                        "Login server accepted the account login and returned "
                        "account_server_info (0x0AC4)."
                    ),
                    details=details,
                )

            if opcode in {0x006A, 0x0081, 0x083E}:
                return LoginProbe(
                    connected=True,
                    accepted=False,
                    response_opcode=opcode_text,
                    response_length=len(data),
                    message=f"Login server rejected the login with {opcode_text}.",
                    details=details,
                )

            if not data:
                return LoginProbe(
                    connected=True,
                    accepted=False,
                    response_opcode=None,
                    response_length=0,
                    message="Server closed the connection without a login response.",
                    details={},
                )

            return LoginProbe(
                connected=True,
                accepted=False,
                response_opcode=opcode_text,
                response_length=len(data),
                message=(
                    "Server replied, but the first response is not yet recognized "
                    f"as login success: {opcode_text}."
                ),
                details=details,
            )

        except asyncio.TimeoutError:
            return LoginProbe(
                connected=writer is not None,
                accepted=False,
                response_opcode=None,
                response_length=0,
                message="Timed out waiting for the Soulbound login server response.",
                details={},
            )
        except Exception as exc:
            return LoginProbe(
                connected=False,
                accepted=False,
                response_opcode=None,
                response_length=0,
                message=f"Login probe failed: {exc}",
                details={},
            )
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
