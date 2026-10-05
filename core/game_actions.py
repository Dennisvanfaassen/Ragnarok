from __future__ import annotations

from typing import Any

from core.mouse_adapter import mouse_game_adapter
from diagnostics.authenticated_client import authenticated_client_monitor


class GameActionAdapter:
    """Single actuator boundary for all in-game actions.

    Hunting/pathing code should depend on this class, not on the physical mouse
    implementation. The current backend remains MouseGameAdapter so behavior is
    unchanged. A future native-client backend can implement the same contract.
    """

    def __init__(self):
        self._backend_name = "mouse"
        self._backend = mouse_game_adapter

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def sprite_y_offset(self) -> int:
        return int(self._backend.sprite_y_offset)

    def snapshot(self) -> dict[str, Any]:
        client = authenticated_client_monitor.snapshot()
        return {
            "backend": self._backend_name,
            "classic_pid": client.get("classic_pid"),
            "authenticated_client_seen": bool(client.get("classic_pid")),
            "capabilities": {
                "move": True,
                "attack": True,
                "loot": True,
                "held_direction": True,
                "native_move": False,
                "native_attack": False,
                "native_loot": False,
            },
            "native_status": (
                "not_implemented"
                if self._backend_name == "mouse"
                else "available"
            ),
            "calibration": self._backend.calibration_snapshot(),
        }

    def direct_action_probe(self) -> dict[str, Any]:
        """Read-only probe for the future native action backend.

        This intentionally performs no client-memory writes and sends no game
        action. It exposes actor IDs/positions so the action boundary can be
        verified before a native backend is introduced.
        """
        snapshot = authenticated_client_monitor.snapshot()
        live = snapshot.get("live_state") or {}
        world = live.get("world") or {}
        actors = []
        for actor in live.get("actors") or []:
            if actor.get("kind") != "monster":
                continue
            actor_id = actor.get("id")
            x, y = actor.get("x"), actor.get("y")
            if actor_id is None or x is None or y is None:
                continue
            actors.append({
                "id": int(actor_id),
                "name": str(actor.get("name") or f"Monster #{actor_id}"),
                "x": int(x),
                "y": int(y),
                "render_x": actor.get("render_x"),
                "render_y": actor.get("render_y"),
            })
        return {
            "backend": self._backend_name,
            "classic_pid": snapshot.get("classic_pid"),
            "map": world.get("map"),
            "player": {
                "x": world.get("x"),
                "y": world.get("y"),
                "render_x": world.get("render_x"),
                "render_y": world.get("render_y"),
            },
            "monsters": actors,
            "native_action_ready": False,
            "message": (
                "GameActionAdapter is active. Native client actions are not "
                "enabled yet; this probe is read-only."
            ),
        }

    def dry_run_attack(self, actor_id: int) -> dict[str, Any]:
        """Resolve an actor ID exactly as a future native attack would."""
        probe = self.direct_action_probe()
        actor = next(
            (entry for entry in probe["monsters"] if int(entry["id"]) == int(actor_id)),
            None,
        )
        if actor is None:
            return {
                "ok": False,
                "backend": self._backend_name,
                "actor_id": int(actor_id),
                "reason": "actor_not_visible",
            }
        return {
            "ok": True,
            "executed": False,
            "backend": self._backend_name,
            "command": "attack_actor",
            "actor": actor,
            "message": (
                "Actor resolved successfully. No input was sent; this is the "
                "contract that a future native backend will execute."
            ),
        }

    # Mouse-compatible action contract. These delegation methods deliberately
    # keep HuntingAI independent from the implementation behind the boundary.
    def configure(self, **kwargs):
        return self._backend.configure(**kwargs)

    def calibration_valid(self) -> bool:
        return self._backend.calibration_valid()

    def calibration_snapshot(self) -> dict[str, Any]:
        return self._backend.calibration_snapshot()

    def start_calibration(self) -> dict[str, Any]:
        return self._backend.start_calibration()

    def clear_calibration(self) -> dict[str, Any]:
        return self._backend.clear_calibration()

    def can_project(self, *args, **kwargs):
        return self._backend.can_project(*args, **kwargs)

    def move(self, *args, **kwargs):
        return self._backend.move(*args, **kwargs)

    def update_hold_direction(self, *args, **kwargs):
        return self._backend.update_hold_direction(*args, **kwargs)

    def release_hold_move(self):
        return self._backend.release_hold_move()

    def loot(self, *args, **kwargs):
        return self._backend.loot(*args, **kwargs)

    def attack(self, *args, **kwargs):
        return self._backend.attack(*args, **kwargs)

    def note_attack_registered(self, *args, **kwargs):
        return self._backend.note_attack_registered(*args, **kwargs)

    def reset_attack_learning(self):
        return self._backend.reset_attack_learning()

    def precision_snapshot(self) -> dict[str, Any]:
        return self._backend.precision_snapshot()


game_actions = GameActionAdapter()
