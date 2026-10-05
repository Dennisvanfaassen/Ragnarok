from __future__ import annotations

import threading
import time
from typing import Any

from core.game_actions import game_actions
from core.pathing import astar, nav_repository
from core.state import app_state
from core.world_route import world_route_planner
from diagnostics.authenticated_client import authenticated_client_monitor


class TownTravelController:
    """Smooth held-mouse travel through physical portals toward town."""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.state = "IDLE"
        self.message = "Idle"
        self.destination_town: str | None = None
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

            # When on/near the trigger, keep holding toward it and wait for the
            # server map change instead of clicking repeatedly.
            if max(abs(player[0] - portal[0]), abs(player[1] - portal[1])) <= 2:
                self._set(
                    "ENTERING_PORTAL",
                    f"Entering portal to {leg['dest_map']}",
                )

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

                preferred = str(app_state.get_profile().town.storage_map or "").strip() or None
                route = world_route_planner.route_to_town(
                    current_map,
                    preferred_town=preferred,
                )

                if route.get("status") != "ready":
                    self._set("ERROR", str(route.get("message") or "No town route."))
                    return

                self.destination_town = route.get("town")
                next_portal = route.get("next_portal")
                if not next_portal:
                    self._set(
                        "ARRIVED",
                        f"Arrived in {self.destination_town}.",
                    )
                    return

                if next_portal.get("interactive"):
                    self._set(
                        "ERROR",
                        "Fastest known route requires an NPC/dialog warp. "
                        "Physical portal routing is ready; NPC travel is not automated yet.",
                    )
                    return

                self.current_leg = next_portal
                self._log("town_leg", **next_portal)
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
            if not game_actions.calibration_valid():
                raise RuntimeError("Valid screen calibration is required.")

            self._stop.clear()
            self.running = True
            self.state = "PLANNING"
            self.message = "Planning fastest physical route to town."
            self.destination_town = None
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
                "current_leg": self.current_leg,
                "actions": self.actions[-20:],
            }


town_travel_controller = TownTravelController()
