from __future__ import annotations

import math
import random
import threading
import time
from typing import Any

from core.game_actions import game_actions
from core.exploration import exploration_planner
from core.hunt_routes import hunt_route_store
from core.openkore_data import item_name
from core.pathing import astar, build_pathing_state, clear_walk_line, nav_repository
from core.state import app_state
from core.targeting import build_targeting_state
from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.hunt_recorder import hunting_diagnostic_recorder
from diagnostics.native_action_bridge import native_action_bridge


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
        self.attack_confirm_timeout = 0.35
        self.max_attack_retries = 1
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
        self.wander_cursor_radius = 180
        self.wander_turn_pixel_threshold = 18
        self.wander_path: list[tuple[int, int]] = []
        self.wander_goal: tuple[int, int] | None = None
        self.wander_progress_index = 0
        self.wander_line_points: list[tuple[int, int]] = []
        self.wander_line_index = 0
        self.wander_reconnect_target: tuple[int, int] | None = None
        self.wander_reconnect_route_index: int | None = None
        self._native_wander_destination: tuple[int, int] | None = None
        self._native_wander_sent_at = 0.0
        self.saved_route_map: str | None = None
        self.saved_route_waypoint_index = 0
        self.saved_route_direction = 1
        self.saved_route_mode = "loop"
        self._attack_reposition_required = False
        self._attack_reposition_origin: tuple[int, int] | None = None
        self._attack_route_anchor: tuple[int, int] | None = None

        self.attack_retry = 0
        self.attack_clicked_at = 0.0
        self._combat_click_locked = False
        self._combat_seen = False
        self._attack_origin = None
        self._attack_backend = "mouse"
        self._last_heal_hotkey_at = 0.0
        self._last_heal_result: dict[str, Any] | None = None
        self._return_weight_reached = False
        self._last_teleport_at = 0.0
        self._last_teleport_reason: str | None = None
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
        game_actions.configure(
            sprite_y_offset=payload.get("sprite_y_offset")
        )

    def _set_state(self, state: str, message: str):
        if state not in STATES:
            raise ValueError(f"Unknown AI state: {state}")
        previous = self.state
        if previous == "WANDERING" and state != "WANDERING":
            game_actions.release_hold_move()
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

        screenshot_actions = {
            "instant_attack",
            "attack_attempt",
            "attack_precision_retry",
            "client_attack_registered",
            "route_failed",
            "target_finished",
            "move_fallback",
        }
        hunting_diagnostic_recorder.event(
            "hunting_ai",
            action,
            details,
            screenshot=action in screenshot_actions,
            screenshot_cooldown=0.20,
        )

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
    def _render_position(
        snapshot: dict[str, Any],
    ) -> tuple[float, float] | None:
        world = HuntingAI._world(snapshot)
        x = world.get("render_x", world.get("x"))
        y = world.get("render_y", world.get("y"))
        if x is None or y is None:
            return None
        return float(x), float(y)

    def _target_render_position(
        self,
        snapshot: dict[str, Any],
    ) -> tuple[float, float] | None:
        actor = self._find_actor(snapshot, self.target_id)
        if actor is None:
            return None
        x = actor.get("render_x", actor.get("x"))
        y = actor.get("render_y", actor.get("y"))
        if x is None or y is None:
            return None
        return float(x), float(y)

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
        self._combat_click_locked = False
        self._combat_seen = False
        self._attack_reposition_required = False
        self._attack_reposition_origin = None
        self._attack_route_anchor = None
        self._attack_backend = "mouse"

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

    @staticmethod
    def _monster_rule(name: str | None):
        normalized = str(name or "").strip().lower()
        if not normalized:
            return None
        for rule in app_state.get_profile().hunt.monster_rules:
            if str(rule.monster or "").strip().lower() == normalized:
                return rule
        return None

    def _monster_behavior(self, name: str | None) -> str:
        rule = self._monster_rule(name)
        if rule is None:
            return "attack"
        if not rule.enabled:
            return "ignore"
        return str(rule.behavior or "attack").strip().lower()

    def _use_teleport_item(self, snapshot: dict[str, Any], reason: str) -> bool:
        now = time.time()
        if now - self._last_teleport_at < 1.5:
            return False

        profile = app_state.get_profile()
        wanted_name = str(profile.hunt.teleport_item or "Fly Wing").strip().lower()
        items = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
        item = next(
            (
                row for row in items
                if str(row.get("name") or "").strip().lower() == wanted_name
                or (
                    wanted_name == "fly wing"
                    and int(row.get("name_id") or -1) == 601
                )
            ),
            None,
        )
        if item is None:
            self._log("teleport_failed", reason=reason, error="teleport_item_not_found")
            return False

        world = self._world(snapshot)
        target_id = world.get("self_account_id") or world.get("self_char_id")
        if target_id is None:
            self._log("teleport_failed", reason=reason, error="self_target_id_unknown")
            return False

        game_actions.release_hold_move()
        result = native_action_bridge.item_use(int(item["index"]), int(target_id))
        self._log(
            "teleport_item_use",
            reason=reason,
            item=item.get("name"),
            inventory_index=item.get("index"),
            result=result,
        )
        if not result.get("ok"):
            return False

        self._last_teleport_at = now
        self._last_teleport_reason = reason
        self._clear_target()
        self.wander_path = []
        self.wander_goal = None
        self.wander_line_points = []
        self.wander_line_index = 0
        self._set_state("SEARCHING", f"Teleported: {reason}")
        self._stop.wait(0.45)
        return True

    def _maybe_emergency_action(self, snapshot: dict[str, Any]) -> bool:
        profile = app_state.get_profile()
        action = str(profile.hunt.emergency_action or "none").strip().lower()
        if action == "none":
            return False

        world = self._world(snapshot)
        hp = world.get("hp")
        hp_max = world.get("hp_max")
        if hp is None or not hp_max or int(hp) <= 0:
            return False
        hp_percent = float(world.get("hp_percent") or (int(hp) * 100 / int(hp_max)))
        threshold = max(1, min(99, int(profile.hunt.emergency_hp_percent)))
        if hp_percent >= threshold:
            return False

        if action == "teleport":
            return self._use_teleport_item(
                snapshot,
                f"emergency HP {hp_percent:.1f}% < {threshold}%",
            )

        if action == "stop":
            game_actions.release_hold_move()
            self._log(
                "emergency_stop",
                hp_percent=hp_percent,
                threshold=threshold,
            )
            self._set_state("FAILED", f"Emergency stop at {hp_percent:.1f}% HP")
            self._stop.set()
            return True

        return False

    def _maybe_teleport_for_monster(self, snapshot: dict[str, Any]) -> bool:
        actors = ((snapshot.get("live_state") or {}).get("actors") or [])
        teleport_names = {
            str(rule.monster or "").strip().lower()
            for rule in app_state.get_profile().hunt.monster_rules
            if rule.enabled and str(rule.behavior or "").strip().lower() == "teleport"
        }
        if not teleport_names:
            return False

        player = self._position(snapshot)
        visible = []
        for actor in actors:
            if actor.get("kind") != "monster":
                continue
            name = str(actor.get("name") or "").strip()
            if name.lower() not in teleport_names:
                continue
            if player and actor.get("x") is not None and actor.get("y") is not None:
                distance = max(
                    abs(int(actor["x"]) - player[0]),
                    abs(int(actor["y"]) - player[1]),
                )
            else:
                distance = 999999
            visible.append((distance, name, actor))

        if not visible:
            return False
        visible.sort(key=lambda row: row[0])
        distance, name, _ = visible[0]
        return self._use_teleport_item(
            snapshot,
            f"{name} spotted at {distance} tiles",
        )

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
            and self._monster_behavior(actor.get("name")) in {"attack", "aggressor_only"}
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
        player = self._position(snapshot)
        candidates = []

        for actor in targeting.get("candidates") or []:
            rule = self._monster_rule(actor.get("name"))
            behavior = self._monster_behavior(actor.get("name"))
            if behavior != "attack":
                continue

            tile_distance = actor.get("tile_distance")
            if rule is not None and tile_distance is not None:
                if int(rule.min_distance or 0) > 0 and int(tile_distance) < int(rule.min_distance):
                    continue
                if int(rule.max_distance or 0) > 0 and int(tile_distance) > int(rule.max_distance):
                    continue

            priority = int(rule.priority) if rule is not None else 50
            candidates.append((priority, int(tile_distance or 0), actor))

        if not candidates:
            return False

        candidates.sort(key=lambda row: (row[0], row[1], int(row[2].get("id") or 0)))
        return self._lock_actor(candidates[0][2], "normal_target")

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

    def _client_attack_registered(
        self,
        snapshot: dict[str, Any],
        target_id: int,
        since: float,
    ) -> bool:
        action = self._world(snapshot).get("last_client_action") or {}
        try:
            return (
                int(action.get("target_id")) == int(target_id)
                and float(action.get("timestamp") or 0) >= since
                and int(action.get("type") or -1) in {7, 0}
            )
        except Exception:
            return False

    def _wait_for_move_progress(
        self,
        origin: tuple[int, int],
        destination: tuple[int, int],
        target_id: int,
    ) -> tuple[int, int] | None:
        """Let one issued movement click finish its useful travel.

        Replanning on the first changed tile caused moving targets to produce a
        ROUTING/APPROACHING loop every ~150 ms. Commit to the current segment
        until we reach it, stop moving briefly, or hit the normal timeout.
        """
        deadline = time.time() + self.move_settle_timeout
        last_pos = origin
        last_change = time.time()
        moved = False

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
                moved = True
                last_pos = pos
                last_change = time.time()
                continue

            if moved and time.time() - last_change >= 0.22:
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

    def _loot_allowed_for_kill(
        self,
        monster_name: str | None,
        item: dict[str, Any],
    ) -> bool:
        hunt = app_state.get_profile().hunt
        normalized_monster = str(monster_name or "").strip().lower()
        rule = next(
            (
                row for row in hunt.monster_rules
                if str(row.monster or "").strip().lower() == normalized_monster
            ),
            None,
        )
        if rule is None:
            return bool(hunt.loot_all)
        if not rule.enabled:
            return False

        mode = str(rule.loot_mode or "all").strip().lower()
        if mode == "none":
            return False

        name_id = item.get("name_id")
        resolved = item_name(int(name_id)) if name_id is not None else ""
        normalized_item = resolved.strip().lower()
        include = {str(x).strip().lower() for x in rule.include_items if str(x).strip()}
        exclude = {str(x).strip().lower() for x in rule.exclude_items if str(x).strip()}

        if mode == "selected":
            return normalized_item in include
        if mode == "excluded":
            return normalized_item not in exclude
        return normalized_item not in exclude

    def _loot_candidates(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        hunt = app_state.get_profile().hunt
        if not hunt.loot_all and not hunt.monster_rules:
            return []
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
                    if self._loot_allowed_for_kill(kill.get("monster"), item):
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

    def _reset_saved_route_if_map_changed(self, map_name: str):
        if self.saved_route_map != map_name:
            self.saved_route_map = map_name
            self.saved_route_waypoint_index = 0
            self.saved_route_direction = 1
            self.saved_route_mode = "loop"

    def _advance_saved_route(self, count: int, mode: str):
        if count <= 1:
            self.saved_route_waypoint_index = 0
            return

        if mode == "pingpong":
            nxt = self.saved_route_waypoint_index + self.saved_route_direction
            if nxt >= count:
                self.saved_route_direction = -1
                nxt = count - 2
            elif nxt < 0:
                self.saved_route_direction = 1
                nxt = 1
            self.saved_route_waypoint_index = max(0, min(count - 1, nxt))
            return

        self.saved_route_waypoint_index = (
            self.saved_route_waypoint_index + 1
        ) % count

    def _saved_route_target(
        self,
        map_name: str,
        player: tuple[int, int],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        route = hunt_route_store.get(map_name)
        points = route.get("waypoints") or []
        if len(points) < 2:
            return None, route

        self._reset_saved_route_if_map_changed(map_name)
        self.saved_route_mode = str(route.get("mode") or "loop")
        self.saved_route_waypoint_index = max(
            0,
            min(self.saved_route_waypoint_index, len(points) - 1),
        )

        target = points[self.saved_route_waypoint_index]
        target_pos = (int(target["x"]), int(target["y"]))

        # Reaching a waypoint advances exactly one route step. Combat does not
        # change this index, so hunting resumes where the patrol was interrupted.
        if self._tile_distance(player, target_pos) <= 2:
            self._advance_saved_route(len(points), self.saved_route_mode)
            target = points[self.saved_route_waypoint_index]

        return target, route

    def _choose_wander_path(self, snapshot: dict[str, Any]) -> bool:
        player = self._position(snapshot)
        map_name = self._world(snapshot).get("map")
        if player is None or not map_name:
            return False

        map_name = str(map_name)
        try:
            grid, _ = nav_repository.load(map_name)
        except Exception:
            return False

        navigation_mode = str(
            app_state.get_profile().hunt.navigation_mode or "saved_or_explore"
        ).strip().lower()

        # A user-defined hunting route normally has priority over autonomous
        # exploration. The Hunting tab can force saved-route-only or
        # exploration-only behavior.
        if navigation_mode == "explore_only":
            saved_target, saved_route = None, {"waypoints": []}
        else:
            saved_target, saved_route = self._saved_route_target(
                map_name,
                player,
            )

        if saved_target is not None:
            goal = (
                int(saved_target["x"]),
                int(saved_target["y"]),
            )
            path = astar(
                grid,
                player,
                goal,
                max_expansions=150000,
                clearance_weight=0.85,
            )
            if path and len(path) >= 2:
                self._set_wander_route(grid, path, goal)
                self._log(
                    "saved_hunt_route",
                    waypoint_index=self.saved_route_waypoint_index,
                    waypoint_number=self.saved_route_waypoint_index + 1,
                    mode=self.saved_route_mode,
                    goal={"x": goal[0], "y": goal[1]},
                    steps=len(path) - 1,
                )
                return True

            # If we're already effectively on this waypoint, advance and retry
            # immediately once rather than falling back to random exploration.
            points = saved_route.get("waypoints") or []
            if points and self._tile_distance(player, goal) <= 2:
                self._advance_saved_route(len(points), self.saved_route_mode)
                nxt = points[self.saved_route_waypoint_index]
                goal = (int(nxt["x"]), int(nxt["y"]))
                path = astar(
                    grid,
                    player,
                    goal,
                    max_expansions=150000,
                    clearance_weight=0.85,
                )
                if path and len(path) >= 2:
                    self._set_wander_route(grid, path, goal)
                    return True

            return False

        if navigation_mode == "saved_only":
            self._log(
                "saved_route_required",
                map=map_name,
                message="No usable saved route is available.",
            )
            return False

        path = exploration_planner.choose_route(
            map_name,
            grid,
            player,
        )
        if not path:
            return False

        self._set_wander_route(grid, path, path[-1])
        self._log(
            "wander_route",
            goal={"x": self.wander_goal[0], "y": self.wander_goal[1]},
            steps=len(path) - 1,
            strategy="coverage_heading",
            exploration=exploration_planner.snapshot(),
        )
        return True

    @staticmethod
    def _angle_between_vectors(
        a: tuple[int, int],
        b: tuple[int, int],
    ) -> float:
        alen = math.hypot(a[0], a[1])
        blen = math.hypot(b[0], b[1])
        if alen < 1e-6 or blen < 1e-6:
            return 180.0
        dot = max(
            -1.0,
            min(1.0, (a[0] * b[0] + a[1] * b[1]) / (alen * blen)),
        )
        return math.degrees(math.acos(dot))

    def _merge_shallow_wander_bends(
        self,
        grid,
        points: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """Remove route vertices that create only a very shallow bend."""
        if len(points) <= 2:
            return points[:]

        merged = [points[0]]
        i = 1
        while i < len(points) - 1:
            prev = merged[-1]
            current = points[i]
            nxt = points[i + 1]

            v1 = (current[0] - prev[0], current[1] - prev[1])
            v2 = (nxt[0] - current[0], nxt[1] - current[1])
            angle = self._angle_between_vectors(v1, v2)
            total_distance = max(
                abs(nxt[0] - prev[0]),
                abs(nxt[1] - prev[1]),
            )

            if (
                angle <= 11.0
                and total_distance <= 24
                and clear_walk_line(grid, prev, nxt)
            ):
                i += 1
                continue

            merged.append(current)
            i += 1

        merged.append(points[-1])
        return merged

    def _anticipated_wander_direction(
        self,
        grid,
        player: tuple[int, int],
        destination: tuple[int, int],
    ) -> tuple[int, int]:
        """Preview a safe bend shortly before the current segment ends."""
        if self.wander_line_index >= len(self.wander_line_points) - 1:
            return (
                destination[0] - player[0],
                destination[1] - player[1],
            )

        if self._tile_distance(player, destination) > 5:
            return (
                destination[0] - player[0],
                destination[1] - player[1],
            )

        next_destination = self.wander_line_points[self.wander_line_index + 1]
        vx = next_destination[0] - destination[0]
        vy = next_destination[1] - destination[1]
        length = max(abs(vx), abs(vy), 1)

        # Preview only a few cells into the next segment. The preview itself
        # must have a completely walkable line from the current player cell.
        preview_steps = min(3, length)
        preview = (
            int(round(destination[0] + vx * preview_steps / length)),
            int(round(destination[1] + vy * preview_steps / length)),
        )
        if not grid.walkable(*preview):
            return (
                destination[0] - player[0],
                destination[1] - player[1],
            )
        if not clear_walk_line(grid, player, preview):
            return (
                destination[0] - player[0],
                destination[1] - player[1],
            )

        return (
            preview[0] - player[0],
            preview[1] - player[1],
        )

    def _build_straight_wander_segments(
        self,
        grid,
        path: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """Compress A* into the longest safe straight walking segments."""
        if not path:
            return []
        if len(path) <= 2:
            return path[:]

        result = [path[0]]
        anchor_index = 0

        while anchor_index < len(path) - 1:
            chosen = anchor_index + 1

            # Pick the furthest later A* cell that can be reached by one
            # completely walkable straight line from this anchor.
            max_idx = min(len(path) - 1, anchor_index + 18)
            for idx in range(max_idx, anchor_index, -1):
                if clear_walk_line(grid, path[anchor_index], path[idx]):
                    chosen = idx
                    break

            result.append(path[chosen])
            anchor_index = chosen

        return self._merge_shallow_wander_bends(grid, result)

    def _set_wander_route(
        self,
        grid,
        path: list[tuple[int, int]],
        goal: tuple[int, int],
    ):
        self.wander_path = path
        self.wander_goal = goal
        self.wander_progress_index = 0
        self.wander_line_points = self._build_straight_wander_segments(
            grid,
            path,
        )
        self.wander_line_index = 1 if len(self.wander_line_points) > 1 else 0
        self.wander_reconnect_target = None
        self.wander_reconnect_route_index = None
        self._native_wander_destination = None
        self._native_wander_sent_at = 0.0

    def _nearest_wander_index(self, player: tuple[int, int]) -> int:
        if not self.wander_path:
            return 0

        # Progress can only move forward. Search a limited window ahead from
        # the last accepted path index so crossing/looping routes cannot make
        # the character suddenly turn back toward an older path segment.
        start = max(0, min(self.wander_progress_index, len(self.wander_path) - 1))
        end = min(len(self.wander_path), start + 24)

        best_i = start
        best_d = 999999
        for i in range(start, end):
            point = self.wander_path[i]
            d = max(abs(point[0] - player[0]), abs(point[1] - player[1]))
            if d < best_d:
                best_d = d
                best_i = i

        self.wander_progress_index = max(self.wander_progress_index, best_i)
        return self.wander_progress_index


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
        game_actions.release_hold_move()

        # Give Classic.exe one render slice to stop held-mouse steering, then
        # project the monster from fresh network coordinates. This is short
        # enough to feel instant but avoids aiming with the last walking frame.
        self._stop.wait(0.025)
        fresh = authenticated_client_monitor.snapshot()
        actor = self._refresh_locked_target(fresh)
        player = self._position(fresh)

        if actor is None or player is None or self.target_pos is None:
            return False

        player_render = self._render_position(fresh)
        target_render = self._target_render_position(fresh)
        if player_render is None or target_render is None:
            return False

        if not game_actions.can_project(
            player_render,
            target_render,
            sprite=True,
        ):
            return False

        self.attack_clicked_at = time.time()
        result = game_actions.attack(
            player_render,
            target_render,
            retry_index=0,
            target_key=self.target_name,
            actor_id=int(self.target_id) if self.target_id is not None else None,
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

        self._attack_backend = str(result.get("backend") or "mouse")
        self.attack_retry = 0
        self._attack_origin = player
        self._combat_click_locked = False
        self._combat_seen = False
        self._set_state(
            "ATTACKING",
            f"Attack click sent to {self.target_name}; verifying actor click",
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
            and self._render_position(snapshot) is not None
            and self._target_render_position(snapshot) is not None
            and game_actions.can_project(
                self._render_position(snapshot),
                self._target_render_position(snapshot),
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

        if self._attack_reposition_required and distance <= 3:
            self._attack_reposition_required = False
            self._attack_reposition_origin = None
            self._attack_route_anchor = None
            self._set_state(
                "ATTACK_READY",
                f"Close retry on {self.target_name}",
            )
            return

        # After a missed actor click, approach a latched monster position
        # instead of rebuilding the route every time the monster moves one cell.
        # Refresh that anchor only after we have actually reached the old one.
        if self._attack_reposition_required:
            if self._attack_route_anchor is None:
                self._attack_route_anchor = self.target_pos
            elif (
                self._tile_distance(player, self._attack_route_anchor) <= 2
                and distance > 3
            ):
                old_anchor = self._attack_route_anchor
                self._attack_route_anchor = self.target_pos
                self._log(
                    "attack_route_anchor_refresh",
                    target_id=self.target_id,
                    from_pos={"x": old_anchor[0], "y": old_anchor[1]},
                    to_pos={
                        "x": self._attack_route_anchor[0],
                        "y": self._attack_route_anchor[1],
                    },
                )

        # Player-like behavior: if the monster is already visible/clickable,
        # attack immediately and let Ragnarok perform the final approach.
        try:
            grid, _ = nav_repository.load(str(self._world(snapshot).get("map")))
        except Exception:
            grid = None

        if (
            not self._attack_reposition_required
            and self._render_position(snapshot) is not None
            and self._target_render_position(snapshot) is not None
            and game_actions.can_project(
                self._render_position(snapshot),
                self._target_render_position(snapshot),
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

        approach_target = (
            self._attack_route_anchor
            if self._attack_reposition_required and self._attack_route_anchor
            else self.target_pos
        )
        targeting = {
            "selected": {
                "id": self.target_id,
                "name": self.target_name,
                "x": approach_target[0],
                "y": approach_target[1],
            }
        }
        pathing = build_pathing_state(snapshot, targeting, nav_repository)
        path_preview = pathing.get("path_preview") or []

        if not pathing.get("path_found") or len(path_preview) < 2:
            if (
                self.target_pos is not None
                and game_actions.can_project(
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
            if self._attack_reposition_required and len(path_preview) > 1:
                idx = min(2, len(path_preview) - 1)
                segment = (
                    int(path_preview[idx]["x"]),
                    int(path_preview[idx]["y"]),
                )
            else:
                self._set_state("ATTACK_READY", "At target approach position")
                return

        self.route_target_pos = approach_target
        self._set_state(
            "APPROACHING",
            f"Approaching {self.target_name} via {segment[0]},{segment[1]}",
        )

        result = game_actions.move(player, segment)
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
                result = game_actions.move(player, fallback)
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

        if end_pos != player and not self._attack_reposition_required:
            self._attack_reposition_origin = None

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
        player_render = self._render_position(snapshot)
        target_render = self._target_render_position(snapshot)
        if (
            player_render is None
            or target_render is None
            or not game_actions.can_project(
                player_render,
                target_render,
                sprite=True,
            )
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

        player_render = self._render_position(fresh)
        target_render = self._target_render_position(fresh)
        if player_render is None or target_render is None:
            return

        self.attack_clicked_at = time.time()
        result = game_actions.attack(
            player_render,
            target_render,
            retry_index=self.attack_retry,
            target_key=self.target_name,
            actor_id=int(self.target_id) if self.target_id is not None else None,
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

        self._attack_backend = str(result.get("backend") or "mouse")
        self._attack_origin = player
        self._combat_click_locked = False
        self._combat_seen = False
        self._set_state(
            "ATTACKING",
            f"Attack click sent to {self.target_name}; verifying actor click",
        )

    def _step_attacking(self, snapshot: dict[str, Any]):
        actor = self._refresh_locked_target(snapshot)
        if actor is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return

        if self.target_id is None:
            self._set_state("SEARCHING", "Lost target lock")
            return

        # Confirm the mouse click by observing Classic.exe itself send 0437 for
        # this exact actor. Once that happens, absolutely no more attack clicks.
        if self._client_attack_registered(
            snapshot,
            int(self.target_id),
            self.attack_clicked_at,
        ):
            self._combat_click_locked = True
            game_actions.note_attack_registered(
                self.attack_retry,
                target_key=self.target_name,
            )
            self._log(
                "client_attack_registered",
                target_id=self.target_id,
                target_name=self.target_name,
            )
            self._set_state(
                "WAITING_FOR_DEATH",
                f"{self.target_name} click registered; locked until death",
            )
            return

        elapsed = time.time() - self.attack_clicked_at
        if elapsed < self.attack_confirm_timeout:
            self._stop.wait(0.03)
            return

        # Native actor-ID sends already returned successfully from Classic.exe's
        # own authenticated map socket. Do not apply mouse-style precision
        # retries to them; duplicate packets can create repeated attack actions.
        if self._attack_backend == "native":
            self._combat_click_locked = True
            self._set_state(
                "WAITING_FOR_DEATH",
                f"Native attack sent to {self.target_name}; waiting for combat/death",
            )
            return

        # No outgoing actor-action means the mouse click did not actually land
        # on the monster. Allow exactly one immediate precision retry.
        if self.attack_retry < self.max_attack_retries:
            self.attack_retry += 1
            fresh = authenticated_client_monitor.snapshot()
            actor = self._refresh_locked_target(fresh)
            player = self._position(fresh)
            if actor is None:
                self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
                return
            if player is None or self.target_pos is None:
                self._stop.wait(0.03)
                return

            player_render = self._render_position(fresh)
            target_render = self._target_render_position(fresh)
            if player_render is None or target_render is None:
                self._stop.wait(0.03)
                return

            self.attack_clicked_at = time.time()
            result = game_actions.attack(
                player_render,
                target_render,
                retry_index=self.attack_retry,
                target_key=self.target_name,
            )
            self._log(
                "attack_precision_retry",
                target_id=self.target_id,
                target_name=self.target_name,
                retry=self.attack_retry,
                result=result,
            )
            if result.get("ok"):
                self._attack_origin = player
                return

        # The click still did not register. Do not freeze on this actor and do
        # not spam-click it: refresh/reposition once, then try again naturally.
        self.attack_retry = 0
        self._combat_click_locked = False
        self._attack_reposition_required = True
        self._attack_reposition_origin = self._position(snapshot)
        self._attack_route_anchor = self.target_pos
        self._set_state(
            "ROUTING",
            f"Click missed {self.target_name}; approaching within 3 tiles before retry",
        )


    def _step_waiting_for_death(self, snapshot: dict[str, Any]):
        if self._refresh_locked_target(snapshot) is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} defeated")
            return

        self._combat_click_locked = True

        # A successful actor-action packet is useful telemetry, but it must not
        # trigger another attack click. One click owns this target until removal.
        if (
            not self._combat_seen
            and self.target_id is not None
            and self._combat_confirmed(
                snapshot,
                int(self.target_id),
                self.attack_clicked_at,
            )
        ):
            self._combat_seen = True
            self._log(
                "combat_confirmed",
                target_id=self.target_id,
                target_name=self.target_name,
            )
            self.message = (
                f"Combat confirmed on {self.target_name}; waiting for death"
            )

        # Strict combat lock: no movement, no retargeting, no attack retries.
        self._stop.wait(0.06)

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
            self.recent_kills.append({
                "time": time.time(),
                "pos": old_pos,
                "monster": old_name,
            })
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
        player_render = self._render_position(snapshot) or (
            float(player[0]),
            float(player[1]),
        )
        result = game_actions.loot(player_render, item_pos)
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

    def _wander_corridor_is_narrow(
        self,
        grid,
        player: tuple[int, int],
        destination: tuple[int, int],
    ) -> bool:
        """Detect cliff edges, bridges and other low-clearance path segments."""
        x0, y0 = player
        x1, y1 = destination
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)

        for i in range(steps + 1):
            t = i / steps
            x = int(round(x0 + (x1 - x0) * t))
            y = int(round(y0 + (y1 - y0) * t))

            # In open ground most of the 8 surrounding cells are walkable.
            # Near a cliff/bridge/wall that count drops sharply.
            neighbors = 0
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    if ox == 0 and oy == 0:
                        continue
                    if grid.walkable(x + ox, y + oy):
                        neighbors += 1
            if neighbors <= 4:
                return True

        return False

    def _safe_wander_steering_point(
        self,
        grid,
        player: tuple[int, int],
        path_index: int,
    ) -> tuple[int, int] | None:
        """Choose a route point whose straight corridor is fully walkable.

        A* may bend around cliffs. Directional held-mouse steering must never
        point across that bend, otherwise RO continues into blocked terrain.
        """
        if not self.wander_path or path_index >= len(self.wander_path) - 1:
            return None

        end = min(
            len(self.wander_path) - 1,
            path_index + self.wander_lookahead,
        )

        # Furthest clear point first. Every candidate is an actual A* path cell
        # and the supercover line must contain only walkable cells.
        for idx in range(end, path_index, -1):
            point = self.wander_path[idx]
            if clear_walk_line(grid, player, point):
                return point

        # At a tight corner, one next path cell is safer than aiming across it.
        point = self.wander_path[min(path_index + 1, len(self.wander_path) - 1)]
        return point if grid.walkable(*point) else None

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
            # Keep the button held while chaining exploration routes. The next
            # directional update turns the existing hold instead of creating a
            # visible stop/click/start cycle.
            self.wander_path = []
            self.wander_goal = None
            self.wander_progress_index = 0
            self.wander_line_points = []
            self.wander_line_index = 0
            if not self._choose_wander_path(snapshot):
                game_actions.release_hold_move()
                self._stop.wait(0.10)
                return

        index = self._nearest_wander_index(player)
        if index >= len(self.wander_path) - 1:
            self.wander_path = []
            self.wander_goal = None
            self.wander_line_points = []
            self.wander_line_index = 0
            return

        try:
            grid, _ = nav_repository.load(str(map_name))
        except Exception:
            game_actions.release_hold_move()
            self.wander_path = []
            self.wander_goal = None
            self._stop.wait(0.08)
            return

        if not self.wander_line_points:
            self.wander_line_points = self._build_straight_wander_segments(
                grid,
                self.wander_path,
            )
            self.wander_line_index = (
                1 if len(self.wander_line_points) > 1 else 0
            )

        if self.wander_line_index >= len(self.wander_line_points):
            self.wander_path = []
            self.wander_goal = None
            self.wander_line_points = []
            self.wander_line_index = 0
            return

        destination = self.wander_line_points[self.wander_line_index]

        # Stay committed to one straight segment until its endpoint is reached.
        # Only then turn the held mouse toward the next segment. A latched
        # reconnect owns steering until it has rejoined the route.
        if (
            self.wander_reconnect_target is None
            and self._tile_distance(player, destination) <= 2
        ):
            self.wander_line_index += 1
            if self.wander_line_index >= len(self.wander_line_points):
                self.wander_path = []
                self.wander_goal = None
                self.wander_line_points = []
                self.wander_line_index = 0
                return
            destination = self.wander_line_points[self.wander_line_index]

        # Stay committed to the current straight run. Small cell-rounding
        # deviations are normal while RO is walking and must not trigger
        # left/right route corrections. Only reconnect when we are more than
        # two cells away from the forward A* corridor.
        route_index = self._nearest_wander_index(player)
        corridor_start = max(0, route_index - 2)
        corridor_end = min(len(self.wander_path), route_index + 20)
        corridor_distance = min(
            (
                self._tile_distance(player, p)
                for p in self.wander_path[corridor_start:corridor_end]
            ),
            default=999,
        )

        reconnecting = self.wander_reconnect_target is not None

        if reconnecting:
            reconnect = self.wander_reconnect_target
            reconnect_index = self.wander_reconnect_route_index
            if (
                corridor_distance <= 2
                or self._tile_distance(player, reconnect) <= 2
            ):
                self._log(
                    "wander_route_reconnect_complete",
                    player={"x": player[0], "y": player[1]},
                    destination={"x": reconnect[0], "y": reconnect[1]},
                    corridor_distance=corridor_distance,
                )

                # Rebuild the straight-segment view from the position where we
                # actually rejoined. Keeping the old line list can point behind
                # the player and create a large artificial U-turn.
                suffix_index = max(
                    route_index + 1,
                    int(reconnect_index or route_index) + 1,
                )
                remaining = [player] + self.wander_path[
                    min(suffix_index, len(self.wander_path) - 1):
                ]
                rebuilt = self._build_straight_wander_segments(
                    grid,
                    remaining,
                )
                if len(rebuilt) >= 2:
                    self.wander_line_points = rebuilt
                    self.wander_line_index = 1
                    destination = rebuilt[1]

                self.wander_reconnect_target = None
                self.wander_reconnect_route_index = None
                reconnecting = False

            elif clear_walk_line(grid, player, reconnect):
                destination = reconnect

            else:
                # Never allow reconnect selection to jump backward. If the
                # straight sight-line to the latched point changes while RO is
                # moving, advance the latch to another clear path point ahead.
                previous_index = int(reconnect_index or route_index)
                min_index = max(route_index + 1, previous_index + 1)
                end = min(len(self.wander_path), max(min_index + 1, route_index + 16))

                replacement_index = None
                replacement = None
                for idx in range(end - 1, min_index - 1, -1):
                    candidate = self.wander_path[idx]
                    if clear_walk_line(grid, player, candidate):
                        replacement_index = idx
                        replacement = candidate
                        break

                if replacement is not None:
                    self.wander_reconnect_target = replacement
                    self.wander_reconnect_route_index = replacement_index
                    destination = replacement
                    reconnecting = True
                    self._log(
                        "wander_route_reconnect_advanced",
                        player={"x": player[0], "y": player[1]},
                        destination={"x": replacement[0], "y": replacement[1]},
                        previous_index=previous_index,
                        reconnect_index=replacement_index,
                        corridor_distance=corridor_distance,
                    )
                else:
                    # No safe farther point is visible yet. Use the immediate
                    # forward path cell without throwing away reconnect state.
                    immediate_index = min(
                        max(route_index + 1, previous_index),
                        len(self.wander_path) - 1,
                    )
                    immediate = self.wander_path[immediate_index]
                    if clear_walk_line(grid, player, immediate):
                        destination = immediate
                    else:
                        game_actions.release_hold_move()
                        self._stop.wait(0.06)
                        return

        if not reconnecting and corridor_distance > 2:
            reconnect_index = None
            reconnect = None
            end = min(len(self.wander_path), route_index + 15)

            # Prefer a point well ahead of current progress. This avoids
            # waypoint-boundary U-turns caused by snapping to the nearest path
            # cell behind the character.
            for idx in range(end - 1, route_index, -1):
                candidate = self.wander_path[idx]
                if clear_walk_line(grid, player, candidate):
                    reconnect_index = idx
                    reconnect = candidate
                    break

            if reconnect is None and route_index + 1 < len(self.wander_path):
                candidate = self.wander_path[route_index + 1]
                if grid.walkable(*candidate):
                    reconnect_index = route_index + 1
                    reconnect = candidate

            if reconnect is not None:
                self.wander_reconnect_target = reconnect
                self.wander_reconnect_route_index = reconnect_index
                destination = reconnect
                reconnecting = True
                self._log(
                    "wander_route_reconnect",
                    player={"x": player[0], "y": player[1]},
                    destination={"x": destination[0], "y": destination[1]},
                    route_index=route_index,
                    reconnect_index=reconnect_index,
                    corridor_distance=corridor_distance,
                    latched=True,
                )
            else:
                game_actions.release_hold_move()
                self.wander_path = []
                self.wander_goal = None
                self.wander_line_points = []
                self.wander_line_index = 0
                self.wander_reconnect_target = None
                self.wander_reconnect_route_index = None
                self._stop.wait(0.12)
                return

        narrow_corridor = self._wander_corridor_is_narrow(
            grid,
            player,
            destination,
        )

        if game_actions.native_move_ready():
            now = time.time()
            resend_due = (
                self._native_wander_destination != destination
                or now - self._native_wander_sent_at >= 1.0
            )
            if resend_due:
                result = game_actions.move_to(destination)
                if not result.get("ok"):
                    self._native_wander_destination = None
                    self._native_wander_sent_at = 0.0
                    self._stop.wait(0.08)
                    return
                self._native_wander_destination = destination
                self._native_wander_sent_at = now
                self._log(
                    "native_wander_move",
                    destination={"x": destination[0], "y": destination[1]},
                    reconnecting=reconnecting,
                    narrow_corridor=narrow_corridor,
                    result=result,
                )
        else:
            if reconnecting:
                dx = destination[0] - player[0]
                dy = destination[1] - player[1]
            else:
                dx, dy = self._anticipated_wander_direction(
                    grid,
                    player,
                    destination,
                )

            steering_radius = 115 if narrow_corridor else 185
            turn_threshold = 10 if narrow_corridor else 22

            result = game_actions.update_hold_direction(
                dx,
                dy,
                radius_px=steering_radius,
                min_pixel_change=turn_threshold,
            )

            if not result.get("ok"):
                # Never substitute an arbitrary screen/cell click during wandering.
                game_actions.release_hold_move()
                self.wander_path = []
                self.wander_goal = None
                self._stop.wait(0.08)
                return

        route = hunt_route_store.get(str(map_name))
        if route.get("exists"):
            self.message = (
                f"Hunting route → point {self.saved_route_waypoint_index + 1} "
                f"({self.wander_goal[0]},{self.wander_goal[1]})"
                f" · straight segment {self.wander_line_index}/"
                f"{max(1, len(self.wander_line_points) - 1)}"
                + (" · narrow corridor" if narrow_corridor else "")
            )
        else:
            self.message = (
                f"Wandering smoothly toward {self.wander_goal[0]},{self.wander_goal[1]}"
                + (" · narrow corridor" if narrow_corridor else "")
            )
        self._stop.wait(0.04 if narrow_corridor else 0.05)

    def _maybe_heal(self, snapshot: dict[str, Any]) -> bool:
        profile = app_state.get_profile()
        settings = profile.healing
        if not settings.enabled:
            return False

        world = self._world(snapshot)
        hp = world.get("hp")
        hp_max = world.get("hp_max")
        if hp is None or not hp_max or int(hp) <= 0:
            return False

        hp_percent = float(world.get("hp_percent") or (int(hp) * 100 / int(hp_max)))
        threshold = max(1, min(99, int(settings.hp_below_percent)))
        if hp_percent >= threshold:
            return False

        now = time.time()
        cooldown = max(0.35, float(settings.cooldown_seconds))
        if now - self._last_heal_hotkey_at < cooldown:
            return False

        result = game_actions.press_hotkey(settings.hotkey or "1")
        self._last_heal_hotkey_at = now
        self._last_heal_result = dict(result)
        self._log(
            "healing_hotkey",
            hp=int(hp),
            hp_max=int(hp_max),
            hp_percent=round(hp_percent, 1),
            threshold=threshold,
            hotkey=settings.hotkey or "1",
            result=result,
        )
        return bool(result.get("ok"))

    def _update_return_weight(self, snapshot: dict[str, Any]):
        profile = app_state.get_profile()
        world = self._world(snapshot)
        weight_percent = world.get("weight_percent")
        threshold = max(1, min(99, int(profile.town.return_weight_percent)))
        reached = bool(
            weight_percent is not None
            and float(weight_percent) >= float(threshold)
        )
        if reached and not self._return_weight_reached:
            self._log(
                "return_weight_reached",
                weight_percent=float(weight_percent),
                threshold=threshold,
                hunt_map=profile.hunt.map,
                return_method=profile.town.return_method,
            )
        self._return_weight_reached = reached

    def _step_failed(self):
        snapshot = authenticated_client_monitor.snapshot()
        actor = self._refresh_locked_target(snapshot)
        player = self._position(snapshot)

        if actor is not None and player is not None and self.target_pos is not None:
            if game_actions.can_project(
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

            # Emergency behavior and monster-specific teleport rules have
            # priority over normal targeting and route movement.
            if self._maybe_emergency_action(snapshot):
                continue
            if self._maybe_teleport_for_monster(snapshot):
                continue

            # Healing is an interrupt-level concern and runs independently of
            # combat/path state. The configured hotkey is only pressed when the
            # authenticated client's live HP falls below the profile threshold.
            self._maybe_heal(snapshot)
            self._update_return_weight(snapshot)

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
            if not game_actions.calibration_valid():
                raise RuntimeError(
                    "A valid screen calibration is required for the current Classic.exe window size."
                )

            self._run_id += 1
            run_id = self._run_id
            self._stop.clear()
            self._clear_target()
            self.saved_route_map = None
            self.saved_route_waypoint_index = 0
            self.saved_route_direction = 1
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
        game_actions.release_hold_move()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self.running = False
        self._clear_target()
        self.state = "IDLE"
        self.message = "Hunting AI stopped"
        return self.snapshot()

    def diagnostic_navigation(self) -> dict[str, Any]:
        with self._lock:
            return {
                "wander_goal": (
                    {"x": self.wander_goal[0], "y": self.wander_goal[1]}
                    if self.wander_goal else None
                ),
                "wander_progress_index": self.wander_progress_index,
                "straight_segment_index": self.wander_line_index,
                "astar_path": [
                    {"x": x, "y": y}
                    for x, y in self.wander_path
                ],
                "straight_segments": [
                    {"x": x, "y": y}
                    for x, y in self.wander_line_points
                ],
            }

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
                    "sprite_y_offset": game_actions.sprite_y_offset,
                    "direct_attack_click_range": self.direct_attack_click_range,
                    "attack_walk_timeout": self.attack_walk_timeout,
                    "loot_radius": self.loot_radius,
                    "wander_lookahead": self.wander_lookahead,
                    "wander_cursor_radius": self.wander_cursor_radius,
                    "wander_turn_pixel_threshold": self.wander_turn_pixel_threshold,
                    "combat_click_mode": "fresh-frame_0437-confirmed_lock_until_death",
                    "attack_precision": game_actions.precision_snapshot(),
                    "wander_corridor_mode": "astar_clear_line_only",
                    "wander_progress_mode": "forward_only_smoothed_segments",
                    "steering_mode": (
                        "native_destination_segments"
                        if game_actions.native_move_ready()
                        else "heading_dead_zone_velocity_curve_corner_preview"
                    ),
                    "healing": {
                        "enabled": app_state.get_profile().healing.enabled,
                        "hotkey": app_state.get_profile().healing.hotkey,
                        "hp_below_percent": app_state.get_profile().healing.hp_below_percent,
                        "last_result": self._last_heal_result,
                    },
                    "town_return": {
                        "weight_percent": (
                            self._world(authenticated_client_monitor.snapshot()).get("weight_percent")
                        ),
                        "threshold_percent": app_state.get_profile().town.return_weight_percent,
                        "return_required": self._return_weight_reached,
                        "return_method": app_state.get_profile().town.return_method,
                        "auto_nearest_services": app_state.get_profile().town.auto_nearest_services,
                    },
                    "saved_hunt_route": {
                        "map": self.saved_route_map,
                        "waypoint_index": self.saved_route_waypoint_index,
                        "waypoint_number": self.saved_route_waypoint_index + 1,
                        "direction": self.saved_route_direction,
                        "mode": self.saved_route_mode,
                        "active": bool(
                            hunt_route_store.get(
                                str(self._world(authenticated_client_monitor.snapshot()).get("map") or "")
                            ).get("exists")
                        ),
                    },
                    "exploration": exploration_planner.snapshot(),
                },
                "calibration": game_actions.calibration_snapshot(),
                "actions": self.actions[-30:],
                "architecture": (
                    "OpenKore-style state machine in map coordinates; "
                    "mouse adapter only executes MOVE and ATTACK actions."
                ),
            }


hunting_ai = HuntingAI()
