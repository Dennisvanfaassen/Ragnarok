from __future__ import annotations

import math
import random
import threading
import time
from typing import Any

from core.mouse_adapter import mouse_game_adapter
from core.exploration import exploration_planner
from core.pathing import astar, build_pathing_state, clear_walk_line, nav_repository
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
    "LOOTING",
    "WANDERING",
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
        self._run_id = 0
        self.running = False

        self.state = "IDLE"
        self.message = "Idle"
        self.target_id: int | None = None
        self.target_name: str | None = None
        self.target_pos: tuple[int, int] | None = None
        self.route_target_pos: tuple[int, int] | None = None

        self.attack_range = 1
        self.move_segment_tiles = 6
        self.target_move_reset_tiles = 2
        self.attack_confirm_timeout = 0.65
        self.max_attack_retries = 3
        self.move_settle_timeout = 1.2
        self.direct_attack_click_range = 7
        self.attack_walk_timeout = 3.0
        self._attack_origin: tuple[int, int] | None = None
        self.loot_radius = 12
        self.recent_kills: list[dict[str, Any]] = []
        self.loot_retry: dict[int, int] = {}
        self.wander_min_distance = 16
        self.wander_max_distance = 32
        self.wander_lookahead = 10
        self.wander_cursor_radius = 175
        self.wander_turn_pixel_threshold = 22
        self.wander_path: list[tuple[int, int]] = []
        self.wander_goal: tuple[int, int] | None = None

        self.attack_retry = 0
        self.attack_clicked_at = 0.0
        self._attack_origin = None
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
        previous = self.state
        if previous == "WANDERING" and state != "WANDERING":
            mouse_game_adapter.release_hold_move()
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

    def _lock_actor(self, actor: dict[str, Any], reason: str) -> bool:
        x, y = actor.get("x"), actor.get("y")
        if x is None or y is None:
            return False
        self.target_id = int(actor["id"])
        self.target_name = str(actor.get("name") or f"Monster #{self.target_id}")
        self.target_pos = (int(x), int(y))
        self.route_target_pos = None
        self.attack_retry = 0
        self._log(
            "target_locked",
            target_id=self.target_id,
            target_name=self.target_name,
            x=self.target_pos[0],
            y=self.target_pos[1],
            reason=reason,
        )
        return True

    def _acquire_aggressor(self, snapshot: dict[str, Any]) -> bool:
        live = snapshot.get("live_state") or {}
        ids = set(live.get("aggressor_ids") or [])
        if not ids:
            return False
        player = self._position(snapshot)
        actors = [
            actor
            for actor in (live.get("actors") or [])
            if actor.get("kind") == "monster"
            and int(actor.get("id") or -1) in ids
        ]
        if not actors:
            return False
        if player:
            actors.sort(
                key=lambda actor: max(
                    abs(int(actor.get("x") or player[0]) - player[0]),
                    abs(int(actor.get("y") or player[1]) - player[1]),
                )
            )
        return self._lock_actor(actors[0], "aggressor")

    def _acquire_target(self, snapshot: dict[str, Any]) -> bool:
        profile = app_state.get_profile()
        targeting = build_targeting_state(snapshot, profile.hunt.monsters)
        selected = targeting.get("selected")
        if not selected:
            return False

        return self._lock_actor(selected, "normal_target")

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
        """Return on first useful movement instead of waiting for a full stop."""
        deadline = time.time() + self.move_settle_timeout
        last_pos = origin

        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(0.06)
            snapshot = authenticated_client_monitor.snapshot()

            if self._find_actor(snapshot, target_id) is None:
                return None

            pos = self._position(snapshot)
            if pos is None:
                continue

            if self._tile_distance(pos, destination) <= 1:
                return pos

            if pos != last_pos:
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

    def _loot_candidates(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        items = list(((snapshot.get("live_state") or {}).get("floor_items") or []))
        if not items or not self.recent_kills:
            return []

        now = time.time()
        self.recent_kills = [
            kill for kill in self.recent_kills
            if now - float(kill["time"]) <= 30.0
        ]
        if not self.recent_kills:
            return []

        result = []
        for item in items:
            ix, iy = item.get("x"), item.get("y")
            if ix is None or iy is None:
                continue
            seen = float(item.get("last_seen") or 0)
            for kill in self.recent_kills:
                kx, ky = kill["pos"]
                if seen + 1.0 < float(kill["time"]):
                    continue
                if max(abs(int(ix) - kx), abs(int(iy) - ky)) <= self.loot_radius:
                    result.append(item)
                    break

        player = self._position(snapshot)
        if player:
            result.sort(
                key=lambda item: max(
                    abs(int(item["x"]) - player[0]),
                    abs(int(item["y"]) - player[1]),
                )
            )
        return result

    def _choose_wander_path(self, snapshot: dict[str, Any]) -> bool:
        player = self._position(snapshot)
        map_name = self._world(snapshot).get("map")
        if player is None or not map_name:
            return False
        try:
            grid, _ = nav_repository.load(str(map_name))
        except Exception:
            return False

        path = exploration_planner.choose_route(
            str(map_name),
            grid,
            player,
        )
        if not path:
            return False

        self.wander_path = path
        self.wander_goal = path[-1]
        self._log(
            "wander_route",
            goal={"x": self.wander_goal[0], "y": self.wander_goal[1]},
            steps=len(path) - 1,
            strategy="coverage_heading",
            exploration=exploration_planner.snapshot(),
        )
        return True

    def _nearest_wander_index(self, player: tuple[int, int]) -> int:
        if not self.wander_path:
            return 0
        best_i = 0
        best_d = 999999
        for i, point in enumerate(self.wander_path):
            d = max(abs(point[0] - player[0]), abs(point[1] - player[1]))
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _step_searching(self, snapshot: dict[str, Any]):
        if self._acquire_aggressor(snapshot):
            if self._attack_locked_immediately(
                snapshot,
                reason="search_aggressor",
            ):
                return
            self._set_state(
                "TARGET_SELECTED",
                f"Prioritizing aggressor {self.target_name}",
            )
            return

        loot = self._loot_candidates(snapshot)
        if loot:
            self._set_state("LOOTING", f"Looting {len(loot)} floor item(s)")
            return

        if self._acquire_target(snapshot):
            if self._attack_locked_immediately(
                snapshot,
                reason="search_target",
            ):
                return
            self._set_state(
                "TARGET_SELECTED",
                f"Locked {self.target_name}",
            )
            return

        self._set_state("WANDERING", "No target visible; wandering")

    def _attack_locked_immediately(
        self,
        snapshot: dict[str, Any],
        *,
        reason: str,
    ) -> bool:
        """Interrupt movement and attack the locked visible actor in this cycle."""
        mouse_game_adapter.release_hold_move()

        # Re-read state after mouse-up. The player may still be completing the
        # previous RO movement command, so never click using the stale wander
        # snapshot if a fresher one is available.
        fresh = authenticated_client_monitor.snapshot()
        actor = self._refresh_locked_target(fresh)
        player = self._position(fresh)

        if actor is None or player is None or self.target_pos is None:
            return False

        if not mouse_game_adapter.can_project(
            player,
            self.target_pos,
            sprite=True,
        ):
            return False

        result = mouse_game_adapter.attack(
            player,
            self.target_pos,
            retry_index=0,
        )
        self._log(
            "instant_attack",
            target_id=self.target_id,
            target_name=self.target_name,
            reason=reason,
            result=result,
        )

        if not result.get("ok"):
            return False

        self.attack_retry = 0
        self.attack_clicked_at = time.time()
        self._attack_origin = player
        self._set_state(
            "ATTACKING",
            f"Instant attack on {self.target_name}; waiting for combat confirmation",
        )
        return True

    def _step_target_selected(self, snapshot: dict[str, Any]):
        actor = self._refresh_locked_target(snapshot)
        if actor is None:
            self._set_state("TARGET_DEAD", "Target disappeared before approach")
            return

        player = self._position(snapshot)
        if (
            player is not None
            and self.target_pos is not None
            and mouse_game_adapter.can_project(
                player,
                self.target_pos,
                sprite=True,
            )
        ):
            if self._attack_locked_immediately(
                snapshot,
                reason="visible_target_selected",
            ):
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

        # Player-like behavior: if the monster is already visible/clickable,
        # attack immediately and let Ragnarok perform the final approach.
        try:
            grid, _ = nav_repository.load(str(self._world(snapshot).get("map")))
        except Exception:
            grid = None

        if (
            mouse_game_adapter.can_project(
                player,
                self.target_pos,
                sprite=True,
            )
            and grid is not None
        ):
            if clear_walk_line(grid, player, self.target_pos):
                self._set_state(
                    "ATTACK_READY",
                    f"{self.target_name} is visible; attacking immediately",
                )
                return

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
            if (
                self.target_pos is not None
                and mouse_game_adapter.can_project(
                    player,
                    self.target_pos,
                    sprite=True,
                )
            ):
                self._log(
                    "route_unavailable_attack_visible",
                    target_id=self.target_id,
                    message=pathing.get("message"),
                )
                self._set_state(
                    "ATTACK_READY",
                    f"Navigation unavailable; attacking visible {self.target_name}",
                )
                return

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
        if not mouse_game_adapter.can_project(
            player,
            self.target_pos,
            sprite=True,
        ):
            self._set_state(
                "ROUTING",
                f"{self.target_name} moved out of clickable range",
            )
            return

        fresh = authenticated_client_monitor.snapshot()
        actor = self._refresh_locked_target(fresh)
        player = self._position(fresh)
        if actor is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return
        if player is None or self.target_pos is None:
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
        self._attack_origin = player
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

        elapsed = time.time() - self.attack_clicked_at
        current_pos = self._position(snapshot)
        moved_after_click = (
            self._attack_origin is not None
            and current_pos is not None
            and current_pos != self._attack_origin
        )

        if moved_after_click and elapsed < self.attack_walk_timeout:
            self._stop.wait(0.06)
            return

        if elapsed < self.attack_confirm_timeout:
            self._stop.wait(0.05)
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
                f"No combat confirmation; quick retry {self.attack_retry + 1}/{self.max_attack_retries}",
            )
            return

        self.attack_retry = 0
        self._set_state(
            "ROUTING",
            "Attack click missed; recalculating from live position",
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
        old_pos = self.target_pos
        self._log(
            "target_finished",
            target_id=old_id,
            target_name=old_name,
        )
        if old_pos is not None:
            self.recent_kills.append({"time": time.time(), "pos": old_pos})
            self.recent_kills = self.recent_kills[-10:]
        self._clear_target()

        snapshot = authenticated_client_monitor.snapshot()
        if self._acquire_aggressor(snapshot):
            self._set_state(
                "TARGET_SELECTED",
                f"Aggressor priority: {self.target_name}",
            )
            return

        if self._loot_candidates(snapshot):
            self._set_state("LOOTING", "Combat clear; looting drops")
            return

        self._set_state("SEARCHING", "Selecting next monster")

    def _step_looting(self, snapshot: dict[str, Any]):
        if self._acquire_aggressor(snapshot):
            self._set_state(
                "TARGET_SELECTED",
                f"Interrupted loot for aggressor {self.target_name}",
            )
            return

        items = self._loot_candidates(snapshot)
        if not items:
            self.loot_retry.clear()
            self._set_state("SEARCHING", "Loot complete")
            return

        player = self._position(snapshot)
        if player is None:
            self._stop.wait(0.04)
            return

        item = items[0]
        item_id = int(item["id"])
        item_pos = (int(item["x"]), int(item["y"]))
        result = mouse_game_adapter.loot(player, item_pos)
        self._log(
            "loot_attempt",
            item_id=item_id,
            item_name_id=item.get("name_id"),
            x=item_pos[0],
            y=item_pos[1],
            result=result,
        )

        if not result.get("ok"):
            self.loot_retry[item_id] = self.loot_retry.get(item_id, 0) + 1
            self._stop.wait(0.06)
            return

        deadline = time.time() + 1.2
        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(0.04)
            fresh = authenticated_client_monitor.snapshot()

            if self._acquire_aggressor(fresh):
                self._set_state(
                    "TARGET_SELECTED",
                    f"Interrupted loot for aggressor {self.target_name}",
                )
                return

            ids = {
                int(entry["id"])
                for entry in ((fresh.get("live_state") or {}).get("floor_items") or [])
            }
            if item_id not in ids:
                self.loot_retry.pop(item_id, None)
                return

        self.loot_retry[item_id] = self.loot_retry.get(item_id, 0) + 1

    def _step_wandering(self, snapshot: dict[str, Any]):
        if self._acquire_aggressor(snapshot):
            if self._attack_locked_immediately(
                snapshot,
                reason="wander_aggressor_interrupt",
            ):
                return
            self._set_state(
                "TARGET_SELECTED",
                f"Aggressor spotted: {self.target_name}",
            )
            return

        if self._acquire_target(snapshot):
            if self._attack_locked_immediately(
                snapshot,
                reason="wander_target_interrupt",
            ):
                return
            self._set_state(
                "TARGET_SELECTED",
                f"Monster spotted: {self.target_name}",
            )
            return

        player = self._position(snapshot)
        map_name = self._world(snapshot).get("map")
        if player is None or not map_name:
            self._stop.wait(0.03)
            return

        exploration_planner.observe(str(map_name), player)

        if (
            not self.wander_path
            or self.wander_goal is None
            or self._tile_distance(player, self.wander_goal) <= 2
        ):
            mouse_game_adapter.release_hold_move()
            self.wander_path = []
            self.wander_goal = None
            if not self._choose_wander_path(snapshot):
                self._stop.wait(0.10)
                return

        index = self._nearest_wander_index(player)
        if index >= len(self.wander_path) - 1:
            mouse_game_adapter.release_hold_move()
            self.wander_path = []
            self.wander_goal = None
            return

        # Look well ahead on the route and steer by DIRECTION, not by
        # individual cell coordinates. This mirrors normal RO mouse walking:
        # hold the button at a stable distance from the character and only
        # rotate the cursor when the route bends.
        lookahead = min(
            len(self.wander_path) - 1,
            index + self.wander_lookahead,
        )
        destination = self.wander_path[lookahead]
        dx = destination[0] - player[0]
        dy = destination[1] - player[1]

        result = mouse_game_adapter.update_hold_direction(
            dx,
            dy,
            radius_px=self.wander_cursor_radius,
            min_pixel_change=self.wander_turn_pixel_threshold,
        )

        if not result.get("ok"):
            # Try a shorter directional lookahead, but never fall back to
            # rapid per-cell clicking while wandering.
            for short in (7, 5, 3):
                idx = min(len(self.wander_path) - 1, index + short)
                destination = self.wander_path[idx]
                dx = destination[0] - player[0]
                dy = destination[1] - player[1]
                result = mouse_game_adapter.update_hold_direction(
                    dx,
                    dy,
                    radius_px=self.wander_cursor_radius,
                    min_pixel_change=14,
                )
                if result.get("ok"):
                    break

        if not result.get("ok"):
            mouse_game_adapter.release_hold_move()
            self.wander_path = []
            self.wander_goal = None
            self._stop.wait(0.08)
            return

        self.message = (
            f"Wandering smoothly toward {self.wander_goal[0]},{self.wander_goal[1]}"
        )
        self._stop.wait(0.08)

    def _step_failed(self):
        snapshot = authenticated_client_monitor.snapshot()
        actor = self._refresh_locked_target(snapshot)
        player = self._position(snapshot)

        if actor is not None and player is not None and self.target_pos is not None:
            if mouse_game_adapter.can_project(
                player,
                self.target_pos,
                sprite=True,
            ):
                self._set_state(
                    "ATTACK_READY",
                    f"Recovering by attacking visible {self.target_name}",
                )
                return

            self._log(
                "target_retry",
                target_id=self.target_id,
                target_name=self.target_name,
                reason=self.message,
            )
            self._stop.wait(0.25)
            self._set_state("ROUTING", "Retrying same locked target")
            return

        self._clear_target()
        self._set_state("SEARCHING", "Target gone; selecting another target")

    def _loop(self, run_id: int):
        self._set_state("SEARCHING", "Searching for monster")

        while not self._stop.is_set() and run_id == self._run_id:
            snapshot = authenticated_client_monitor.snapshot()

            if not snapshot.get("classic_pid"):
                self._set_state("IDLE", "Waiting for Classic.exe")
                self._stop.wait(0.10)
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
            elif self.state == "LOOTING":
                self._step_looting(snapshot)
            elif self.state == "WANDERING":
                self._step_wandering(snapshot)
            elif self.state == "FAILED":
                self._step_failed()
            else:
                self._set_state("FAILED", f"Unexpected AI state {self.state}")

        # A previous worker must never overwrite a newer run's state.
        if run_id == self._run_id:
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

            self._run_id += 1
            run_id = self._run_id
            self._stop.clear()
            self._clear_target()
            self.running = True
            self._thread = threading.Thread(
                target=self._loop,
                args=(run_id,),
                daemon=True,
            )
            self._thread.start()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._run_id += 1
        self._stop.set()
        mouse_game_adapter.release_hold_move()
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
                    "direct_attack_click_range": self.direct_attack_click_range,
                    "attack_walk_timeout": self.attack_walk_timeout,
                    "loot_radius": self.loot_radius,
                    "wander_lookahead": self.wander_lookahead,
                    "wander_cursor_radius": self.wander_cursor_radius,
                    "wander_turn_pixel_threshold": self.wander_turn_pixel_threshold,
                    "exploration": exploration_planner.snapshot(),
                },
                "calibration": mouse_game_adapter.calibration_snapshot(),
                "actions": self.actions[-30:],
                "architecture": (
                    "OpenKore-style state machine in map coordinates; "
                    "mouse adapter only executes MOVE and ATTACK actions."
                ),
            }


hunting_ai = HuntingAI()
