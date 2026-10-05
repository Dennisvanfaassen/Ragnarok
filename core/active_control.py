from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes
from typing import Any

from core.pathing import build_pathing_state, nav_repository
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
    """Drive the authenticated Classic.exe with ordinary mouse input.

    Network traffic remains read-only. Exact map/actor state comes from the
    authenticated observer; this controller only sends normal Windows clicks.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._status = "stopped"
        self._message = "Idle"
        self._last_click: dict[str, Any] | None = None
        self._actions: list[dict[str, Any]] = []

        # Default Ragnarok isometric projection at normal zoom. These values
        # are intentionally configurable from the dashboard.
        self.tile_width = 40.0
        self.tile_height = 20.0
        self.max_path_lookahead = 5
        self.attack_range_tiles = 1
        self.move_interval = 0.30
        self.attack_interval = 0.55

    def configure(self, payload: dict[str, Any]):
        with self._lock:
            self.tile_width = max(10.0, min(100.0, float(payload.get("tile_width", self.tile_width))))
            self.tile_height = max(5.0, min(60.0, float(payload.get("tile_height", self.tile_height))))
            self.max_path_lookahead = max(1, min(10, int(payload.get("lookahead", self.max_path_lookahead))))
            self.attack_range_tiles = max(1, min(8, int(payload.get("attack_range", self.attack_range_tiles))))

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
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

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

        # RO's world viewport occupies almost the whole client. Slightly above
        # geometric center avoids bottom UI bars on classic skins.
        point = POINT(
            int((rect.right - rect.left) * 0.50),
            int((rect.bottom - rect.top) * 0.46),
        )
        if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def _map_delta_to_screen(self, dx: int, dy: int) -> tuple[int, int]:
        # Ragnarok map Y increases north/up while screen Y increases downward.
        sx = (dx - dy) * (self.tile_width / 2.0)
        sy = -(dx + dy) * (self.tile_height / 2.0)
        return int(round(sx)), int(round(sy))

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

        # Avoid clicks far outside the visible field if the path planner ever
        # feeds us an unexpectedly distant waypoint.
        off_x = max(-420, min(420, off_x))
        off_y = max(-260, min(260, off_y))

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
    def _target_still_present(snapshot: dict[str, Any], target_id: int) -> bool:
        actors = ((snapshot.get("live_state") or {}).get("actors") or [])
        return any(int(a.get("id") or -1) == int(target_id) for a in actors)

    def _loop(self):
        self._status = "running"
        self._message = "Searching for target"

        while not self._stop.is_set():
            snapshot = authenticated_client_monitor.snapshot()
            profile = app_state.get_profile()
            world = (snapshot.get("live_state") or {}).get("world") or {}

            if not snapshot.get("classic_pid"):
                self._message = "Waiting for Classic.exe"
                self._stop.wait(0.5)
                continue

            px, py = world.get("x"), world.get("y")
            if px is None or py is None:
                self._message = "Waiting for live coordinates"
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
            distance_tiles = max(abs(tx - int(px)), abs(ty - int(py)))

            app_state.patch_runtime(
                target=f"{target.get('name')} @ {tx},{ty}",
            )

            hwnd = self._find_classic_window()
            if not hwnd:
                self._message = "Classic.exe window not found"
                self._stop.wait(0.5)
                continue

            if distance_tiles <= self.attack_range_tiles:
                self._status = "attacking"
                self._message = f"Attacking {target.get('name')}"
                app_state.patch_runtime(
                    current_action="Attacking",
                    message=self._message,
                )
                self._click_map_position(
                    hwnd,
                    int(px),
                    int(py),
                    tx,
                    ty,
                    kind="attack_click",
                )
                self._stop.wait(self.attack_interval)
                continue

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

            index = min(self.max_path_lookahead, len(path_preview) - 1)
            step = path_preview[index]
            sx, sy = int(step["x"]), int(step["y"])

            self._status = "walking"
            self._message = (
                f"Walking to {target.get('name')} via {sx},{sy}"
            )
            app_state.patch_runtime(
                current_action="Walking to target",
                message=self._message,
            )
            self._click_map_position(
                hwnd,
                int(px),
                int(py),
                sx,
                sy,
                kind="move_click",
            )

            # Wait until network state confirms movement before issuing the
            # next route click, otherwise clicks can queue faster than RO walks.
            start_pos = (int(px), int(py))
            deadline = time.time() + 2.5
            while not self._stop.is_set() and time.time() < deadline:
                self._stop.wait(0.10)
                fresh = authenticated_client_monitor.snapshot()
                fresh_world = (fresh.get("live_state") or {}).get("world") or {}
                new_pos = (fresh_world.get("x"), fresh_world.get("y"))
                if new_pos[0] is not None and new_pos[1] is not None:
                    if (int(new_pos[0]), int(new_pos[1])) != start_pos:
                        break
                if not self._target_still_present(fresh, target_id):
                    break

            self._stop.wait(self.move_interval)

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
        self._status = "stopped"
        self._message = "Active hunt stopped"
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "status": self._status,
                "message": self._message,
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
