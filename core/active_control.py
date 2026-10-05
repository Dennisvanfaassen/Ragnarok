from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any

from core.pathing import (
    build_pathing_state,
    clear_walk_line,
    nav_repository,
)
from core.state import app_state
from core.targeting import build_targeting_state
from diagnostics.authenticated_client import authenticated_client_monitor


user32 = ctypes.windll.user32

SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class ActiveHuntController:
    """Drive Classic.exe with ordinary mouse input using live network state."""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._status = "stopped"
        self._message = "Idle"
        self._last_click: dict[str, Any] | None = None
        self._actions: list[dict[str, Any]] = []
        self._engaged_target_id: int | None = None
        self._engaged_target_name: str | None = None

        self.tile_width = 40.0
        self.tile_height = 20.0
        self.max_path_lookahead = 5
        self.attack_range_tiles = 1

        # Keep clicks comfortably inside the world viewport.
        self.max_screen_x = 360
        self.max_screen_y = 210

    def configure(self, payload: dict[str, Any]):
        with self._lock:
            self.tile_width = max(
                10.0,
                min(100.0, float(payload.get("tile_width", self.tile_width))),
            )
            self.tile_height = max(
                5.0,
                min(60.0, float(payload.get("tile_height", self.tile_height))),
            )
            self.max_path_lookahead = max(
                1,
                min(10, int(payload.get("lookahead", self.max_path_lookahead))),
            )
            self.attack_range_tiles = max(
                1,
                min(8, int(payload.get("attack_range", self.attack_range_tiles))),
            )

    def _log(self, action: str, **details):
        entry = {"time": time.time(), "action": action, **details}
        with self._lock:
            self._actions.append(entry)
            self._actions = self._actions[-50:]

    def _find_classic_window(self) -> int | None:
        snapshot = authenticated_client_monitor.snapshot()
        pid = snapshot.get("classic_pid")
        if not pid:
            return None

        result = {"hwnd": None}
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
        )

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) != int(pid):
                return True
            rect = RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            if rect.right - rect.left < 300 or rect.bottom - rect.top < 200:
                return True
            result["hwnd"] = int(hwnd)
            return False

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return result["hwnd"]

    def _client_center_screen(self, hwnd: int) -> tuple[int, int] | None:
        rect = RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None

        point = POINT(
            int((rect.right - rect.left) * 0.50),
            int((rect.bottom - rect.top) * 0.46),
        )
        if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def _map_delta_to_screen(self, dx: int, dy: int) -> tuple[int, int]:
        sx = (dx - dy) * (self.tile_width / 2.0)
        sy = -(dx + dy) * (self.tile_height / 2.0)
        return int(round(sx)), int(round(sy))

    def _screen_offset_is_safe(self, dx: int, dy: int) -> bool:
        off_x, off_y = self._map_delta_to_screen(dx, dy)
        return abs(off_x) <= self.max_screen_x and abs(off_y) <= self.max_screen_y

    def _click_screen(self, hwnd: int, x: int, y: int):
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.025)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.035)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _click_map_position(
        self,
        hwnd: int,
        player_x: int,
        player_y: int,
        target_x: int,
        target_y: int,
        *,
        kind: str,
    ) -> bool:
        center = self._client_center_screen(hwnd)
        if not center:
            return False

        dx = target_x - player_x
        dy = target_y - player_y
        off_x, off_y = self._map_delta_to_screen(dx, dy)

        # Never clamp a distant point to the screen edge; that changes the
        # intended map tile. Refuse it and let the caller pick a nearer path cell.
        if abs(off_x) > self.max_screen_x or abs(off_y) > self.max_screen_y:
            return False

        click_x = center[0] + off_x
        click_y = center[1] + off_y
        self._click_screen(hwnd, click_x, click_y)

        self._last_click = {
            "kind": kind,
            "map_from": {"x": player_x, "y": player_y},
            "map_to": {"x": target_x, "y": target_y},
            "screen": {"x": click_x, "y": click_y},
        }
        self._log(kind, **self._last_click)
        return True

    @staticmethod
    def _find_actor(snapshot: dict[str, Any], target_id: int) -> dict[str, Any] | None:
        actors = ((snapshot.get("live_state") or {}).get("actors") or [])
        for actor in actors:
            if int(actor.get("id") or -1) == int(target_id):
                return actor
        return None

    def _wait_for_movement_to_finish(
        self,
        clicked_x: int,
        clicked_y: int,
        target_id: int,
    ):
        """Wait for the current RO walk to settle before another movement click."""
        deadline = time.time() + 7.0
        moved = False
        previous: tuple[int, int] | None = None
        last_change = time.time()

        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(0.10)
            snapshot = authenticated_client_monitor.snapshot()

            # If the target vanished while walking, stop this route immediately.
            if self._find_actor(snapshot, target_id) is None:
                return

            world = (snapshot.get("live_state") or {}).get("world") or {}
            x, y = world.get("x"), world.get("y")
            if x is None or y is None:
                continue

            current = (int(x), int(y))
            if previous is None:
                previous = current
                continue

            if current != previous:
                moved = True
                last_change = time.time()
                previous = current

            if max(abs(current[0] - clicked_x), abs(current[1] - clicked_y)) <= 1:
                return

            # Once movement began, wait until coordinates have been stable for
            # a moment. This prevents queueing another click mid-walk.
            if moved and time.time() - last_change >= 0.55:
                return

    def _furthest_visible_clear_path_point(
        self,
        grid,
        start: tuple[int, int],
        path_preview: list[dict[str, Any]],
    ) -> tuple[int, int] | None:
        if len(path_preview) < 2:
            return None

        # Walk backwards from the target end and pick the furthest path cell
        # that is both directly reachable and actually inside the game viewport.
        for item in reversed(path_preview[1:]):
            point = (int(item["x"]), int(item["y"]))
            dx = point[0] - start[0]
            dy = point[1] - start[1]
            if not self._screen_offset_is_safe(dx, dy):
                continue
            if clear_walk_line(grid, start, point):
                return point

        first = path_preview[1]
        return int(first["x"]), int(first["y"])

    def _wait_for_locked_target_death(self):
        """After one monster click, issue no more clicks until that actor is gone."""
        target_id = self._engaged_target_id
        target_name = self._engaged_target_name or "monster"
        if target_id is None:
            return

        self._status = "engaged"
        self._message = f"Engaged {target_name}; waiting for it to die"
        app_state.patch_runtime(
            current_action="Fighting target",
            message=self._message,
        )

        while not self._stop.is_set():
            snapshot = authenticated_client_monitor.snapshot()
            if self._find_actor(snapshot, target_id) is None:
                self._log(
                    "target_gone",
                    target_id=target_id,
                    target_name=target_name,
                )
                self._engaged_target_id = None
                self._engaged_target_name = None
                self._status = "running"
                self._message = f"{target_name} defeated; selecting next target"
                app_state.patch_runtime(
                    current_action="Target defeated",
                    message=self._message,
                    target=None,
                )
                self._stop.wait(0.25)
                return

            # No mouse input while the target remains present.
            self._stop.wait(0.15)

    def _loop(self):
        self._status = "running"
        self._message = "Searching for target"

        while not self._stop.is_set():
            if self._engaged_target_id is not None:
                self._wait_for_locked_target_death()
                continue

            snapshot = authenticated_client_monitor.snapshot()
            profile = app_state.get_profile()
            world = (snapshot.get("live_state") or {}).get("world") or {}

            if not snapshot.get("classic_pid"):
                self._message = "Waiting for Classic.exe"
                self._stop.wait(0.5)
                continue

            px, py = world.get("x"), world.get("y")
            map_name = world.get("map")
            if px is None or py is None or not map_name:
                self._message = "Waiting for live map/coordinates"
                self._stop.wait(0.4)
                continue

            targeting = build_targeting_state(snapshot, profile.hunt.monsters)
            target = targeting.get("selected")
            if not target:
                self._message = "No valid monster target"
                app_state.patch_runtime(
                    current_action="Searching for target",
                    target=None,
                )
                self._stop.wait(0.4)
                continue

            target_id = int(target["id"])
            tx, ty = int(target["x"]), int(target["y"])
            start = (int(px), int(py))
            target_point = (tx, ty)

            app_state.patch_runtime(
                target=f"{target.get('name')} @ {tx},{ty}",
            )

            hwnd = self._find_classic_window()
            if not hwnd:
                self._message = "Classic.exe window not found"
                self._stop.wait(0.5)
                continue

            try:
                grid, _source = nav_repository.load(str(map_name))
            except Exception as exc:
                self._status = "path_error"
                self._message = str(exc)
                self._stop.wait(0.5)
                continue

            # Preferred behavior: if the monster is visible and there is a clear
            # straight walk line, click the monster itself exactly once. RO then
            # performs its normal walk-to-target + auto attack behavior.
            dx = tx - start[0]
            dy = ty - start[1]
            if (
                self._screen_offset_is_safe(dx, dy)
                and clear_walk_line(grid, start, target_point)
            ):
                self._status = "attacking"
                self._message = f"Clicking {target.get('name')} once"
                app_state.patch_runtime(
                    current_action="Engaging target",
                    message=self._message,
                )

                if self._click_map_position(
                    hwnd,
                    start[0],
                    start[1],
                    tx,
                    ty,
                    kind="attack_click",
                ):
                    self._engaged_target_id = target_id
                    self._engaged_target_name = str(target.get("name") or "monster")
                    self._wait_for_locked_target_death()
                    continue

            # Direct path is blocked (or target is outside our safe click area).
            # Use A* only to get around the obstacle, then click as far along the
            # currently clear corridor as possible with one movement click.
            pathing = build_pathing_state(snapshot, targeting, nav_repository)
            path_preview = pathing.get("path_preview") or []
            if not pathing.get("path_found") or len(path_preview) < 2:
                self._status = "path_error"
                self._message = pathing.get("message") or "No route to target"
                app_state.patch_runtime(
                    current_action="Path blocked",
                    message=self._message,
                )
                self._stop.wait(0.5)
                continue

            move_point = self._furthest_visible_clear_path_point(
                grid,
                start,
                path_preview,
            )
            if move_point is None:
                self._status = "path_error"
                self._message = "No visible clear movement point found"
                self._stop.wait(0.5)
                continue

            mx, my = move_point
            self._status = "walking"
            self._message = (
                f"Path blocked; moving toward {target.get('name')} via {mx},{my}"
            )
            app_state.patch_runtime(
                current_action="Walking around obstacle",
                message=self._message,
            )

            if self._click_map_position(
                hwnd,
                start[0],
                start[1],
                mx,
                my,
                kind="move_click",
            ):
                self._wait_for_movement_to_finish(mx, my, target_id)
            else:
                self._message = "Movement point was outside safe viewport"

            self._stop.wait(0.15)

        self._engaged_target_id = None
        self._engaged_target_name = None
        self._running = False
        self._status = "stopped"
        self._message = "Active hunt stopped"
        app_state.patch_runtime(
            current_action="Idle",
            message="Active hunt stopped",
            target=None,
        )

    def start(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload:
            self.configure(payload)

        with self._lock:
            if self._running:
                return self.snapshot()
            if not authenticated_client_monitor.snapshot().get("classic_pid"):
                raise RuntimeError(
                    "Classic.exe is not detected. Launch SoulBound and enter the game first."
                )
            self._stop.clear()
            self._engaged_target_id = None
            self._engaged_target_name = None
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
        self._running = False
        self._engaged_target_id = None
        self._engaged_target_name = None
        self._status = "stopped"
        self._message = "Active hunt stopped"
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "status": self._status,
                "message": self._message,
                "engaged_target": (
                    {
                        "id": self._engaged_target_id,
                        "name": self._engaged_target_name,
                    }
                    if self._engaged_target_id is not None
                    else None
                ),
                "settings": {
                    "tile_width": self.tile_width,
                    "tile_height": self.tile_height,
                    "lookahead": self.max_path_lookahead,
                    "attack_range": self.attack_range_tiles,
                },
                "last_click": self._last_click,
                "actions": self._actions[-20:],
                "control_mode": (
                    "Windows mouse input to Classic.exe; authenticated network "
                    "session remains read-only."
                ),
            }


active_hunt_controller = ActiveHuntController()
