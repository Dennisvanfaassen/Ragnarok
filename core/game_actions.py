from __future__ import annotations

from typing import Any

from core.mouse_adapter import mouse_game_adapter
from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.native_action_bridge import native_action_bridge


class GameActionAdapter:
    """Single actuator boundary for all in-game actions.

    Hunting/pathing code should depend on this class, not on the physical mouse
    implementation. The current backend remains MouseGameAdapter so behavior is
    unchanged. A future native-client backend can implement the same contract.
    """

    def __init__(self):
        self._backend_name = "mouse"
        self._backend = mouse_game_adapter
        self._last_attack_backend = "mouse"

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def sprite_y_offset(self) -> int:
        return int(self._backend.sprite_y_offset)

    def native_attack_ready(self) -> bool:
        try:
            state = native_action_bridge.snapshot()
            return bool(
                state.get("attached")
                and state.get("status") == "ready"
                and (state.get("agent") or {}).get("socket_learned")
            )
        except Exception:
            return False

    def native_move_ready(self) -> bool:
        return self.native_attack_ready()

    def snapshot(self) -> dict[str, Any]:
        client = authenticated_client_monitor.snapshot()
        native_ready = self.native_attack_ready()
        return {
            "backend": "hybrid_native_attack" if native_ready else self._backend_name,
            "classic_pid": client.get("classic_pid"),
            "authenticated_client_seen": bool(client.get("classic_pid")),
            "capabilities": {
                "move": True,
                "attack": True,
                "loot": True,
                "held_direction": True,
                "native_move": True,
                "native_move_ready": native_ready,
                "native_attack": True,
                "native_attack_ready": native_ready,
                "native_loot": False,
            },
            "native_status": native_action_bridge.snapshot().get("status"),
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
            "native_action_ready": self.native_attack_ready(),
            "message": (
                "Native attack is ready; movement and loot still use the mouse."
                if self.native_attack_ready()
                else "Native attack bridge is not ready; mouse backend remains active."
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

    def move(self, player, destination, *args, **kwargs):
        if self.native_move_ready():
            result = native_action_bridge.move(
                int(destination[0]),
                int(destination[1]),
            )
            if result.get("ok"):
                result["backend"] = "native"
                result["input_mode"] = "map_destination"
                return result

        result = self._backend.move(player, destination, *args, **kwargs)
        if isinstance(result, dict):
            result["backend"] = "mouse"
            result["input_mode"] = "screen_projection"
        return result

    def move_to(self, destination: tuple[int, int]) -> dict[str, Any]:
        if self.native_move_ready():
            result = native_action_bridge.move(
                int(destination[0]),
                int(destination[1]),
            )
            if result.get("ok"):
                result["backend"] = "native"
                result["input_mode"] = "map_destination"
                return result
        return {
            "ok": False,
            "backend": "native",
            "reason": "native_move_not_ready",
        }

    def update_hold_direction(self, *args, **kwargs):
        return self._backend.update_hold_direction(*args, **kwargs)

    def release_hold_move(self):
        return self._backend.release_hold_move()

    def loot(self, *args, **kwargs):
        return self._backend.loot(*args, **kwargs)

    def press_hotkey(self, key: str) -> dict[str, Any]:
        result = self._backend.press_hotkey(key)
        if isinstance(result, dict):
            result.setdefault("backend", "windows_input")
        return result

    def attack(self, *args, actor_id: int | None = None, **kwargs):
        if actor_id is not None and self.native_attack_ready():
            result = native_action_bridge.attack(int(actor_id))
            if result.get("ok"):
                self._last_attack_backend = "native"
                result["backend"] = "native"
                result["input_mode"] = "actor_id"
                return result

        self._last_attack_backend = "mouse"
        result = self._backend.attack(*args, **kwargs)
        if isinstance(result, dict):
            result["backend"] = "mouse"
            result["input_mode"] = "screen_projection"
        return result

    def last_attack_backend(self) -> str:
        return self._last_attack_backend

    def note_attack_registered(self, *args, **kwargs):
        # Sprite-offset learning only applies to physical mouse attacks.
        if self._last_attack_backend == "mouse":
            return self._backend.note_attack_registered(*args, **kwargs)
        return None

    def reset_attack_learning(self):
        return self._backend.reset_attack_learning()

    def precision_snapshot(self) -> dict[str, Any]:
        return self._backend.precision_snapshot()


game_actions = GameActionAdapter()
