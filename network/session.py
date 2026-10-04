from __future__ import annotations

import asyncio
from dataclasses import dataclass
from core.models import ServerProfile


@dataclass
class ConnectionProbe:
    reachable: bool
    message: str


class RagnarokSession:
    """Network boundary for the new bot core.

    The real Ragnarok login/char/map protocol will be implemented here only
    after the exact packet profile has been identified and verified.
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

    async def connect(self) -> None:
        raise NotImplementedError(
            "Protocol login is disabled until the packet profile is verified."
        )
