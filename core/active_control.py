from __future__ import annotations

from typing import Any

from core.hunting_ai import hunting_ai
from core.game_actions import game_actions


class ActiveHuntController:
    """Compatibility facade for the v2 hunting AI.

    The old hold-and-steer implementation has been retired. All behavior now
    lives in HuntingAI; MouseGameAdapter only executes MOVE/ATTACK requests.
    """

    def start(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return hunting_ai.start(payload or {})

    def stop(self) -> dict[str, Any]:
        return hunting_ai.stop()

    def snapshot(self) -> dict[str, Any]:
        return hunting_ai.snapshot()

    def start_calibration(self) -> dict[str, Any]:
        if hunting_ai.running:
            raise RuntimeError("Stop active hunt before calibrating.")
        return game_actions.start_calibration()

    def clear_calibration(self) -> dict[str, Any]:
        if hunting_ai.running:
            raise RuntimeError("Stop active hunt before clearing calibration.")
        return game_actions.clear_calibration()

    def calibration_snapshot(self) -> dict[str, Any]:
        return game_actions.calibration_snapshot()


active_hunt_controller = ActiveHuntController()
