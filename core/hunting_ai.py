from __future__ import annotations

import threading
import time
from typing import Any

from core.mouse_adapter import mouse_game_adapter
from core.pathing import build_pathing_state, nav_repository
from core.state import app_state
from core.targeting import build_targeting_state
from diagnostics.authenticated_client import authenticated_client_monitor


STATES = {
    "IDLE",
    "SEARCHING",
    "TARGET_SELECTED",
    "ROUTING",
    "APPROACHING",
    "ATTACK_READY",
    "ATTACKING",
    "WAITING_FOR_DEATH",
    "TARGET_DEAD",
    "FAILED",
}


class HuntingAI:
    """OpenKore-style hunting state machine.

    All decisions happen in map coordinates. MouseGameAdapter is only an
    actuator and does not choose targets, routes or combat behavior.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False

        self.state = "IDLE"
        self.message = "Idle"
        self.target_id: int | None = None
        self.target_name: str | None = None
        self.target_pos: tuple[int, int] | None = None
        self.route_target_pos: tuple[int, int] | None = None

        self.attack_range = 1
        self.move_segment_tiles = 4
        self.target_move_reset_tiles = 2
        self.attack_confirm_timeout = 1.8
        self.max_attack_retries = 3
        self.move_settle_timeout = 4.0

        self.attack_retry = 0
        self.attack_clicked_at = 0.0
        self.state_since = time.time()
        self.actions: list[dict[str, Any]] = []

    def configure(self, payload: dict[str, Any]):
        self.attack_range = max(
            1, min(8, int(payload.get("attack_range", self.attack_range)))
        )
        self.move_segment_tiles = max(
            1,
            min(8, int(payload.get("move_segment_tiles", self.move_segment_tiles))),
        )
        self.target_move_reset_tiles = max(
            1,
            min(
                6,
                int(
                    payload.get(
                        "target_move_reset_tiles",
                        self.target_move_reset_tiles,
                    )
                ),
            ),
        )
        self.attack_confirm_timeout = max(
            0.5,
            min(
                5.0,
                float(
                    payload.get(
                        "combat_confirm_timeout",
                        self.attack_confirm_timeout,
                    )
                ),
            ),
        )
        mouse_game_adapter.configure(
            sprite_y_offset=payload.get("sprite_y_offset")
        )

    def _set_state(self, state: str, message: str):
        if state not in STATES:
            raise ValueError(f"Unknown AI state: {state}")
        with self._lock:
            changed = state != self.state
            self.state = state
            self.message = message
            if changed:
                self.state_since = time.time()
                self._log("state", state=state, message=message)

        app_state.patch_runtime(
            current_action=state.replace("_", " ").title(),
            message=message,
            target=(
                f"{self.target_name} @ {self.target_pos[0]},{self.target_pos[1]}"
                if self.target_name and self.target_pos
                else None
            ),
        )

    def _log(self, action: str, **details):
        entry = {"time": time.time(), "action": action, **details}
        with self._lock:
            self.actions.append(entry)
            self.actions = self.actions[-80:]

    @staticmethod
    def _world(snapshot: dict[str, Any]) -> dict[str, Any]:
        return (snapshot.get("live_state") or {}).get("world") or {}

    @staticmethod
    def _position(snapshot: dict[str, Any]) -> tuple[int, int] | None:
        world = HuntingAI._world(snapshot)
        x, y = world.get("x"), world.get("y")
        if x is None or y is None:
            return None
        return int(x), int(y)

    @staticmethod
    def _find_actor(
        snapshot: dict[str, Any],
        actor_id: int | None,
    ) -> dict[str, Any] | None:
        if actor_id is None:
            return None
        actors = ((snapshot.get("live_state") or {}).get("actors") or [])
        for actor in actors:
            if int(actor.get("id") or -1) == int(actor_id):
                return actor
        return None

    def _refresh_locked_target(
        self,
        snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        actor = self._find_actor(snapshot, self.target_id)
        if actor is None:
            return None

        x, y = actor.get("x"), actor.get("y")
        if x is not None and y is not None:
            self.target_pos = (int(x), int(y))
        if actor.get("name"):
            self.target_name = str(actor["name"])
        return actor

    def _clear_target(self):
        self.target_id = None
        self.target_name = None
        self.target_pos = None
        self.route_target_pos = None
        self.attack_retry = 0
        self.attack_clicked_at = 0.0

    def _acquire_target(self, snapshot: dict[str, Any]) -> bool:
        profile = app_state.get_profile()
        targeting = build_targeting_state(snapshot, profile.hunt.monsters)
        selected = targeting.get("selected")
        if not selected:
            return False

        self.target_id = int(selected["id"])
        self.target_name = str(selected.get("name") or f"Monster #{self.target_id}")
        self.target_pos = (int(selected["x"]), int(selected["y"]))
        self.route_target_pos = None
        self.attack_retry = 0
        self._log(
            "target_locked",
            target_id=self.target_id,
            target_name=self.target_name,
            x=self.target_pos[0],
            y=self.target_pos[1],
        )
        return True

    @staticmethod
    def _tile_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    def _combat_confirmed(
        self,
        snapshot: dict[str, Any],
        target_id: int,
        since: float,
    ) -> bool:
        combat = self._world(snapshot).get("last_combat") or {}
        try:
            return (
                int(combat.get("target_id")) == int(target_id)
                and float(combat.get("timestamp") or 0) >= since
            )
        except Exception:
            return False

    def _wait_for_move_progress(
        self,
        origin: tuple[int, int],
        destination: tuple[int, int],
        target_id: int,
    ) -> tuple[int, int] | None:
        deadline = time.time() + self.move_settle_timeout
        last_pos = origin
        last_change = time.time()
        moved = False

        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(0.10)
            snapshot = authenticated_client_monitor.snapshot()

            if self._find_actor(snapshot, target_id) is None:
                return None

            pos = self._position(snapshot)
            if pos is None:
                continue

            if pos != last_pos:
                moved = True
                last_pos = pos
                last_change = time.time()

            if self._tile_distance(pos, destination) <= 1:
                return pos

            if moved and time.time() - last_change >= 0.55:
                return pos

        return last_pos

    def _choose_move_segment(
        self,
        path_preview: list[dict[str, Any]],
    ) -> tuple[int, int] | None:
        if len(path_preview) < 2:
            return None

        # Never deliberately walk onto the monster tile. Keep at least
        # attack_range route cells available for the attack phase.
        max_index = max(
            1,
            len(path_preview) - 1 - self.attack_range,
        )
        index = min(self.move_segment_tiles, max_index)
        point = path_preview[index]
        return int(point["x"]), int(point["y"])

    def _step_searching(self, snapshot: dict[str, Any]):
        if self._acquire_target(snapshot):
            self._set_state(
                "TARGET_SELECTED",
                f"Locked {self.target_name}",
            )
        else:
            self._set_state("SEARCHING", "No valid target in live actor list")
            self._stop.wait(0.20)

    def _step_target_selected(self, snapshot: dict[str, Any]):
        if self._refresh_locked_target(snapshot) is None:
            self._set_state("TARGET_DEAD", "Target disappeared before approach")
            return
        self._set_state("ROUTING", f"Calculating route to {self.target_name}")

    def _step_routing(self, snapshot: dict[str, Any]):
        actor = self._refresh_locked_target(snapshot)
        player = self._position(snapshot)

        if actor is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return
        if player is None or self.target_pos is None:
            self._stop.wait(0.10)
            return

        distance = self._tile_distance(player, self.target_pos)
        if distance <= self.attack_range:
            self._set_state(
                "ATTACK_READY",
                f"{self.target_name} is in attack position",
            )
            return

        targeting = {
            "selected": {
                "id": self.target_id,
                "name": self.target_name,
                "x": self.target_pos[0],
                "y": self.target_pos[1],
            }
        }
        pathing = build_pathing_state(snapshot, targeting, nav_repository)
        path_preview = pathing.get("path_preview") or []

        if not pathing.get("path_found") or len(path_preview) < 2:
            self._log(
                "route_failed",
                target_id=self.target_id,
                message=pathing.get("message"),
            )
            self._set_state(
                "FAILED",
                pathing.get("message") or "Could not route to target",
            )
            return

        segment = self._choose_move_segment(path_preview)
        if segment is None:
            self._set_state("ATTACK_READY", "At target approach position")
            return

        self.route_target_pos = self.target_pos
        self._set_state(
            "APPROACHING",
            f"Approaching {self.target_name} via {segment[0]},{segment[1]}",
        )

        result = mouse_game_adapter.move(player, segment)
        self._log(
            "move",
            target_id=self.target_id,
            destination={"x": segment[0], "y": segment[1]},
            result=result,
        )

        if not result.get("ok"):
            # A long segment can be outside the calibrated viewport. Retry one
            # cell ahead rather than changing the target or guessing a pixel.
            if len(path_preview) > 1:
                fallback = (
                    int(path_preview[1]["x"]),
                    int(path_preview[1]["y"]),
                )
                result = mouse_game_adapter.move(player, fallback)
                self._log(
                    "move_fallback",
                    target_id=self.target_id,
                    destination={"x": fallback[0], "y": fallback[1]},
                    result=result,
                )
                if result.get("ok"):
                    segment = fallback

        if not result.get("ok"):
            self._set_state(
                "FAILED",
                f"Mouse adapter could not execute route segment: {result.get('reason')}",
            )
            return

        end_pos = self._wait_for_move_progress(
            player,
            segment,
            int(self.target_id),
        )
        if end_pos is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return

        fresh = authenticated_client_monitor.snapshot()
        actor = self._refresh_locked_target(fresh)
        if actor is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return

        if (
            self.route_target_pos
            and self.target_pos
            and self._tile_distance(self.route_target_pos, self.target_pos)
            >= self.target_move_reset_tiles
        ):
            self._log(
                "route_reset_target_moved",
                target_id=self.target_id,
                from_pos={
                    "x": self.route_target_pos[0],
                    "y": self.route_target_pos[1],
                },
                to_pos={
                    "x": self.target_pos[0],
                    "y": self.target_pos[1],
                },
            )

        self._set_state("ROUTING", "Re-evaluating approach route")

    def _step_attack_ready(self, snapshot: dict[str, Any]):
        actor = self._refresh_locked_target(snapshot)
        player = self._position(snapshot)

        if actor is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return
        if player is None or self.target_pos is None:
            self._stop.wait(0.10)
            return

        distance = self._tile_distance(player, self.target_pos)
        if distance > self.attack_range + 1:
            self._set_state(
                "ROUTING",
                f"{self.target_name} moved out of attack position",
            )
            return

        result = mouse_game_adapter.attack(
            player,
            self.target_pos,
            retry_index=self.attack_retry,
        )
        self._log(
            "attack_attempt",
            target_id=self.target_id,
            target_name=self.target_name,
            retry=self.attack_retry,
            result=result,
        )

        if not result.get("ok"):
            self._set_state(
                "ROUTING",
                f"Target is not safely clickable: {result.get('reason')}",
            )
            return

        self.attack_clicked_at = time.time()
        self._set_state(
            "ATTACKING",
            f"Waiting for combat confirmation on {self.target_name}",
        )

    def _step_attacking(self, snapshot: dict[str, Any]):
        actor = self._refresh_locked_target(snapshot)
        if actor is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return

        if self._combat_confirmed(
            snapshot,
            int(self.target_id),
            self.attack_clicked_at,
        ):
            self._log(
                "combat_confirmed",
                target_id=self.target_id,
                target_name=self.target_name,
            )
            self._set_state(
                "WAITING_FOR_DEATH",
                f"Combat confirmed; waiting for {self.target_name} to die",
            )
            return

        if time.time() - self.attack_clicked_at < self.attack_confirm_timeout:
            self._stop.wait(0.08)
            return

        self.attack_retry += 1
        if self.attack_retry < self.max_attack_retries:
            self._log(
                "attack_retry",
                target_id=self.target_id,
                retry=self.attack_retry,
            )
            self._set_state(
                "ATTACK_READY",
                f"Attack was not confirmed; retry {self.attack_retry + 1}/{self.max_attack_retries}",
            )
            return

        # OpenKore-style failure behavior: stop hammering the same interaction,
        # re-evaluate the approach, and only try again after repositioning.
        self.attack_retry = 0
        self._set_state(
            "ROUTING",
            "Attack was not confirmed after retries; recalculating approach",
        )

    def _step_waiting_for_death(self, snapshot: dict[str, Any]):
        if self._refresh_locked_target(snapshot) is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} defeated")
            return

        # Intentionally no movement, target switching or attack clicks here.
        self._stop.wait(0.12)

    def _step_target_dead(self):
        old_id = self.target_id
        old_name = self.target_name
        self._log(
            "target_finished",
            target_id=old_id,
            target_name=old_name,
        )
        self._clear_target()
        self._stop.wait(0.20)
        self._set_state("SEARCHING", "Selecting next monster")

    def _step_failed(self):
        # Drop only the current target instead of oscillating on the same bad
        # approach forever.
        self._log(
            "target_dropped",
            target_id=self.target_id,
            target_name=self.target_name,
            reason=self.message,
        )
        self._clear_target()
        self._stop.wait(0.50)
        self._set_state("SEARCHING", "Recovering and selecting another target")

    def _loop(self):
        self._set_state("SEARCHING", "Searching for monster")

        while not self._stop.is_set():
            snapshot = authenticated_client_monitor.snapshot()

            if not snapshot.get("classic_pid"):
                self._set_state("IDLE", "Waiting for Classic.exe")
                self._stop.wait(0.30)
                continue

            if self.state in {"IDLE", "SEARCHING"}:
                self._step_searching(snapshot)
            elif self.state == "TARGET_SELECTED":
                self._step_target_selected(snapshot)
            elif self.state == "ROUTING":
                self._step_routing(snapshot)
            elif self.state == "APPROACHING":
                # APPROACHING is normally consumed synchronously by ROUTING.
                self._set_state("ROUTING", "Continue route")
            elif self.state == "ATTACK_READY":
                self._step_attack_ready(snapshot)
            elif self.state == "ATTACKING":
                self._step_attacking(snapshot)
            elif self.state == "WAITING_FOR_DEATH":
                self._step_waiting_for_death(snapshot)
            elif self.state == "TARGET_DEAD":
                self._step_target_dead()
            elif self.state == "FAILED":
                self._step_failed()
            else:
                self._set_state("FAILED", f"Unexpected AI state {self.state}")

        self.running = False
        self._clear_target()
        self._set_state("IDLE", "Hunting AI stopped")

    def start(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload:
            self.configure(payload)

        with self._lock:
            if self.running:
                return self.snapshot()
            if not authenticated_client_monitor.snapshot().get("classic_pid"):
                raise RuntimeError(
                    "Classic.exe is not detected. Launch SoulBound and enter the game first."
                )
            if not mouse_game_adapter.calibration_valid():
                raise RuntimeError(
                    "A valid screen calibration is required for the current Classic.exe window size."
                )

            self._stop.clear()
            self._clear_target()
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self.running = False
        self._clear_target()
        self.state = "IDLE"
        self.message = "Hunting AI stopped"
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "state": self.state,
                "message": self.message,
                "state_since": self.state_since,
                "target": (
                    {
                        "id": self.target_id,
                        "name": self.target_name,
                        "x": self.target_pos[0] if self.target_pos else None,
                        "y": self.target_pos[1] if self.target_pos else None,
                    }
                    if self.target_id is not None
                    else None
                ),
                "settings": {
                    "attack_range": self.attack_range,
                    "move_segment_tiles": self.move_segment_tiles,
                    "target_move_reset_tiles": self.target_move_reset_tiles,
                    "combat_confirm_timeout": self.attack_confirm_timeout,
                    "max_attack_retries": self.max_attack_retries,
                    "sprite_y_offset": mouse_game_adapter.sprite_y_offset,
                },
                "calibration": mouse_game_adapter.calibration_snapshot(),
                "actions": self.actions[-30:],
                "architecture": (
                    "OpenKore-style state machine in map coordinates; "
                    "mouse adapter only executes MOVE and ATTACK actions."
                ),
            }


hunting_ai = HuntingAI()
