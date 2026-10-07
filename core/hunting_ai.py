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
        self.loot_ignored: set[int] = set()
        self.max_loot_retries = 3
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
        self._last_combat_move_destination: tuple[int, int] | None = None
        self._last_combat_move_sent_at = 0.0
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
        self._combat_committed_target_id: int | None = None
        self._attack_origin = None
        self._attack_backend = "mouse"
        self._opening_skill_used_target_id = None
        self._last_heal_hotkey_at = 0.0
        self._last_heal_result: dict[str, Any] | None = None
        self._next_heal_threshold: int | None = None
        self._last_aspd_use_at = 0.0
        self._last_aspd_result: dict[str, Any] | None = None
        self._opening_skill_used_target_id: int | None = None
        self._skill_last_used_at: dict[int, float] = {}
        self._last_skill_result: dict[str, Any] | None = None
        self._return_weight_reached = False
        self._last_teleport_at = 0.0
        self._last_teleport_reason: str | None = None
        self._trusted_position: tuple[int, int] | None = None
        self._trusted_position_map: str | None = None
        self._trusted_position_at = 0.0
        self._position_candidate: tuple[int, int] | None = None
        self._position_candidate_since = 0.0
        self._position_reject_count = 0
        self._position_last_rejected: dict[str, Any] | None = None
        self._wander_last_position: tuple[int, int] | None = None
        self._wander_last_progress_at = time.time()
        self._target_failure_count: dict[int, int] = {}
        self._target_cooldown_until: dict[int, float] = {}
        self._target_locked_at: dict[int, float] = {}
        self._monster_memory: dict[int, dict[str, Any]] = {}
        self._reaction_ready_at: dict[int, float] = {}
        self._attack_commit_range = self.attack_range
        self._next_roam_pause_at = time.time() + 12.0
        self._roam_pause_until = 0.0
        self._last_roam_reconsider_at = 0.0
        self._wander_visibility_stop_distance = 10
        self._loot_not_before = 0.0
        self._next_loot_at = 0.0
        self._loot_first_attempt_at: dict[int, float] = {}
        self._liveness_last_position: tuple[int, int] | None = None
        self._liveness_last_progress_at = time.time()
        self._liveness_recoveries = 0
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

    def _position(self, snapshot: dict[str, Any]) -> tuple[int, int] | None:
        """Return a map-sane position and quarantine transient bad coordinates."""
        world = self._world(snapshot)
        x, y = world.get("x"), world.get("y")
        map_name = str(world.get("map") or "").strip()
        if x is None or y is None:
            return self._trusted_position

        raw = (int(x), int(y))
        now = time.time()

        try:
            grid, _ = nav_repository.load(map_name) if map_name else (None, None)
        except Exception:
            grid = None

        in_bounds = bool(
            grid is None
            or (
                0 <= raw[0] < int(grid.width)
                and 0 <= raw[1] < int(grid.height)
            )
        )
        if not in_bounds:
            self._position_reject_count += 1
            self._position_last_rejected = {
                "time": now,
                "map": map_name or None,
                "raw": {"x": raw[0], "y": raw[1]},
                "reason": "outside_map_bounds",
                "trusted": (
                    {"x": self._trusted_position[0], "y": self._trusted_position[1]}
                    if self._trusted_position else None
                ),
            }
            self._position_candidate = None
            self._position_candidate_since = 0.0
            return self._trusted_position

        map_changed = bool(
            map_name
            and self._trusted_position_map
            and map_name != self._trusted_position_map
        )
        if self._trusted_position is None or map_changed:
            self._trusted_position = raw
            self._trusted_position_map = map_name or self._trusted_position_map
            self._trusted_position_at = now
            self._position_candidate = None
            self._position_candidate_since = 0.0
            return raw

        jump = self._tile_distance(self._trusted_position, raw)
        teleport_recent = now - float(self._last_teleport_at or 0.0) <= 1.25

        # Ordinary movement updates are small. A very large same-map jump can
        # be legitimate after Fly Wing/teleport, otherwise quarantine it for a
        # short period so one malformed movement packet cannot poison A*.
        if jump > 40 and not teleport_recent:
            if (
                self._position_candidate is None
                or self._tile_distance(self._position_candidate, raw) > 2
            ):
                self._position_candidate = raw
                self._position_candidate_since = now

            held_for = now - self._position_candidate_since
            if held_for < 0.75:
                self._position_reject_count += 1
                self._position_last_rejected = {
                    "time": now,
                    "map": map_name or None,
                    "raw": {"x": raw[0], "y": raw[1]},
                    "reason": "implausible_same_map_jump",
                    "jump_tiles": jump,
                    "held_seconds": round(held_for, 3),
                    "trusted": {
                        "x": self._trusted_position[0],
                        "y": self._trusted_position[1],
                    },
                }
                return self._trusted_position

        self._trusted_position = raw
        self._trusted_position_map = map_name or self._trusted_position_map
        self._trusted_position_at = now
        self._position_candidate = None
        self._position_candidate_since = 0.0
        return raw

    def _position_guard_snapshot(self) -> dict[str, Any]:
        return {
            "trusted": (
                {"x": self._trusted_position[0], "y": self._trusted_position[1]}
                if self._trusted_position else None
            ),
            "map": self._trusted_position_map,
            "accepted_at": round(self._trusted_position_at, 3)
            if self._trusted_position_at else None,
            "rejected_count": self._position_reject_count,
            "last_rejected": self._position_last_rejected,
            "candidate": (
                {"x": self._position_candidate[0], "y": self._position_candidate[1]}
                if self._position_candidate else None
            ),
        }

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
        self._combat_committed_target_id = None
        self._attack_reposition_required = False
        self._attack_reposition_origin = None
        self._attack_route_anchor = None
        self._attack_backend = "mouse"
        self._opening_skill_used_target_id = None

    def _lock_actor(self, actor: dict[str, Any], reason: str) -> bool:
        x, y = actor.get("x"), actor.get("y")
        if x is None or y is None:
            return False
        self.target_id = int(actor["id"])
        self.target_name = str(actor.get("name") or f"Monster #{self.target_id}")
        self.target_pos = (int(x), int(y))
        self.route_target_pos = None
        self.attack_retry = 0
        self._target_failure_count.setdefault(int(self.target_id), 0)
        self._target_locked_at[int(self.target_id)] = time.time()
        self._last_combat_move_destination = None
        self._last_combat_move_sent_at = 0.0
        self._attack_commit_range = random.randint(
            max(1, int(self.attack_range) - 1),
            max(1, int(self.attack_range)),
        )
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
        normalized = str(name or "").strip().lower()
        rule = self._monster_rule(name)
        if rule is None:
            # Never attack random monsters simply because they became aggressive.
            # A monster must be part of the saved Hunt profile. Keep legacy
            # profiles working by treating names in hunt.monsters as "attack".
            selected = {
                str(monster or "").strip().lower()
                for monster in app_state.get_profile().hunt.monsters
                if str(monster or "").strip()
            }
            return "attack" if normalized and normalized in selected else "ignore"
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
        native_state = native_action_bridge.snapshot()
        if not native_state.get("attached"):
            try:
                native_action_bridge.start()
            except Exception as exc:
                self._log(
                    "teleport_failed",
                    reason=reason,
                    error="native_bridge_start_failed",
                    message=str(exc),
                )
                return False
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

    def _target_on_cooldown(self, actor_id: int | None) -> bool:
        if actor_id is None:
            return False
        until = float(self._target_cooldown_until.get(int(actor_id), 0.0))
        if until <= time.time():
            self._target_cooldown_until.pop(int(actor_id), None)
            return False
        return True

    def _cooldown_target(self, actor_id: int | None, reason: str) -> None:
        if actor_id is None:
            return
        seconds = max(
            1.0,
            float(app_state.get_profile().hunt.unreachable_target_cooldown_seconds),
        )
        actor_id = int(actor_id)
        self._target_cooldown_until[actor_id] = time.time() + seconds
        self._target_locked_at.pop(actor_id, None)
        self._log(
            "target_cooldown",
            target_id=int(actor_id),
            seconds=seconds,
            reason=reason,
        )

    def _observe_monster_memory(self, snapshot: dict[str, Any]) -> None:
        """Remember recently visible monsters and assign stable reaction delays."""
        now = time.time()
        profile = app_state.get_profile().hunt
        live = snapshot.get("live_state") or {}
        aggressor_ids = {int(v) for v in (live.get("aggressor_ids") or [])}
        visible_ids: set[int] = set()

        for actor in live.get("actors") or []:
            if actor.get("kind") != "monster" or actor.get("id") is None:
                continue
            actor_id = int(actor["id"])
            visible_ids.add(actor_id)
            x, y = actor.get("x"), actor.get("y")
            entry = self._monster_memory.get(actor_id)
            if entry is None:
                ttl = random.uniform(
                    max(0.2, float(profile.monster_memory_min_seconds)),
                    max(float(profile.monster_memory_min_seconds), float(profile.monster_memory_max_seconds)),
                )
                entry = {
                    "first_seen": now,
                    "ttl": ttl,
                }
                self._monster_memory[actor_id] = entry
                if actor_id in aggressor_ids:
                    low = max(0.0, float(profile.aggressor_reaction_delay_min))
                    high = max(low, float(profile.aggressor_reaction_delay_max))
                else:
                    low = max(0.0, float(profile.normal_reaction_delay_min))
                    high = max(low, float(profile.normal_reaction_delay_max))
                self._reaction_ready_at[actor_id] = now + random.uniform(low, high)
            elif actor_id in aggressor_ids:
                low = max(0.0, float(profile.aggressor_reaction_delay_min))
                high = max(low, float(profile.aggressor_reaction_delay_max))
                self._reaction_ready_at[actor_id] = min(
                    float(self._reaction_ready_at.get(actor_id, now)),
                    now + random.uniform(low, high),
                )

            entry.update({
                "last_seen": now,
                "x": int(x) if x is not None else entry.get("x"),
                "y": int(y) if y is not None else entry.get("y"),
                "name": str(actor.get("name") or entry.get("name") or ""),
                "priority": self._monster_priority(actor.get("name")),
                "aggressor": actor_id in aggressor_ids,
            })

        expired = []
        for actor_id, entry in self._monster_memory.items():
            last_seen = float(entry.get("last_seen") or entry.get("first_seen") or now)
            if actor_id not in visible_ids and now - last_seen > float(entry.get("ttl") or 1.0):
                expired.append(actor_id)
        for actor_id in expired:
            self._monster_memory.pop(actor_id, None)
            self._reaction_ready_at.pop(actor_id, None)

    def _reaction_ready(self, actor: dict[str, Any]) -> bool:
        actor_id = int(actor.get("id") or -1)
        return time.time() >= float(self._reaction_ready_at.get(actor_id, 0.0))

    def _remembered_target(self) -> dict[str, Any] | None:
        if self.target_id is None:
            return None
        entry = self._monster_memory.get(int(self.target_id))
        if not entry or entry.get("x") is None or entry.get("y") is None:
            return None
        now = time.time()
        last_seen = float(entry.get("last_seen") or 0.0)
        if now - last_seen > float(entry.get("ttl") or 1.0):
            return None
        return {
            "id": int(self.target_id),
            "name": entry.get("name") or self.target_name,
            "x": int(entry["x"]),
            "y": int(entry["y"]),
            "_remembered": True,
            "_last_seen": last_seen,
        }

    def _schedule_next_roam_pause(self) -> None:
        hunt = app_state.get_profile().hunt
        low = max(2.0, float(hunt.roam_pause_interval_min))
        high = max(low, float(hunt.roam_pause_interval_max))
        self._next_roam_pause_at = time.time() + random.uniform(low, high)

    def _adaptive_move_segment_tiles(
        self,
        grid,
        player: tuple[int, int],
    ) -> int:
        openness = exploration_planner._local_openness(grid, player, radius=5)
        wall = exploration_planner._wall_proximity(grid, player, max_radius=5)
        if openness >= 0.78 and wall <= 0.25:
            return min(8, max(self.move_segment_tiles, 7))
        if openness <= 0.48 or wall >= 0.70:
            return min(4, max(2, self.move_segment_tiles - 2))
        return min(6, max(3, self.move_segment_tiles))

    def _monster_priority(self, name: str | None) -> int:
        rule = self._monster_rule(name)
        return max(1, min(999, int(rule.priority))) if rule is not None else 50

    def _eligible_target_candidates(
        self,
        snapshot: dict[str, Any],
        *,
        exclude_id: int | None = None,
    ) -> list[tuple[int, int, dict[str, Any]]]:
        profile = app_state.get_profile()
        targeting = build_targeting_state(snapshot, profile.hunt.monsters)
        live = snapshot.get("live_state") or {}
        aggressor_ids = {int(v) for v in (live.get("aggressor_ids") or [])}
        candidates: list[tuple[int, int, dict[str, Any]]] = []

        for actor in targeting.get("candidates") or []:
            actor_id = int(actor.get("id") or -1)
            if exclude_id is not None and actor_id == int(exclude_id):
                continue
            if self._target_on_cooldown(actor_id):
                continue

            rule = self._monster_rule(actor.get("name"))
            behavior = self._monster_behavior(actor.get("name"))
            if behavior == "ignore":
                continue
            if behavior == "aggressor_only" and actor_id not in aggressor_ids:
                continue
            if behavior not in {"attack", "aggressor_only"}:
                continue

            actor_x, actor_y = actor.get("x"), actor.get("y")
            if (
                actor_x is not None
                and actor_y is not None
                and self._in_avoid_zone(
                    self._world(snapshot).get("map"),
                    int(actor_x),
                    int(actor_y),
                )
            ):
                continue

            tile_distance = actor.get("tile_distance")
            if rule is not None and tile_distance is not None:
                if int(rule.min_distance or 0) > 0 and int(tile_distance) < int(rule.min_distance):
                    continue
                if int(rule.max_distance or 0) > 0 and int(tile_distance) > int(rule.max_distance):
                    continue

            priority = self._monster_priority(actor.get("name"))
            candidates.append((priority, int(tile_distance or 0), actor))

        # Priority is authoritative. Distance is only a tie-breaker between
        # monsters with the same numeric priority.
        candidates.sort(
            key=lambda row: (
                row[0],
                row[1],
                int(row[2].get("id") or 0),
            )
        )
        return candidates

    def _best_aggressor_actor(
        self,
        snapshot: dict[str, Any],
        *,
        exclude_id: int | None = None,
    ) -> dict[str, Any] | None:
        live = snapshot.get("live_state") or {}
        aggressor_ids = {int(v) for v in (live.get("aggressor_ids") or [])}
        for _priority, _distance, actor in self._eligible_target_candidates(
            snapshot,
            exclude_id=exclude_id,
        ):
            if int(actor.get("id") or -1) in aggressor_ids:
                return actor
        return None

    def _acquire_aggressor(self, snapshot: dict[str, Any]) -> bool:
        actor = self._best_aggressor_actor(snapshot)
        if actor is None or not self._reaction_ready(actor):
            return False
        return self._lock_actor(actor, "aggressor")

    def _maybe_preempt_for_higher_priority_target(
        self,
        snapshot: dict[str, Any],
    ) -> bool:
        profile = app_state.get_profile()
        if not (
            profile.hunt.preempt_for_higher_priority_aggressor
            and self.target_id is not None
        ):
            return False

        # Once an attack has actually been committed, finish that monster.
        # Before that point, monster priority is authoritative: any lower
        # numeric priority may replace the current approach/selection lock.
        if (
            self._combat_committed_target_id is not None
            and int(self._combat_committed_target_id) == int(self.target_id)
        ):
            return False

        candidates = self._eligible_target_candidates(
            snapshot,
            exclude_id=int(self.target_id),
        )
        if not candidates:
            return False

        candidate_priority, _distance, candidate = candidates[0]
        current_priority = self._monster_priority(self.target_name)
        if candidate_priority >= current_priority:
            return False

        old_id = self.target_id
        old_name = self.target_name
        if not self._lock_actor(candidate, "higher_priority_target_preempt"):
            return False

        self._log(
            "priority_preempt",
            previous_target_id=old_id,
            previous_target_name=old_name,
            previous_priority=current_priority,
            new_target_id=self.target_id,
            new_target_name=self.target_name,
            new_priority=candidate_priority,
            strict_priority=True,
        )
        self._set_state(
            "TARGET_SELECTED",
            f"Priority switched to {self.target_name} ({candidate_priority})",
        )
        return True

    def _acquire_target(self, snapshot: dict[str, Any]) -> bool:
        profile = app_state.get_profile()
        player = self._position(snapshot)
        candidates = [
            row for row in self._eligible_target_candidates(snapshot)
            if self._reaction_ready(row[2])
        ]

        if not candidates:
            return False

        # Route validation is evaluated in strict priority order. We only move
        # to the next priority when the higher-priority candidate is currently
        # unreachable/outside configured chase distance.
        if player is not None:
            map_name = str(self._world(snapshot).get("map") or "")
            try:
                grid, _ = nav_repository.load(map_name)
            except Exception:
                grid = None

            if grid is not None:
                max_path = max(1, int(profile.hunt.attack_route_max_path_distance))
                for priority, _distance, actor in candidates:
                    ax, ay = actor.get("x"), actor.get("y")
                    if ax is None or ay is None:
                        continue
                    target = (int(ax), int(ay))

                    if self._tile_distance(player, target) <= self.attack_range:
                        if (
                            not profile.hunt.attack_check_los
                            or clear_walk_line(grid, player, target)
                        ):
                            self._log(
                                "priority_target_selected",
                                target_id=actor.get("id"),
                                target_name=actor.get("name"),
                                priority=priority,
                            )
                            return self._lock_actor(actor, "priority_target_route_checked")
                        continue

                    path = astar(
                        grid,
                        player,
                        target,
                        max_expansions=60000,
                        clearance_weight=0.65,
                    )
                    if not path:
                        self._cooldown_target(
                            int(actor.get("id") or -1),
                            "target_precheck_no_route",
                        )
                        continue
                    if len(path) - 1 > max_path:
                        self._log(
                            "target_deferred_too_far",
                            target_id=actor.get("id"),
                            target_name=actor.get("name"),
                            priority=priority,
                            path_steps=len(path) - 1,
                            max_path_steps=max_path,
                        )
                        continue
                    if self._path_crosses_avoid_zone(map_name, path):
                        self._cooldown_target(
                            int(actor.get("id") or -1),
                            "target_precheck_avoid_zone",
                        )
                        continue

                    self._log(
                        "priority_target_selected",
                        target_id=actor.get("id"),
                        target_name=actor.get("name"),
                        priority=priority,
                    )
                    return self._lock_actor(actor, "priority_target_route_checked")

                return False

        priority, _distance, actor = candidates[0]
        self._log(
            "priority_target_selected",
            target_id=actor.get("id"),
            target_name=actor.get("name"),
            priority=priority,
        )
        return self._lock_actor(actor, "priority_target")

    @staticmethod
    def _tile_distance(a: tuple[int, int], b: tuple[int, int]) -> int:
        return max(abs(a[0] - b[0]), abs(a[1] - b[1]))

    def _in_avoid_zone(self, map_name: str | None, x: int, y: int) -> bool:
        current = str(map_name or "").strip().lower()
        for zone in app_state.get_profile().hunt.avoid_zones:
            zone_map = str(zone.map or current).strip().lower()
            if zone_map and zone_map != current:
                continue
            min_x, max_x = sorted((int(zone.x1), int(zone.x2)))
            min_y, max_y = sorted((int(zone.y1), int(zone.y2)))
            if min_x <= int(x) <= max_x and min_y <= int(y) <= max_y:
                return True
        return False

    def _path_crosses_avoid_zone(
        self,
        map_name: str | None,
        path: list[dict[str, Any]] | list[tuple[int, int]],
    ) -> bool:
        for point in path:
            if isinstance(point, dict):
                x, y = point.get("x"), point.get("y")
            else:
                x, y = point
            if x is None or y is None:
                continue
            if self._in_avoid_zone(map_name, int(x), int(y)):
                return True
        return False


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
        deadline = time.time() + max(
            self.move_settle_timeout,
            float(app_state.get_profile().hunt.move_giveup_seconds),
        )
        last_pos = origin
        last_change = time.time()
        moved = False

        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(0.06)
            snapshot = authenticated_client_monitor.snapshot()

            if self._find_actor(snapshot, target_id) is None:
                remembered = self._monster_memory.get(int(target_id))
                if remembered is None:
                    return None
                last_seen = float(remembered.get("last_seen") or 0.0)
                if time.time() - last_seen > float(remembered.get("ttl") or 1.0):
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
        *,
        max_tiles: int | None = None,
    ) -> tuple[int, int] | None:
        if len(path_preview) < 2:
            return None

        # Never deliberately walk onto the monster tile. Keep the selected
        # per-target commit range available for the final attack approach.
        max_index = max(
            1,
            len(path_preview) - 1 - max(1, int(self._attack_commit_range)),
        )
        segment_tiles = (
            max(1, int(max_tiles))
            if max_tiles is not None
            else self.move_segment_tiles
        )
        index = min(segment_tiles, max_index)
        point = path_preview[index]
        return int(point["x"]), int(point["y"])

    def _monster_looting_enabled(self, monster_name: str | None) -> bool:
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
        return str(rule.loot_mode or "all").strip().lower() != "none"

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

        # Forget ignored IDs as soon as the client no longer reports them.
        # This prevents one stale/unlootable floor item from blocking the whole
        # hunting loop forever while still allowing future drops to be looted.
        live_item_ids = {
            int(row.get("id"))
            for row in items
            if row.get("id") is not None
        }
        self.loot_ignored.intersection_update(live_item_ids)

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
            try:
                item_id = int(item.get("id"))
            except Exception:
                item_id = -1
            if item_id in self.loot_ignored:
                continue

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

    def _fallback_exploration_route(
        self,
        map_name: str,
        grid,
        player: tuple[int, int],
    ) -> list[tuple[int, int]] | None:
        """Find any useful reachable forward route when frontier planning has no result."""
        candidates: list[list[tuple[int, int]]] = []
        recent = exploration_planner.snapshot().get("recent_goals") or []

        for _ in range(28):
            angle = random.random() * math.tau
            distance = random.randint(14, 38)
            gx = int(round(player[0] + math.cos(angle) * distance))
            gy = int(round(player[1] + math.sin(angle) * distance))

            goal = None
            for radius in range(0, 7):
                ring = []
                for ox in range(-radius, radius + 1):
                    for oy in range(-radius, radius + 1):
                        if radius and max(abs(ox), abs(oy)) != radius:
                            continue
                        x, y = gx + ox, gy + oy
                        if not grid.walkable(x, y):
                            continue
                        if self._in_avoid_zone(map_name, x, y):
                            continue
                        ring.append((x, y))
                if ring:
                    goal = min(
                        ring,
                        key=lambda p: math.hypot(p[0] - gx, p[1] - gy),
                    )
                    break

            if goal is None:
                continue

            if any(
                max(abs(goal[0] - int(r.get("x") or 0)), abs(goal[1] - int(r.get("y") or 0))) < 8
                for r in recent
            ):
                continue

            path = astar(
                grid,
                player,
                goal,
                max_expansions=100000,
                clearance_weight=0.85,
            )
            if not path or len(path) < 6:
                continue
            if self._path_crosses_avoid_zone(map_name, path):
                continue
            candidates.append(path)

        if not candidates:
            # Random sampling can be unlucky on corridor-heavy dungeon maps.
            # Fall back to a deterministic radial scan so an otherwise healthy
            # hunter never spends seconds bouncing SEARCHING <-> WANDERING.
            radial_goals: list[tuple[int, int]] = []
            for distance in (12, 18, 26, 34):
                for step in range(16):
                    angle = (math.tau * step) / 16.0
                    gx = int(round(player[0] + math.cos(angle) * distance))
                    gy = int(round(player[1] + math.sin(angle) * distance))

                    goal = None
                    for radius in range(0, 9):
                        best = None
                        best_error = 999999.0
                        for ox in range(-radius, radius + 1):
                            for oy in range(-radius, radius + 1):
                                if radius and max(abs(ox), abs(oy)) != radius:
                                    continue
                                x, y = gx + ox, gy + oy
                                if not grid.walkable(x, y):
                                    continue
                                if self._in_avoid_zone(map_name, x, y):
                                    continue
                                error = math.hypot(x - gx, y - gy)
                                if error < best_error:
                                    best = (x, y)
                                    best_error = error
                        if best is not None:
                            goal = best
                            break

                    if goal is not None and goal not in radial_goals:
                        radial_goals.append(goal)

            best_path = None
            best_score = None
            for goal in radial_goals:
                path = astar(
                    grid,
                    player,
                    goal,
                    max_expansions=120000,
                    clearance_weight=0.85,
                )
                if not path or len(path) < 4:
                    continue
                if self._path_crosses_avoid_zone(map_name, path):
                    continue

                recently_used = any(
                    max(
                        abs(goal[0] - int(r.get("x") or 0)),
                        abs(goal[1] - int(r.get("y") or 0)),
                    ) < 8
                    for r in recent
                )
                score = (
                    0 if recently_used else 1,
                    self._tile_distance(player, goal),
                    len(path),
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best_path = path

            if best_path is None:
                return None

            self._log(
                "wander_deterministic_fallback",
                map=map_name,
                goal={"x": best_path[-1][0], "y": best_path[-1][1]},
                steps=len(best_path) - 1,
            )
            return best_path

        candidates.sort(
            key=lambda path: (
                len(path),
                self._tile_distance(player, path[-1]),
            ),
            reverse=True,
        )
        return candidates[0]

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
            if path and len(path) >= 2 and not self._path_crosses_avoid_zone(map_name, path):
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

            if navigation_mode == "saved_only":
                self._log(
                    "saved_route_required",
                    map=map_name,
                    message="Saved hunting route is temporarily unusable.",
                )
                return False

            # In the normal mode a bad/blocked saved leg must never leave the
            # character standing still. Fall through to autonomous exploration
            # and keep searching for configured monsters.
            self._log(
                "saved_route_fallback_to_exploration",
                map=map_name,
                goal={"x": goal[0], "y": goal[1]},
            )

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
            frontier_bias=app_state.get_profile().hunt.exploration_frontier_bias,
        )

        if path and self._path_crosses_avoid_zone(map_name, path):
            self._log(
                "wander_route_blocked_by_avoid_zone",
                map=map_name,
                goal={"x": path[-1][0], "y": path[-1][1]},
            )
            path = None

        if not path:
            path = self._fallback_exploration_route(
                map_name,
                grid,
                player,
            )
            if path:
                self._log(
                    "wander_fallback_route",
                    map=map_name,
                    goal={"x": path[-1][0], "y": path[-1][1]},
                    steps=len(path) - 1,
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

            # Open ground supports long, confident runs. Near walls/corners
            # keep segments shorter so turns happen deliberately instead of
            # overshooting into obstacles.
            openness = exploration_planner._local_openness(
                grid, path[anchor_index], radius=5
            )
            wall = exploration_planner._wall_proximity(
                grid, path[anchor_index], max_radius=5
            )
            if openness >= 0.78 and wall <= 0.25:
                span = 18
            elif openness <= 0.48 or wall >= 0.70:
                span = 7
            else:
                span = 12
            max_idx = min(len(path) - 1, anchor_index + span)
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
        base = max(
            7,
            int(app_state.get_profile().hunt.exploration_reconsider_distance),
        )
        self._wander_visibility_stop_distance = random.randint(
            max(6, base - 2),
            base + 2,
        )

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

        self._set_state("WANDERING", "No target visible; exploring")
        # Do not spend a separate AI tick standing still between SEARCHING and
        # WANDERING. Pick a route and send the first move immediately.
        self._step_wandering(snapshot)

    def _attack_locked_immediately(
        self,
        snapshot: dict[str, Any],
        *,
        reason: str,
    ) -> bool:
        game_actions.release_hold_move()
        self._stop.wait(0.015)

        fresh = authenticated_client_monitor.snapshot()

        # Last-moment priority check before any attack command is sent. This
        # closes the small same-tick window where a higher-priority monster can
        # appear after the first target was selected but before the attack.
        if self._maybe_preempt_for_higher_priority_target(fresh):
            return False

        actor = self._refresh_locked_target(fresh)
        player = self._position(fresh)
        if actor is None or player is None or self.target_pos is None:
            return False

        hunt = app_state.get_profile().hunt
        distance = self._tile_distance(player, self.target_pos)
        opener_range = self._required_opening_range(fresh)
        required_range = (
            min(self._attack_commit_range, opener_range)
            if opener_range is not None
            else self._attack_commit_range
        )

        # If an opening skill such as Bash is pending, approach to the skill's
        # actual range before either the skill or the normal attack is sent.
        if hunt.attack_wait_approach_finish and distance > required_range:
            return False

        if hunt.attack_check_los:
            try:
                grid, _ = nav_repository.load(str(self._world(fresh).get("map")))
            except Exception:
                return False
            if not clear_walk_line(grid, player, self.target_pos):
                return False

        # Skill openers are evaluated only after range/route/LOS checks.
        self._try_opening_attack_skill(fresh)

        native_only = bool(hunt.native_only_actions)
        if game_actions.native_attack_ready():
            self.attack_clicked_at = time.time()
            result = game_actions.attack(
                actor_id=int(self.target_id) if self.target_id is not None else None,
                allow_mouse_fallback=False,
            )
        else:
            if native_only:
                return False
            player_render = self._render_position(fresh)
            target_render = self._target_render_position(fresh)
            if (
                player_render is None
                or target_render is None
                or not game_actions.can_project(
                    player_render,
                    target_render,
                    sprite=True,
                )
            ):
                return False
            self.attack_clicked_at = time.time()
            result = game_actions.attack(
                player_render,
                target_render,
                retry_index=0,
                target_key=self.target_name,
                actor_id=int(self.target_id) if self.target_id is not None else None,
                allow_mouse_fallback=True,
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

        self._attack_backend = str(result.get("backend") or "native")
        self._combat_committed_target_id = (
            int(self.target_id) if self.target_id is not None else None
        )
        self.attack_retry = 0
        self._attack_origin = player
        self._combat_click_locked = False
        self._combat_seen = False
        self._set_state(
            "ATTACKING",
            f"Attack sent to {self.target_name}; verifying combat",
        )
        return True

    def _step_target_selected(self, snapshot: dict[str, Any]):
        if self._maybe_preempt_for_higher_priority_target(snapshot):
            return

        actor = self._refresh_locked_target(snapshot)
        if actor is None:
            remembered = self._remembered_target()
            if remembered is None:
                lost_name = self.target_name
                self._clear_target()
                self._set_state("SEARCHING", f"Lost sight of {lost_name}; rescanning")
                return
            self.target_pos = (int(remembered["x"]), int(remembered["y"]))
            self._set_state(
                "ROUTING",
                f"Briefly lost sight of {self.target_name}; checking last seen position",
            )
            return

        player = self._position(snapshot)
        if player is not None and self.target_pos is not None:
            native_ready = game_actions.native_attack_ready()
            screen_ready = (
                not app_state.get_profile().hunt.native_only_actions
                and self._render_position(snapshot) is not None
                and self._target_render_position(snapshot) is not None
                and game_actions.can_project(
                    self._render_position(snapshot),
                    self._target_render_position(snapshot),
                    sprite=True,
                )
            )
            if native_ready or screen_ready:
                if self._attack_locked_immediately(
                    snapshot,
                    reason="visible_target_selected",
                ):
                    return

        self._set_state("ROUTING", f"Calculating route to {self.target_name}")

    def _step_routing(self, snapshot: dict[str, Any]):
        if self._maybe_preempt_for_higher_priority_target(snapshot):
            return

        actor = self._refresh_locked_target(snapshot)
        player = self._position(snapshot)
        remembered_only = False

        if actor is None:
            remembered = self._remembered_target()
            if remembered is None:
                lost_name = self.target_name
                self._clear_target()
                self._set_state("SEARCHING", f"Lost sight of {lost_name}; rescanning")
                return
            actor = remembered
            remembered_only = True
            self.target_pos = (int(remembered["x"]), int(remembered["y"]))

        if player is None or self.target_pos is None:
            self._stop.wait(0.10)
            return

        hunt = app_state.get_profile().hunt
        target_id = int(self.target_id) if self.target_id is not None else -1
        locked_at = float(self._target_locked_at.get(target_id, time.time()))
        if time.time() - locked_at >= max(0.5, float(hunt.attack_max_route_time)):
            failed_name = self.target_name
            self._cooldown_target(self.target_id, "attack_route_time_exceeded")
            self._clear_target()
            self._set_state(
                "SEARCHING",
                f"Route timeout for {failed_name}; choosing another target",
            )
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

        # A briefly lost monster can be followed to its last visible position,
        # but never attacked until the client sees it again.
        if remembered_only and distance <= 2:
            self._stop.wait(0.06)
            fresh = authenticated_client_monitor.snapshot()
            if self._refresh_locked_target(fresh) is None:
                lost_name = self.target_name
                self._clear_target()
                self._set_state("SEARCHING", f"Checked last position of {lost_name}; rescanning")
                return
            actor = self._refresh_locked_target(fresh)
            remembered_only = False
            player = self._position(fresh) or player
            distance = self._tile_distance(player, self.target_pos)

        # Fast path: only attack directly when the configured approach and LOS
        # rules say the target is already in a valid attack position.
        try:
            grid, _ = nav_repository.load(str(self._world(snapshot).get("map")))
        except Exception:
            grid = None

        if (
            not remembered_only
            and not self._attack_reposition_required
            and grid is not None
            and (
                not hunt.attack_wait_approach_finish
                or distance <= (
                    min(self._attack_commit_range, self._required_opening_range(snapshot))
                    if self._required_opening_range(snapshot) is not None
                    else self._attack_commit_range
                )
            )
            and (
                game_actions.native_attack_ready()
                or (
                    not hunt.native_only_actions
                    and self._render_position(snapshot) is not None
                    and self._target_render_position(snapshot) is not None
                    and game_actions.can_project(
                        self._render_position(snapshot),
                        self._target_render_position(snapshot),
                        sprite=True,
                    )
                )
            )
        ):
            if (not hunt.attack_check_los) or clear_walk_line(grid, player, self.target_pos):
                self._set_state(
                    "ATTACK_READY",
                    f"{self.target_name} is in a valid attack position",
                )
                return

        opener_range = self._required_opening_range(snapshot)
        required_range = (
            min(self._attack_commit_range, opener_range)
            if opener_range is not None
            else self._attack_commit_range
        )

        if not remembered_only and distance <= required_range:
            self._set_state(
                "ATTACK_READY",
                (
                    f"{self.target_name} is in opening-skill range"
                    if opener_range is not None
                    else f"{self.target_name} is in attack position"
                ),
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
        if path_preview and self._path_crosses_avoid_zone(
            self._world(snapshot).get("map"),
            path_preview,
        ):
            self._log(
                "route_blocked_by_avoid_zone",
                target_id=self.target_id,
                target_name=self.target_name,
            )
            self._clear_target()
            self._set_state("SEARCHING", "Target route crosses an avoided area")
            return

        if not pathing.get("path_found") or len(path_preview) < 2:
            if (
                not hunt.attack_wait_approach_finish
                and self.target_pos is not None
                and (
                    game_actions.native_attack_ready()
                    or (
                        not hunt.native_only_actions
                        and game_actions.can_project(
                            player,
                            self.target_pos,
                            sprite=True,
                        )
                    )
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

            target_id = int(self.target_id) if self.target_id is not None else -1
            failures = self._target_failure_count.get(target_id, 0) + 1
            self._target_failure_count[target_id] = failures
            self._log(
                "route_failed",
                target_id=self.target_id,
                target_name=self.target_name,
                failures=failures,
                message=pathing.get("message"),
            )
            if failures >= 2:
                failed_id = self.target_id
                failed_name = self.target_name
                self._cooldown_target(failed_id, "route_failed_repeatedly")
                self._clear_target()
                self._set_state(
                    "SEARCHING",
                    f"Skipping unreachable {failed_name}; continuing hunt",
                )
                return
            self._set_state(
                "FAILED",
                pathing.get("message") or "Could not route to target",
            )
            return

        max_path = max(1, int(hunt.attack_route_max_path_distance))
        path_steps = int(pathing.get("path_steps") or max(0, len(path_preview) - 1))
        if path_steps > max_path:
            failed_name = self.target_name
            self._log(
                "route_too_long",
                target_id=self.target_id,
                target_name=self.target_name,
                path_steps=path_steps,
                max_path_steps=max_path,
            )
            self._clear_target()
            self._set_state(
                "SEARCHING",
                f"{failed_name} is outside chase distance; keep hunting until closer",
            )
            return

        adaptive_tiles = (
            self._adaptive_move_segment_tiles(grid, player)
            if grid is not None
            else self.move_segment_tiles
        )
        segment = self._choose_move_segment(
            path_preview,
            max_tiles=adaptive_tiles,
        )
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

        # Avoid issuing the exact same approach destination multiple times in a
        # few hundred milliseconds. The recording showed duplicate commands as
        # close as ~80-160 ms, which adds jitter without improving pursuit.
        now = time.time()
        if (
            self._last_combat_move_destination == segment
            and now - self._last_combat_move_sent_at < 0.45
        ):
            self._stop.wait(0.06)
            self._set_state("ROUTING", f"Continuing approach to {self.target_name}")
            return

        self._set_state(
            "APPROACHING",
            f"Approaching {self.target_name} via {segment[0]},{segment[1]}",
        )

        result = game_actions.move(
            player,
            segment,
            allow_mouse_fallback=not app_state.get_profile().hunt.native_only_actions,
        )
        if result.get("ok"):
            self._last_combat_move_destination = segment
            self._last_combat_move_sent_at = now
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
                result = game_actions.move(
                    player,
                    fallback,
                    allow_mouse_fallback=not app_state.get_profile().hunt.native_only_actions,
                )
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
                f"Could not execute route segment: {result.get('reason')}",
            )
            return

        end_pos = self._wait_for_move_progress(
            player,
            segment,
            int(self.target_id),
        )
        if end_pos is None:
            remembered = self._remembered_target()
            if remembered is None:
                lost_name = self.target_name
                self._clear_target()
                self._set_state("SEARCHING", f"Lost sight of {lost_name}; rescanning")
                return
            end_pos = self._position(authenticated_client_monitor.snapshot()) or player

        if end_pos != player and not self._attack_reposition_required:
            self._attack_reposition_origin = None

        fresh = authenticated_client_monitor.snapshot()
        actor = self._refresh_locked_target(fresh)
        if actor is None:
            remembered = self._remembered_target()
            if remembered is None:
                lost_name = self.target_name
                self._clear_target()
                self._set_state("SEARCHING", f"Lost sight of {lost_name}; rescanning")
                return
            self.target_pos = (int(remembered["x"]), int(remembered["y"]))
            self._set_state("ROUTING", f"Following last sighting of {self.target_name}")
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

    def _eligible_opening_skill_rule(
        self,
        snapshot: dict[str, Any],
    ):
        if self.target_id is None or self.target_name is None:
            return None
        if self._opening_skill_used_target_id == int(self.target_id):
            return None

        hunt = app_state.get_profile().hunt
        world = self._world(snapshot)
        sp_percent = world.get("sp_percent")
        if sp_percent is None:
            sp = world.get("sp")
            sp_max = world.get("sp_max")
            if sp is not None and sp_max:
                sp_percent = float(sp) * 100.0 / float(sp_max)

        target_name = str(self.target_name or "").strip().lower()
        now = time.time()

        for rule in [row for row in hunt.attack_skills if row.enabled]:
            allowed_monsters = {
                str(name).strip().lower()
                for name in (rule.monsters or [])
                if str(name).strip()
            }
            if allowed_monsters and target_name not in allowed_monsters:
                continue
            if sp_percent is None or float(sp_percent) < float(rule.min_sp_percent):
                continue
            cooldown = max(0.0, float(rule.cooldown_seconds))
            last_used = float(self._skill_last_used_at.get(int(rule.skill_id), 0.0))
            if cooldown and now - last_used < cooldown:
                continue
            return rule

        return None

    def _required_opening_range(
        self,
        snapshot: dict[str, Any],
    ) -> int | None:
        rule = self._eligible_opening_skill_rule(snapshot)
        if rule is None:
            return None
        return max(1, int(getattr(rule, "range_tiles", 1) or 1))

    def _try_opening_attack_skill(
        self,
        snapshot: dict[str, Any],
    ) -> bool:
        rule = self._eligible_opening_skill_rule(snapshot)
        if rule is None or self.target_id is None:
            return False

        world = self._world(snapshot)
        sp_percent = world.get("sp_percent")
        if sp_percent is None:
            sp = world.get("sp")
            sp_max = world.get("sp_max")
            if sp is not None and sp_max:
                sp_percent = float(sp) * 100.0 / float(sp_max)

        result = native_action_bridge.use_skill_to_id(
            int(rule.skill_id),
            int(rule.level),
            int(self.target_id),
        )
        self._last_skill_result = {
            "rule": {
                "skill_name": rule.skill_name,
                "skill_id": rule.skill_id,
                "level": rule.level,
                "min_sp_percent": rule.min_sp_percent,
                "range_tiles": getattr(rule, "range_tiles", 1),
                "post_skill_delay_seconds": getattr(rule, "post_skill_delay_seconds", 0.35),
            },
            "result": result,
        }
        self._log(
            "opening_skill_attempt",
            target_id=self.target_id,
            target_name=self.target_name,
            skill_name=rule.skill_name,
            skill_id=rule.skill_id,
            level=rule.level,
            sp_percent=sp_percent,
            result=result,
        )

        self._opening_skill_used_target_id = int(self.target_id)

        if result.get("ok"):
            self._skill_last_used_at[int(rule.skill_id)] = time.time()
            delay = max(0.0, float(getattr(rule, "post_skill_delay_seconds", 0.35) or 0.0))
            self._stop.wait(delay)
            return True
        return False

    def _step_attack_ready(self, snapshot: dict[str, Any]):
        if self._maybe_preempt_for_higher_priority_target(snapshot):
            return

        actor = self._refresh_locked_target(snapshot)
        player = self._position(snapshot)
        if actor is None:
            self._set_state("TARGET_DEAD", f"{self.target_name} disappeared")
            return
        if player is None or self.target_pos is None:
            self._stop.wait(0.10)
            return

        hunt = app_state.get_profile().hunt
        native_only = bool(hunt.native_only_actions)
        distance = self._tile_distance(player, self.target_pos)
        opener_range = self._required_opening_range(snapshot)
        required_range = (
            min(self._attack_commit_range, opener_range)
            if opener_range is not None
            else self._attack_commit_range
        )
        if hunt.attack_wait_approach_finish and distance > required_range:
            self._set_state(
                "ROUTING",
                f"{self.target_name} is too far for opening skill; approaching first",
            )
            return

        if hunt.attack_check_los:
            try:
                grid, _ = nav_repository.load(str(self._world(snapshot).get("map")))
            except Exception:
                grid = None
            if grid is None or not clear_walk_line(grid, player, self.target_pos):
                failed_name = self.target_name
                cooldown = max(1.0, float(hunt.failed_los_cooldown_seconds))
                if self.target_id is not None:
                    self._target_cooldown_until[int(self.target_id)] = time.time() + cooldown
                self._log(
                    "attack_failed_los",
                    target_id=self.target_id,
                    target_name=self.target_name,
                    cooldown_seconds=cooldown,
                )
                self._clear_target()
                self._set_state(
                    "SEARCHING",
                    f"No valid line/path to {failed_name}; choosing another target",
                )
                return

        player_render = self._render_position(snapshot)
        target_render = self._target_render_position(snapshot)

        if not game_actions.native_attack_ready():
            if native_only:
                self._set_state(
                    "FAILED",
                    "Native combat bridge is not ready; physical click fallback is disabled.",
                )
                return
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

        # Opening skills must land before the regular attack starts. This is
        # especially important for melee skills such as Bash on moving mobs.
        if self._required_opening_range(fresh) is not None:
            self._try_opening_attack_skill(fresh)
            fresh = authenticated_client_monitor.snapshot()
            actor = self._refresh_locked_target(fresh)
            player = self._position(fresh)
            if actor is None:
                self._set_state("TARGET_DEAD", f"{self.target_name} disappeared after opener")
                return
            if player is None or self.target_pos is None:
                return

        self.attack_clicked_at = time.time()

        if game_actions.native_attack_ready():
            result = game_actions.attack(
                actor_id=int(self.target_id) if self.target_id is not None else None,
                allow_mouse_fallback=False,
            )
        else:
            result = game_actions.attack(
                player_render,
                target_render,
                retry_index=self.attack_retry,
                target_key=self.target_name,
                actor_id=int(self.target_id) if self.target_id is not None else None,
                allow_mouse_fallback=True,
            )

        self._log(
            "attack_attempt",
            target_id=self.target_id,
            target_name=self.target_name,
            priority=self._monster_priority(self.target_name),
            retry=self.attack_retry,
            result=result,
        )

        if not result.get("ok"):
            # A learned map socket/bridge can occasionally miss one send during
            # map traffic. Refresh the exact locked actor and retry once before
            # abandoning the attack state.
            if game_actions.native_attack_ready() and self.target_id is not None:
                self._stop.wait(0.12)
                retry_snapshot = authenticated_client_monitor.snapshot()
                retry_actor = self._refresh_locked_target(retry_snapshot)
                if retry_actor is not None:
                    retry_result = game_actions.attack(
                        actor_id=int(self.target_id),
                        allow_mouse_fallback=False,
                    )
                    self._log(
                        "native_attack_retry",
                        target_id=self.target_id,
                        target_name=self.target_name,
                        first_result=result,
                        result=retry_result,
                    )
                    result = retry_result

            if not result.get("ok"):
                self._set_state(
                    "ROUTING",
                    f"Could not attack target: {result.get('reason')}",
                )
                return

        self._attack_backend = str(result.get("backend") or "native")
        self._combat_committed_target_id = (
            int(self.target_id) if self.target_id is not None else None
        )
        self._attack_origin = player
        self._combat_click_locked = False
        self._combat_seen = False
        self._set_state(
            "ATTACKING",
            f"Attack sent to {self.target_name}; verifying combat",
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
            self._combat_committed_target_id = int(self.target_id)
            game_actions.note_attack_registered(
                self.attack_retry,
                target_key=self.target_name,
            )
            self._log(
                "client_attack_registered",
                target_id=self.target_id,
                target_name=self.target_name,
                combat_lock=True,
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
                actor_id=int(self.target_id) if self.target_id is not None else None,
                allow_mouse_fallback=not app_state.get_profile().hunt.native_only_actions,
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
        # Once the first attack has been sent, this target owns the combat lock
        # until it disappears/dies. New monsters are queued for the next
        # decision instead of causing mid-kill target switches.
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

        committed = (
            self.target_id is not None
            and self._combat_committed_target_id is not None
            and int(self.target_id) == int(self._combat_committed_target_id)
        )
        if (
            not committed
            and not self._combat_seen
            and time.time() - self.attack_clicked_at
            >= max(0.8, float(app_state.get_profile().hunt.combat_no_progress_timeout))
        ):
            target_id = int(self.target_id) if self.target_id is not None else -1
            failures = self._target_failure_count.get(target_id, 0) + 1
            self._target_failure_count[target_id] = failures
            self._log(
                "combat_no_progress",
                target_id=self.target_id,
                target_name=self.target_name,
                failures=failures,
            )
            self._set_state(
                "ROUTING",
                f"No combat progress on {self.target_name}; trying a better approach",
            )
            return

        self._stop.wait(0.06)

    def _reset_wander_navigation(self, *, release_move: bool = False) -> None:
        if release_move:
            game_actions.release_hold_move()
        self.wander_path = []
        self.wander_goal = None
        self.wander_progress_index = 0
        self.wander_line_points = []
        self.wander_line_index = 0
        self.wander_reconnect_target = None
        self.wander_reconnect_route_index = None
        self._native_wander_destination = None
        self._native_wander_sent_at = 0.0
        self._wander_last_position = None
        self._wander_last_progress_at = time.time()

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
        # Never inherit a stale pre-combat wander destination after a kill.
        # Start the next exploration leg from the character's actual position.
        self._reset_wander_navigation(release_move=True)

        hunt = app_state.get_profile().hunt
        looting_enabled = self._monster_looting_enabled(old_name)

        # When looting is enabled the drop-spawn wait already provides a short,
        # natural post-kill beat. Do not stack another pause on top of it.
        if (
            not looting_enabled
            and random.random()
                < max(0.0, min(1.0, float(hunt.post_kill_pause_chance)))
        ):
            low = max(0.0, float(hunt.post_kill_pause_min))
            high = max(low, float(hunt.post_kill_pause_max))
            pause = random.uniform(low, high)
            self._log("post_kill_pause", seconds=round(pause, 3))
            self._stop.wait(pause)

        snapshot = authenticated_client_monitor.snapshot()

        # Strict kill -> loot -> rescan -> next target lifecycle. Do not chain
        # another visible monster before giving this kill's drops time to appear.
        if looting_enabled:
            low = max(0.0, float(hunt.loot_drop_delay_min))
            high = max(low, float(hunt.loot_drop_delay_max))
            self._loot_not_before = time.time() + random.uniform(low, high)
            self._set_state(
                "LOOTING",
                f"{old_name or 'Monster'} dead; waiting for drops before next target",
            )
            return

        # Loot disabled for this monster: still perform a completely fresh
        # target scan before any wandering command can be sent.
        fresh = authenticated_client_monitor.snapshot()
        candidates = self._eligible_target_candidates(fresh)
        self._log(
            "post_kill_rescan",
            killed_target_id=old_id,
            killed_target_name=old_name,
            candidate_count=len(candidates),
            candidates=[
                {
                    "id": row[2].get("id"),
                    "name": row[2].get("name"),
                    "priority": row[0],
                    "distance": row[1],
                }
                for row in candidates
            ],
        )
        if self._acquire_target(fresh):
            self._set_state(
                "TARGET_SELECTED",
                f"Next visible target after kill: {self.target_name}",
            )
            return

        self._set_state("SEARCHING", "No visible target after kill; exploring")

    def _approach_loot_item(
        self,
        item_id: int,
        item_pos: tuple[int, int],
        *,
        pickup_distance: int = 2,
        timeout: float = 6.0,
    ) -> bool:
        deadline = time.time() + timeout
        last_move_at = 0.0

        while not self._stop.is_set() and time.time() < deadline:
            snapshot = authenticated_client_monitor.snapshot()
            live = snapshot.get("live_state") or {}
            floor_items = live.get("floor_items") or []
            if not any(int(row.get("id") or -1) == int(item_id) for row in floor_items):
                return True

            player = self._position(snapshot)
            if player is None:
                self._stop.wait(0.05)
                continue

            if self._tile_distance(player, item_pos) <= pickup_distance:
                game_actions.release_hold_move()
                return True

            now = time.time()
            if now - last_move_at >= 0.55:
                result = game_actions.move(
                    player,
                    item_pos,
                    allow_mouse_fallback=not app_state.get_profile().hunt.native_only_actions,
                )
                self._log(
                    "loot_approach_move",
                    item_id=item_id,
                    from_pos={"x": player[0], "y": player[1]},
                    destination={"x": item_pos[0], "y": item_pos[1]},
                    result=result,
                )
                if not result.get("ok"):
                    return False
                last_move_at = now

            self.message = (
                f"Walking to loot at {item_pos[0]},{item_pos[1]} "
                f"({self._tile_distance(player, item_pos)} tiles away)"
            )
            self._stop.wait(0.08)

        game_actions.release_hold_move()
        return False

    def _step_looting(self, snapshot: dict[str, Any]):
        now = time.time()
        if now < self._loot_not_before or now < self._next_loot_at:
            self._stop.wait(0.04)
            return

        items = self._loot_candidates(snapshot)
        if not items:
            self.loot_retry.clear()
            self._loot_first_attempt_at.clear()
            self._reset_wander_navigation(release_move=True)

            # Loot phase is complete. Re-read the authenticated client actor
            # table now, rather than reusing the snapshot from before/while
            # looting. This is the authoritative post-loot monster rescan.
            fresh = authenticated_client_monitor.snapshot()
            candidates = self._eligible_target_candidates(fresh)
            self._log(
                "post_loot_rescan",
                candidate_count=len(candidates),
                candidates=[
                    {
                        "id": row[2].get("id"),
                        "name": row[2].get("name"),
                        "priority": row[0],
                        "distance": row[1],
                    }
                    for row in candidates
                ],
            )

            if self._acquire_target(fresh):
                self._set_state(
                    "TARGET_SELECTED",
                    f"Loot complete; next visible target: {self.target_name}",
                )
                return

            self._set_state("SEARCHING", "Loot complete; no visible target")
            return

        player = self._position(snapshot)
        if player is None:
            self._stop.wait(0.04)
            return

        item = items[0]
        item_id = int(item["id"])
        item_pos = (int(item["x"]), int(item["y"]))

        # Native pickup only succeeds inside Ragnarok's pickup range. If the
        # drop is farther away, walk close to it first instead of repeatedly
        # sending pickup and getting "You cannot get the item".
        if self._tile_distance(player, item_pos) > 2:
            if not self._approach_loot_item(item_id, item_pos):
                if self.state != "LOOTING":
                    return
                self.loot_ignored.add(item_id)
                self._log(
                    "loot_approach_failed",
                    item_id=item_id,
                    item_name_id=item.get("name_id"),
                    x=item_pos[0],
                    y=item_pos[1],
                )
                self._set_state(
                    "SEARCHING",
                    "Could not approach loot; continuing hunt",
                )
                return

            fresh = authenticated_client_monitor.snapshot()
            fresh_items = {
                int(row.get("id") or -1): row
                for row in ((fresh.get("live_state") or {}).get("floor_items") or [])
            }
            if item_id not in fresh_items:
                self.loot_retry.pop(item_id, None)
                self._loot_first_attempt_at.pop(item_id, None)
                return
            player = self._position(fresh)
            if player is None:
                return

        # Start the pickup timeout only after we are actually within pickup
        # range; walking time must not count as a failed pickup attempt.
        self._loot_first_attempt_at.setdefault(item_id, time.time())
        if (
            time.time() - self._loot_first_attempt_at[item_id]
            >= max(0.5, float(app_state.get_profile().hunt.loot_giveup_seconds))
        ):
            self.loot_ignored.add(item_id)
            self.loot_retry.pop(item_id, None)
            self._loot_first_attempt_at.pop(item_id, None)
            self._log(
                "loot_giveup_timeout",
                item_id=item_id,
                item_name_id=item.get("name_id"),
            )
            self._set_state(
                "SEARCHING",
                "Loot timeout reached; continuing hunt",
            )
            return
        player_render = self._render_position(snapshot) or (
            float(player[0]),
            float(player[1]),
        )
        result = game_actions.loot(
            player_render,
            item_pos,
            item_id=item_id,
            allow_mouse_fallback=not app_state.get_profile().hunt.native_only_actions,
        )
        self._log(
            "loot_attempt",
            item_id=item_id,
            item_name_id=item.get("name_id"),
            x=item_pos[0],
            y=item_pos[1],
            result=result,
        )

        if not result.get("ok"):
            retries = self.loot_retry.get(item_id, 0) + 1
            self.loot_retry[item_id] = retries
            if retries >= self.max_loot_retries:
                self.loot_ignored.add(item_id)
                self.loot_retry.pop(item_id, None)
                self._log(
                    "loot_skipped_after_retries",
                    item_id=item_id,
                    item_name_id=item.get("name_id"),
                    retries=retries,
                    reason=result.get("reason"),
                )
                self._set_state(
                    "SEARCHING",
                    "Skipping unlootable floor item; continuing hunt",
                )
                return
            self._stop.wait(0.06)
            return

        deadline = time.time() + 1.2
        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(0.04)
            fresh = authenticated_client_monitor.snapshot()

            ids = {
                int(entry["id"])
                for entry in ((fresh.get("live_state") or {}).get("floor_items") or [])
            }
            if item_id not in ids:
                self.loot_retry.pop(item_id, None)
                self._loot_first_attempt_at.pop(item_id, None)
                hunt = app_state.get_profile().hunt
                low = max(0.0, float(hunt.loot_between_items_min))
                high = max(low, float(hunt.loot_between_items_max))
                self._next_loot_at = time.time() + ((low + high) / 2.0)
                return

        retries = self.loot_retry.get(item_id, 0) + 1
        self.loot_retry[item_id] = retries
        if retries >= self.max_loot_retries:
            self.loot_ignored.add(item_id)
            self.loot_retry.pop(item_id, None)
            self._log(
                "loot_skipped_after_retries",
                item_id=item_id,
                item_name_id=item.get("name_id"),
                retries=retries,
                reason="floor_item_did_not_disappear",
            )
            self._set_state(
                "SEARCHING",
                "Loot did not disappear; skipping it and continuing hunt",
            )

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

        now = time.time()
        hunt = app_state.get_profile().hunt
        saved_route = hunt_route_store.get(str(map_name)).get("exists")

        # Rare short pauses break up endless perfectly continuous roaming.
        # In native mode, sending the current tile acts as a gentle stop request.
        if not saved_route and now >= self._next_roam_pause_at:
            low = max(0.1, float(hunt.roam_pause_min))
            high = max(low, float(hunt.roam_pause_max))
            pause = random.uniform(low, high)
            if game_actions.native_move_ready():
                game_actions.move_to(player)
                self._native_wander_destination = player
                self._native_wander_sent_at = now
            else:
                game_actions.release_hold_move()
            self._log("roam_micro_pause", seconds=round(pause, 3))
            self._schedule_next_roam_pause()
            self._stop.wait(pause)
            return

        # Once the destination area is already within normal screen-awareness
        # range and still contains no eligible target, don't march to an exact
        # arbitrary endpoint. Reconsider from the newly revealed area instead.
        if (
            not saved_route
            and self.wander_goal is not None
            and self.wander_path
            and self._tile_distance(player, self.wander_goal)
                <= int(self._wander_visibility_stop_distance)
            and now - self._last_roam_reconsider_at >= 1.5
        ):
            self._last_roam_reconsider_at = now
            old_goal = self.wander_goal
            self._log(
                "exploration_visibility_reconsider",
                player={"x": player[0], "y": player[1]},
                previous_goal={"x": old_goal[0], "y": old_goal[1]},
                reveal_distance=self._wander_visibility_stop_distance,
            )
            self._reset_wander_navigation(release_move=False)
            if not self._choose_wander_path(snapshot):
                self._set_state("SEARCHING", "Visible area checked; rescanning")
                self._stop.wait(0.08)
                return

        if self._wander_last_position != player:
            self._wander_last_position = player
            self._wander_last_progress_at = now
        elif (
            self.wander_path
            and now - self._wander_last_progress_at >= 1.7
        ):
            # A stale path after combat/loot can leave Ragnarok visually idle.
            # Throw that path away and immediately search for a fresh route.
            self._log(
                "wander_stall_recovery",
                map=str(map_name),
                player={"x": player[0], "y": player[1]},
                stalled_seconds=round(now - self._wander_last_progress_at, 2),
            )
            game_actions.release_hold_move()
            self.wander_path = []
            self.wander_goal = None
            self.wander_progress_index = 0
            self.wander_line_points = []
            self.wander_line_index = 0
            self.wander_reconnect_target = None
            self.wander_reconnect_route_index = None
            self._native_wander_destination = None
            self._native_wander_sent_at = 0.0
            self._wander_last_progress_at = now

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
                self._set_state(
                    "SEARCHING",
                    "No usable wander route; retrying monster search",
                )
                self._stop.wait(0.12)
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
                or now - self._native_wander_sent_at >= 1.15
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
            if app_state.get_profile().hunt.native_only_actions:
                game_actions.release_hold_move()
                self._set_state(
                    "FAILED",
                    "Native movement bridge is not ready; physical mouse fallback is disabled.",
                )
                return

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
            self._next_heal_threshold = None
            return False

        world = self._world(snapshot)
        hp = world.get("hp")
        hp_max = world.get("hp_max")
        if hp is None or not hp_max or int(hp) <= 0:
            return False

        hp_percent = float(world.get("hp_percent") or (int(hp) * 100 / int(hp_max)))
        min_threshold = max(1, min(99, int(settings.hp_trigger_min_percent)))
        max_threshold = max(min_threshold, min(99, int(settings.hp_trigger_max_percent)))

        # Pick one threshold and keep it until it actually triggers. Re-rolling
        # every AI tick would make healing erratic and less human-looking.
        if self._next_heal_threshold is None:
            self._next_heal_threshold = random.randint(min_threshold, max_threshold)
        threshold = int(self._next_heal_threshold)

        if hp_percent >= threshold:
            return False

        now = time.time()
        cooldown = max(0.35, float(settings.cooldown_seconds))
        if now - self._last_heal_hotkey_at < cooldown:
            return False

        burst_min = max(1, min(10, int(settings.burst_min_items)))
        burst_max = max(burst_min, min(10, int(settings.burst_max_items)))
        requested_uses = random.randint(burst_min, burst_max)
        delay = max(0.0, min(2.0, float(settings.burst_delay_seconds)))

        native_only = bool(profile.hunt.native_only_actions)
        wanted_name = str(settings.item or "Meat").strip().casefold()
        results: list[dict[str, Any]] = []

        for use_index in range(requested_uses):
            if self._stop.is_set():
                break

            result: dict[str, Any]
            if native_only:
                items = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
                item = next(
                    (
                        row for row in items
                        if str(row.get("name") or "").strip().casefold() == wanted_name
                        or (wanted_name == "meat" and int(row.get("name_id") or -1) == 517)
                    ),
                    None,
                )
                target_id = world.get("self_account_id") or world.get("self_char_id")
                if item is None:
                    result = {
                        "ok": False,
                        "backend": "native",
                        "reason": "healing_item_not_found",
                        "item": settings.item or "Meat",
                    }
                elif target_id is None:
                    result = {
                        "ok": False,
                        "backend": "native",
                        "reason": "self_target_id_unknown",
                    }
                else:
                    result = native_action_bridge.item_use(
                        int(item["index"]),
                        int(target_id),
                    )
                    result["backend"] = "native"
                    result["input_mode"] = "inventory_index"
            else:
                result = game_actions.press_hotkey(settings.hotkey or "1")

            results.append(dict(result))
            if not result.get("ok"):
                break

            # Only multi-use bursts pause between items. Exactly 0.2s by
            # default, matching a quick but believable manual double/triple use.
            if use_index < requested_uses - 1 and delay > 0:
                self._stop.wait(delay)

        self._last_heal_hotkey_at = time.time()
        successful_uses = sum(1 for row in results if row.get("ok"))
        self._last_heal_result = {
            "ok": successful_uses > 0,
            "requested_uses": requested_uses,
            "successful_uses": successful_uses,
            "delay_seconds": delay,
            "results": results,
        }
        self._log(
            "healing_action",
            hp=int(hp),
            hp_max=int(hp_max),
            hp_percent=round(hp_percent, 1),
            threshold=threshold,
            trigger_range=[min_threshold, max_threshold],
            requested_uses=requested_uses,
            successful_uses=successful_uses,
            burst_delay_seconds=delay,
            item=settings.item or "Meat",
            hotkey=settings.hotkey or "1",
            result=self._last_heal_result,
        )

        # A completed healing decision gets a fresh threshold next time.
        # Keeping this reset event-based prevents a predictable fixed cutoff.
        self._next_heal_threshold = random.randint(min_threshold, max_threshold)
        return successful_uses > 0

    def _maybe_use_aspd_potion(self, snapshot: dict[str, Any]) -> bool:
        profile = app_state.get_profile()
        settings = profile.aspd
        if not settings.enabled:
            return False

        now = time.time()
        reuse_seconds = max(30.0, float(settings.reuse_minutes) * 60.0)

        # Prefer the actual client status-effect state over a blind timer.
        # Awakening Potion uses EFST_ATTHASTE_POTION2 (38). If the effect is
        # active, never consume another potion. Once the server reports that
        # effect gone, the bot may reactivate it immediately.
        status_effect_id = getattr(settings, "status_effect_id", None)
        if status_effect_id is not None:
            active = authenticated_client_monitor.status_active(int(status_effect_id))
            if active is True:
                return False
            if active is None and self._last_aspd_use_at:
                # If this client build does not expose status packets, keep the
                # previous duration safeguard rather than consuming repeatedly.
                if now - self._last_aspd_use_at < reuse_seconds:
                    return False
        elif self._last_aspd_use_at and now - self._last_aspd_use_at < reuse_seconds:
            return False

        world = self._world(snapshot)
        target_id = world.get("self_account_id") or world.get("self_char_id")
        if target_id is None:
            return False

        wanted_name = str(settings.item or "").strip().casefold()
        wanted_id = int(settings.name_id) if settings.name_id is not None else None
        items = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
        item = next(
            (
                row for row in items
                if (
                    wanted_id is not None
                    and int(row.get("name_id") or -1) == wanted_id
                )
                or (
                    wanted_name
                    and str(row.get("name") or "").strip().casefold() == wanted_name
                )
            ),
            None,
        )
        if item is None:
            self._last_aspd_result = {
                "ok": False,
                "reason": "aspd_item_not_found",
                "item": settings.item,
                "name_id": settings.name_id,
            }
            return False

        result = native_action_bridge.item_use(
            int(item["index"]),
            int(target_id),
        )
        result["backend"] = "native"
        result["input_mode"] = "inventory_index"
        self._last_aspd_result = dict(result)
        self._log(
            "aspd_consumable",
            item=item.get("name") or settings.item,
            name_id=item.get("name_id"),
            reuse_minutes=settings.reuse_minutes,
            status_effect_id=status_effect_id,
            status_active=(
                authenticated_client_monitor.status_active(int(status_effect_id))
                if status_effect_id is not None else None
            ),
            result=result,
        )
        if result.get("ok"):
            self._last_aspd_use_at = now
            return True
        return False

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
            if (
                game_actions.native_attack_ready()
                or (
                    not app_state.get_profile().hunt.native_only_actions
                    and game_actions.can_project(
                        player,
                        self.target_pos,
                        sprite=True,
                    )
                )
            ):
                self._set_state(
                    "ATTACK_READY",
                    f"Recovering by attacking {self.target_name}",
                )
                return

            target_id = int(self.target_id) if self.target_id is not None else -1
            failures = self._target_failure_count.get(target_id, 0) + 1
            self._target_failure_count[target_id] = failures
            self._log(
                "target_retry",
                target_id=self.target_id,
                target_name=self.target_name,
                failures=failures,
                reason=self.message,
            )
            if failures >= 2:
                failed_id = self.target_id
                failed_name = self.target_name
                self._cooldown_target(failed_id, "failed_state_repeatedly")
                self._clear_target()
                self._set_state(
                    "SEARCHING",
                    f"Skipping stuck target {failed_name}; continuing hunt",
                )
                return
            self._stop.wait(0.20)
            self._set_state("ROUTING", "Retrying same locked target")
            return

        self._clear_target()
        self._set_state("SEARCHING", "Target gone; selecting another target")

    def _maybe_recover_hunting_liveness(
        self,
        snapshot: dict[str, Any],
    ) -> bool:
        player = self._position(snapshot)
        if player is None:
            return False

        now = time.time()
        if self._liveness_last_position != player:
            self._liveness_last_position = player
            self._liveness_last_progress_at = now
            self._liveness_recoveries = 0
            return False

        if self.state not in {"SEARCHING", "WANDERING"}:
            return False

        timeout = max(
            1.0,
            float(app_state.get_profile().hunt.hunting_liveness_timeout),
        )
        if now - self._liveness_last_progress_at < timeout:
            return False

        self._liveness_recoveries += 1
        self._log(
            "hunting_liveness_recovery",
            state=self.state,
            player={"x": player[0], "y": player[1]},
            stationary_seconds=round(now - self._liveness_last_progress_at, 2),
            recovery=self._liveness_recoveries,
        )
        self._reset_wander_navigation(release_move=True)
        self._liveness_last_progress_at = now
        self._set_state(
            "SEARCHING",
            "Idle watchdog reset navigation; selecting a fresh route",
        )
        return True

    def _loop(self, run_id: int):
        self._set_state("SEARCHING", "Searching for monster")

        while not self._stop.is_set() and run_id == self._run_id:
            snapshot = authenticated_client_monitor.snapshot()
            self._observe_monster_memory(snapshot)

            if not snapshot.get("classic_pid"):
                self._set_state("IDLE", "Waiting for Classic.exe")
                self._stop.wait(0.10)
                continue

            if self._maybe_recover_hunting_liveness(snapshot):
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
            self._maybe_use_aspd_potion(snapshot)
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

        profile = app_state.get_profile()
        self.loot_radius = max(2, min(20, int(profile.hunt.loot_radius)))

        with self._lock:
            if self.running:
                return self.snapshot()
            if not authenticated_client_monitor.snapshot().get("classic_pid"):
                raise RuntimeError(
                    "Classic.exe is not detected. Launch SoulBound and enter the game first."
                )

            if profile.hunt.native_only_actions:
                native = native_action_bridge.snapshot()
                if not native.get("attached"):
                    native_action_bridge.start()
                if not native_action_bridge.snapshot().get("agent", {}).get("socket_learned"):
                    raise RuntimeError(
                        "Native hunting is enabled but the authenticated map socket "
                        "has not been learned yet."
                    )
            elif not game_actions.calibration_valid():
                raise RuntimeError(
                    "A valid screen calibration is required when physical fallback is enabled."
                )

            self._run_id += 1
            run_id = self._run_id
            self._stop.clear()
            self._clear_target()
            self.saved_route_map = None
            self.saved_route_waypoint_index = 0
            self.saved_route_direction = 1
            self._wander_last_position = None
            self._wander_last_progress_at = time.time()
            self._monster_memory.clear()
            self._reaction_ready_at.clear()
            self._trusted_position = None
            self._trusted_position_map = None
            self._trusted_position_at = 0.0
            self._position_candidate = None
            self._position_candidate_since = 0.0
            self._position_reject_count = 0
            self._position_last_rejected = None
            self._attack_commit_range = self.attack_range
            self._roam_pause_until = 0.0
            self._last_roam_reconsider_at = 0.0
            self._schedule_next_roam_pause()
            self._target_failure_count.clear()
            self._target_cooldown_until.clear()
            self._target_locked_at.clear()
            self._loot_not_before = 0.0
            self._next_loot_at = 0.0
            self._loot_first_attempt_at.clear()
            self._liveness_last_position = self._position(authenticated_client_monitor.snapshot())
            self._liveness_last_progress_at = time.time()
            self._liveness_recoveries = 0
            self._last_aspd_use_at = 0.0
            self._last_aspd_result = None
            self._opening_skill_used_target_id = None
            self._skill_last_used_at.clear()
            self._last_skill_result = None
            self.loot_retry.clear()
            self.loot_ignored.clear()
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
                "combat_lock": {
                    "committed_target_id": self._combat_committed_target_id,
                    "locked": (
                        self.target_id is not None
                        and self._combat_committed_target_id is not None
                        and int(self.target_id) == int(self._combat_committed_target_id)
                    ),
                },
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
                    "smart_combat": {
                        "native_only_actions": app_state.get_profile().hunt.native_only_actions,
                        "threat_first_combat": app_state.get_profile().hunt.threat_first_combat,
                        "preempt_for_higher_priority_aggressor": app_state.get_profile().hunt.preempt_for_higher_priority_aggressor,
                        "loot_after_aggressors": app_state.get_profile().hunt.loot_after_aggressors,
                        "exploration_frontier_bias": app_state.get_profile().hunt.exploration_frontier_bias,
                        "attack_check_los": app_state.get_profile().hunt.attack_check_los,
                        "attack_wait_approach_finish": app_state.get_profile().hunt.attack_wait_approach_finish,
                        "attack_max_route_time": app_state.get_profile().hunt.attack_max_route_time,
                        "attack_route_max_path_distance": app_state.get_profile().hunt.attack_route_max_path_distance,
                        "failed_los_cooldown_seconds": app_state.get_profile().hunt.failed_los_cooldown_seconds,
                        "move_giveup_seconds": app_state.get_profile().hunt.move_giveup_seconds,
                        "loot_giveup_seconds": app_state.get_profile().hunt.loot_giveup_seconds,
                        "hunting_liveness_timeout": app_state.get_profile().hunt.hunting_liveness_timeout,
                    },
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
                        "trigger_min_percent": app_state.get_profile().healing.hp_trigger_min_percent,
                        "trigger_max_percent": app_state.get_profile().healing.hp_trigger_max_percent,
                        "next_trigger_percent": self._next_heal_threshold,
                        "burst_min_items": app_state.get_profile().healing.burst_min_items,
                        "burst_max_items": app_state.get_profile().healing.burst_max_items,
                        "burst_delay_seconds": app_state.get_profile().healing.burst_delay_seconds,
                        "last_result": self._last_heal_result,
                    },
                    "aspd": {
                        "enabled": app_state.get_profile().aspd.enabled,
                        "item": app_state.get_profile().aspd.item,
                        "name_id": app_state.get_profile().aspd.name_id,
                        "reuse_minutes": app_state.get_profile().aspd.reuse_minutes,
                        "last_used_at": self._last_aspd_use_at or None,
                        "last_result": self._last_aspd_result,
                    },
                    "skills": {
                        "attack_rules": [
                            {
                                "skill_name": rule.skill_name,
                                "skill_id": rule.skill_id,
                                "level": rule.level,
                                "min_sp_percent": rule.min_sp_percent,
                                "first_attack_only": rule.first_attack_only,
                                "monsters": rule.monsters,
                            }
                            for rule in app_state.get_profile().hunt.attack_skills
                        ],
                        "last_result": self._last_skill_result,
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
                "position_guard": self._position_guard_snapshot(),
                "map_debug": {
                    "navigation": {
                        "wander_goal": (
                            {"x": self.wander_goal[0], "y": self.wander_goal[1]}
                            if self.wander_goal else None
                        ),
                        "wander_progress_index": self.wander_progress_index,
                        "astar_path": [{"x": x, "y": y} for x, y in self.wander_path],
                        "straight_segments": [{"x": x, "y": y} for x, y in self.wander_line_points],
                    },
                    "recent_kills": [
                        {
                            "time": row.get("time"),
                            "x": row.get("pos")[0] if row.get("pos") else None,
                            "y": row.get("pos")[1] if row.get("pos") else None,
                            "monster": row.get("monster"),
                        }
                        for row in self.recent_kills[-10:]
                    ],
                },
                "calibration": game_actions.calibration_snapshot(),
                "actions": self.actions[-30:],
                "architecture": (
                    "OpenKore-style native hunting in map coordinates; "
                    "movement, actor attacks and floor-item pickup prefer authenticated "
                    "in-client actions with physical click fallback disabled by default."
                ),
            }


hunting_ai = HuntingAI()
