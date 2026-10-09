from __future__ import annotations

import threading
import time
from typing import Any

from core.game_actions import game_actions
from core.pathing import astar, nav_repository
from core.state import app_state
from core.world_route import world_route_planner
from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.native_action_bridge import native_action_bridge


class TownTravelController:
    """Route between maps using native map-coordinate movement by default.\n\n    Screen calibration is only relevant when the user explicitly enables the\n    legacy physical mouse fallback. Native/OpenKore-style travel is resolution\n    and Windows-scaling independent.\n    """

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.state = "IDLE"
        self.message = "Idle"
        self.destination_town: str | None = None
        self.destination_map: str | None = None
        self.mode = "town"
        self.current_leg: dict[str, Any] | None = None
        self.actions: list[dict[str, Any]] = []
        self.lookahead = 10
        self.cursor_radius = 180
        self._native_destination: tuple[int, int] | None = None
        self._native_sent_at = 0.0

    def _log(self, action: str, **details):
        with self._lock:
            self.actions.append({"time": time.time(), "action": action, **details})
            self.actions = self.actions[-60:]

    @staticmethod
    def _snapshot():
        return authenticated_client_monitor.snapshot()

    @staticmethod
    def _world(snapshot: dict[str, Any]) -> dict[str, Any]:
        return (snapshot.get("live_state") or {}).get("world") or {}

    @staticmethod
    def _position(snapshot: dict[str, Any]) -> tuple[int, int] | None:
        world = TownTravelController._world(snapshot)
        x, y = world.get("x"), world.get("y")
        if x is None or y is None:
            return None
        return int(x), int(y)

    def _set(self, state: str, message: str):
        with self._lock:
            self.state = state
            self.message = message
        app_state.patch_runtime(
            current_action=state.replace("_", " ").title(),
            message=message,
        )

    @staticmethod
    def _native_only() -> bool:
        return bool(app_state.get_profile().hunt.native_only_actions)

    def _ensure_action_mode_ready(self, timeout: float = 6.0) -> None:
        """Require native actions in native-only mode; calibration only for fallback."""
        if not self._native_only():
            if not game_actions.calibration_valid():
                raise RuntimeError(
                    "A valid screen calibration is required only when physical "
                    "mouse fallback is enabled."
                )
            return

        state = native_action_bridge.snapshot()
        if not state.get("attached"):
            native_action_bridge.start()

        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            state = native_action_bridge.snapshot()
            if (
                state.get("attached")
                and state.get("status") == "ready"
                and (state.get("agent") or {}).get("socket_learned")
            ):
                return
            time.sleep(0.10)

        raise RuntimeError(
            "Native movement is not ready yet. Enter the game and wait until "
            "the authenticated map socket is learned; screen calibration is "
            "not required in Native only mode."
        )

    def _route_on_map(
        self,
        map_name: str,
        start: tuple[int, int],
        goal: tuple[int, int],
    ) -> list[tuple[int, int]] | None:
        try:
            grid, _ = nav_repository.load(map_name)
        except Exception:
            return None
        return astar(grid, start, goal, max_expansions=150000)

    def _nearest_index(
        self,
        path: list[tuple[int, int]],
        pos: tuple[int, int],
    ) -> int:
        best_i = 0
        best_d = 999999
        for i, point in enumerate(path):
            d = max(abs(point[0] - pos[0]), abs(point[1] - pos[1]))
            if d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _walk_to_point(
        self,
        map_name: str,
        goal: tuple[int, int],
        *,
        tolerance: int = 3,
        timeout: float = 45.0,
    ) -> bool:
        initial = self._snapshot()
        if str(self._world(initial).get("map") or "") != map_name:
            return False
        player = self._position(initial)
        if player is None:
            return False
        path = self._route_on_map(map_name, player, goal)
        if not path:
            self._set("ERROR", f"No walkable route to {map_name} {goal[0]},{goal[1]}")
            return False

        deadline = time.time() + timeout
        while not self._stop.is_set() and time.time() < deadline:
            snapshot = self._snapshot()
            world = self._world(snapshot)
            if str(world.get("map") or "") != map_name:
                return True
            player = self._position(snapshot)
            if player is None:
                self._stop.wait(0.05)
                continue
            if max(abs(player[0] - goal[0]), abs(player[1] - goal[1])) <= tolerance:
                game_actions.release_hold_move()
                return True

            index = self._nearest_index(path, player)
            lookahead = min(len(path) - 1, index + self.lookahead)
            destination = path[lookahead]

            if game_actions.native_move_ready():
                result = game_actions.move_to(destination)
            elif self._native_only():
                self._set(
                    "ERROR",
                    "Native movement became unavailable; screen fallback is disabled.",
                )
                return False
            else:
                result = game_actions.update_hold_direction(
                    destination[0] - player[0],
                    destination[1] - player[1],
                    radius_px=self.cursor_radius,
                    min_pixel_change=16,
                )
            if not result.get("ok"):
                self._stop.wait(0.10)
            else:
                self._stop.wait(0.08)

        game_actions.release_hold_move()
        return False

    def _nearest_other_actor(
        self,
        point: tuple[int, int],
        *,
        max_distance: int = 8,
    ) -> dict[str, Any] | None:
        snapshot = self._snapshot()
        live = snapshot.get("live_state") or {}
        candidates = []
        for actor in live.get("actors") or []:
            if actor.get("kind") != "other":
                continue
            x, y = actor.get("x"), actor.get("y")
            if x is None or y is None:
                continue
            d = max(abs(int(x) - point[0]), abs(int(y) - point[1]))
            if d <= max_distance:
                candidates.append((d, actor))
        if not candidates:
            return None
        candidates.sort(key=lambda row: row[0])
        return dict(candidates[0][1])

    def _execute_interactive_portal(self, leg: dict[str, Any]) -> bool:
        source_map = str(leg["source_map"])
        point = (int(leg["source_x"]), int(leg["source_y"]))
        if not self._walk_to_point(source_map, point, tolerance=4):
            return False

        actor = self._nearest_other_actor(point, max_distance=10)
        if actor is None:
            self._set(
                "ERROR",
                f"No NPC visible near interactive portal {source_map} {point[0]},{point[1]}",
            )
            return False

        actor_id = int(actor["id"])
        before_map = str(self._world(self._snapshot()).get("map") or "")
        self._set(
            "INTERACTIVE_PORTAL",
            f"Using NPC route to {leg['dest_map']}",
        )

        result = native_action_bridge.talk_npc(actor_id, 1)
        if not result.get("ok"):
            self._set("ERROR", f"Could not talk to portal NPC: {result.get('reason')}")
            return False
        self._stop.wait(0.25)

        for token in list(leg.get("interaction_steps") or []):
            token = str(token).strip().lower()
            if not token or token.isdigit():
                continue
            if token == "c":
                result = native_action_bridge.continue_npc(actor_id)
            elif token.startswith("r") and token[1:].isdigit():
                # OpenKore route syntax is zero-based (r0 = first menu entry),
                # while Ragnarok's menu response packet is one-based.
                result = native_action_bridge.choose_npc_option(
                    actor_id,
                    int(token[1:]) + 1,
                )
            elif token == "n":
                result = native_action_bridge.close_npc(actor_id)
            else:
                continue
            if not result.get("ok"):
                self._set(
                    "ERROR",
                    f"Interactive portal step {token} failed: {result.get('reason')}",
                )
                return False
            self._stop.wait(0.25)

        deadline = time.time() + 8.0
        while not self._stop.is_set() and time.time() < deadline:
            current = str(self._world(self._snapshot()).get("map") or "")
            if current and current != before_map:
                self._log(
                    "interactive_portal_crossed",
                    source_map=before_map,
                    dest_map=current,
                    actor_id=actor_id,
                )
                return True
            self._stop.wait(0.10)

        self._set("ERROR", "Interactive portal did not change map.")
        return False

    def _force_portal_entry(
        self,
        source_map: str,
        portal: tuple[int, int],
        *,
        timeout: float = 4.0,
    ) -> bool:
        """Actively step across a physical portal trigger when pathing stops short.

        OpenKore portal coordinates and the local nav grid can differ by one or
        two cells. Once we are near the portal, probe only the immediate 3x3
        trigger area and wait for the actual server map change.
        """
        before_map = source_map
        deadline = time.time() + timeout
        last_send = 0.0

        # Exact trigger first, then immediate neighbors. This stays tightly
        # bounded around the known portal instead of wandering away.
        candidates = [
            portal,
            (portal[0] + 1, portal[1]),
            (portal[0] - 1, portal[1]),
            (portal[0], portal[1] + 1),
            (portal[0], portal[1] - 1),
            (portal[0] + 1, portal[1] + 1),
            (portal[0] + 1, portal[1] - 1),
            (portal[0] - 1, portal[1] + 1),
            (portal[0] - 1, portal[1] - 1),
        ]
        attempt = 0

        while not self._stop.is_set() and time.time() < deadline:
            snapshot = self._snapshot()
            current_map = str(self._world(snapshot).get("map") or "")
            if current_map and current_map != before_map:
                game_actions.release_hold_move()
                self._log(
                    "portal_crossed_after_entry_probe",
                    source_map=before_map,
                    dest_map=current_map,
                    portal={"x": portal[0], "y": portal[1]},
                )
                return True

            player = self._position(snapshot)
            if player is None:
                self._stop.wait(0.05)
                continue

            now = time.time()
            if now - last_send >= 0.28:
                destination = candidates[attempt % len(candidates)]
                attempt += 1

                if game_actions.native_move_ready():
                    result = game_actions.move_to(destination)
                elif self._native_only():
                    self._set(
                        "ERROR",
                        "Native movement became unavailable while entering portal; "
                        "screen fallback is disabled.",
                    )
                    return False
                else:
                    result = game_actions.update_hold_direction(
                        destination[0] - player[0],
                        destination[1] - player[1],
                        radius_px=self.cursor_radius,
                        min_pixel_change=8,
                    )

                self._log(
                    "portal_entry_probe",
                    attempt=attempt,
                    player={"x": player[0], "y": player[1]},
                    destination={"x": destination[0], "y": destination[1]},
                    result=result,
                )
                last_send = now

            self._stop.wait(0.05)

        game_actions.release_hold_move()
        return False

    def _walk_to_portal(self, leg: dict[str, Any]) -> bool:
        source_map = str(leg["source_map"])
        portal = (int(leg["source_x"]), int(leg["source_y"]))
        initial = self._snapshot()
        start_map = str(self._world(initial).get("map") or "")
        if start_map != source_map:
            return True

        player = self._position(initial)
        if player is None:
            return False

        path = self._route_on_map(source_map, player, portal)
        if not path:
            self._set("ERROR", f"No walkable route to portal {source_map} {portal[0]},{portal[1]}")
            return False

        self._set(
            "WALKING_TO_PORTAL",
            f"Running to portal {source_map} {portal[0]},{portal[1]} → {leg['dest_map']}",
        )

        last_replan = time.time()
        while not self._stop.is_set():
            snapshot = self._snapshot()
            world = self._world(snapshot)
            current_map = str(world.get("map") or "")
            if current_map != source_map:
                game_actions.release_hold_move()
                self._log(
                    "portal_crossed",
                    source_map=source_map,
                    dest_map=current_map,
                )
                return True

            player = self._position(snapshot)
            if player is None:
                self._stop.wait(0.04)
                continue

            # Replan periodically in case server movement deviated around actors.
            if time.time() - last_replan > 1.5:
                fresh = self._route_on_map(source_map, player, portal)
                if fresh:
                    path = fresh
                last_replan = time.time()

            index = self._nearest_index(path, player)
            if index >= len(path) - 1:
                destination = portal
            else:
                lookahead = min(len(path) - 1, index + self.lookahead)
                destination = path[lookahead]

            if game_actions.native_move_ready():
                now = time.time()
                if (
                    self._native_destination != destination
                    or now - self._native_sent_at >= 1.0
                ):
                    result = game_actions.move_to(destination)
                    if not result.get("ok"):
                        self._set(
                            "ERROR",
                            f"Native move to portal failed: {result.get('reason')}",
                        )
                        return False
                    self._native_destination = destination
                    self._native_sent_at = now
                    self._log(
                        "native_portal_move",
                        destination={"x": destination[0], "y": destination[1]},
                        result=result,
                    )
            else:
                if self._native_only():
                    game_actions.release_hold_move()
                    self._set(
                        "ERROR",
                        "Native movement became unavailable on the route; "
                        "screen fallback is disabled.",
                    )
                    return False

                dx = destination[0] - player[0]
                dy = destination[1] - player[1]

                result = game_actions.update_hold_direction(
                    dx,
                    dy,
                    radius_px=self.cursor_radius,
                    min_pixel_change=18,
                )

                if not result.get("ok"):
                    # Keep walking naturally but shorten the lookahead if needed.
                    worked = False
                    for short in (7, 5, 3, 1):
                        idx = min(len(path) - 1, index + short)
                        point = path[idx]
                        result = game_actions.update_hold_direction(
                            point[0] - player[0],
                            point[1] - player[1],
                            radius_px=self.cursor_radius,
                            min_pixel_change=12,
                        )
                        if result.get("ok"):
                            worked = True
                            break
                    if not worked:
                        game_actions.release_hold_move()
                        self._set("ERROR", "Could not steer toward portal.")
                        return False

            # Once close to the portal, stop relying on the ordinary A*
            # endpoint. Some portal coordinates sit one cell beyond the nav
            # endpoint, which previously left the character standing in front
            # of the warp. Actively probe the exact trigger and its immediate
            # neighboring cells until the server confirms a map change.
            if max(abs(player[0] - portal[0]), abs(player[1] - portal[1])) <= 3:
                self._set(
                    "ENTERING_PORTAL",
                    f"Stepping through portal to {leg['dest_map']}",
                )
                if self._force_portal_entry(source_map, portal, timeout=4.0):
                    return True
                self._set(
                    "ERROR",
                    f"Reached portal at {portal[0]},{portal[1]} but the map did not change.",
                )
                return False

            self._stop.wait(0.05)

        game_actions.release_hold_move()
        return False

    def _loop(self):
        try:
            while not self._stop.is_set():
                snapshot = self._snapshot()
                world = self._world(snapshot)
                current_map = str(world.get("map") or "")
                if not current_map:
                    self._set("WAITING", "Waiting for current map.")
                    self._stop.wait(0.1)
                    continue

                if self.mode == "map" and self.destination_map:
                    route = world_route_planner.route_to_map(
                        current_map,
                        self.destination_map,
                    )
                    destination_label = self.destination_map
                else:
                    preferred = str(app_state.get_profile().town.storage_map or "").strip() or None
                    route = world_route_planner.route_to_town(
                        current_map,
                        preferred_town=preferred,
                    )
                    self.destination_town = route.get("town")
                    destination_label = self.destination_town

                if route.get("status") != "ready":
                    self._set("ERROR", str(route.get("message") or "No route."))
                    return

                next_portal = route.get("next_portal")
                if not next_portal:
                    self._set(
                        "ARRIVED",
                        f"Arrived in {destination_label}.",
                    )
                    return

                self.current_leg = next_portal
                self._log("town_leg", **next_portal)
                if next_portal.get("interactive"):
                    if not self._execute_interactive_portal(next_portal):
                        return
                else:
                    if not self._walk_to_portal(next_portal):
                        return

                self._stop.wait(0.08)
        finally:
            game_actions.release_hold_move()
            self.running = False
            if self.state not in {"ARRIVED", "ERROR"}:
                self._set("IDLE", "Town travel stopped.")

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return self.snapshot()
            if not authenticated_client_monitor.snapshot().get("classic_pid"):
                raise RuntimeError("Classic.exe is not detected.")
            self._ensure_action_mode_ready()

            self._stop.clear()
            self.running = True
            self.state = "PLANNING"
            self.message = "Planning fastest physical route to town."
            self.destination_town = None
            self.destination_map = None
            self.mode = "town"
            self.current_leg = None
            self._native_destination = None
            self._native_sent_at = 0.0
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return self.snapshot()

    def start_to_map(self, target_map: str) -> dict[str, Any]:
        target = str(target_map or "").strip().lower()
        if not target:
            raise RuntimeError("Target map is required.")

        with self._lock:
            if self.running:
                return self.snapshot()
            if not authenticated_client_monitor.snapshot().get("classic_pid"):
                raise RuntimeError("Classic.exe is not detected.")
            self._ensure_action_mode_ready()

            self._stop.clear()
            self.running = True
            self.state = "PLANNING"
            self.message = f"Planning route to {target}."
            self.destination_town = None
            self.destination_map = target
            self.mode = "map"
            self.current_leg = None
            self._native_destination = None
            self._native_sent_at = 0.0
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        game_actions.release_hold_move()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self.running = False
        if self.state != "ARRIVED":
            self.state = "IDLE"
            self.message = "Town travel stopped."
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "state": self.state,
                "message": self.message,
                "destination_town": self.destination_town,
                "destination_map": self.destination_map,
                "mode": self.mode,
                "current_leg": self.current_leg,
                "actions": self.actions[-20:],
            }


town_travel_controller = TownTravelController()
