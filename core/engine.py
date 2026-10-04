from __future__ import annotations

from core.models import BotStatus
from core.state import app_state


class BotEngine:
    def start(self) -> None:
        app_state.patch_runtime(
            status=BotStatus.STARTING,
            message="Core ready. Verified Ragnarok protocol profile required.",
            current_action="Protocol setup required",
        )

    def stop(self) -> None:
        app_state.patch_runtime(
            status=BotStatus.STOPPED,
            message="Stopped",
            current_action="Idle",
            target=None,
        )


engine = BotEngine()
