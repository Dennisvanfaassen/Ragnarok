from __future__ import annotations

import ctypes
import json
import math
import threading
import time
from ctypes import wintypes
from pathlib import Path
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
        self.direct_click_range = 4
        self.combat_confirm_timeout = 1.8
        self.monster_sprite_y_offset = -24
        self.steer_interval = 0.15
        self.steer_lookahead_tiles = 4
        self.steer_hysteresis_px = 12

        self._calibration_path = Path(__file__).resolve().parents[1] / "screen_calibration.json"
        self._calibration: dict[str, Any] | None = None
        self._calibration_status = "not_calibrated"
        self._calibration_message = "Run screen calibration before active hunting."
        self._calibration_thread: threading.Thread | None = None
        self._load_calibration()

    def _load_calibration(self):
        try:
            data = json.loads(self._calibration_path.read_text(encoding="utf-8"))
            coeffs = data.get("coefficients")
            if (
                isinstance(coeffs, dict)
                and len(coeffs.get("screen_x", [])) == 3
                and len(coeffs.get("screen_y", [])) == 3
            ):
                self._calibration = data
                self._calibration_status = "ready"
                self._calibration_message = (
                    f"Calibration loaded (RMSE {data.get('rmse_px', '?')} px)."
                )
        except Exception:
            self._calibration = None

    def _save_calibration(self):
        if self._calibration:
            self._calibration_path.write_text(
                json.dumps(self._calibration, indent=2),
                encoding="utf-8",
            )

    @staticmethod
    def _solve_3x3(matrix: list[list[float]], vector: list[float]) -> list[float]:
        a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
        for col in range(3):
            pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
            if abs(a[pivot][col]) < 1e-9:
                raise ValueError("Calibration samples are not geometrically independent.")
            a[col], a[pivot] = a[pivot], a[col]
            div = a[col][col]
            a[col] = [v / div for v in a[col]]
            for row in range(3):
                if row == col:
                    continue
                factor = a[row][col]
                a[row] = [
                    a[row][c] - factor * a[col][c]
                    for c in range(4)
                ]
        return [a[i][3] for i in range(3)]

    @classmethod
    def _least_squares_affine(
        cls,
        samples: list[dict[str, float]],
        key: str,
    ) -> list[float]:
        # Fit value = c0 + c1*dx + c2*dy using normal equations.
        ata = [[0.0] * 3 for _ in range(3)]
        atb = [0.0] * 3
        for sample in samples:
            row = [1.0, sample["dx"], sample["dy"]]
            value = sample[key]
            for i in range(3):
                atb[i] += row[i] * value
                for j in range(3):
                    ata[i][j] += row[i] * row[j]
        return cls._solve_3x3(ata, atb)

    def _client_geometry(self, hwnd: int) -> dict[str, int] | None:
        rect = RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        origin = POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        return {
            "left": int(origin.x),
            "top": int(origin.y),
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
        }

    def _calibration_valid_for_window(self, hwnd: int) -> bool:
        if not self._calibration:
            return False
        geometry = self._client_geometry(hwnd)
        if not geometry:
            return False
        saved = self._calibration.get("window") or {}
        return (
            abs(int(saved.get("width", -9999)) - geometry["width"]) <= 2
            and abs(int(saved.get("height", -9999)) - geometry["height"]) <= 2
        )

    def _project_map_delta(
        self,
        hwnd: int,
        dx: int,
        dy: int,
    ) -> tuple[int, int] | None:
        geometry = self._client_geometry(hwnd)
        if not geometry:
            return None

        if self._calibration_valid_for_window(hwnd):
            coeffs = self._calibration["coefficients"]
            cx = coeffs["screen_x"]
            cy = coeffs["screen_y"]
            local_x = cx[0] + cx[1] * dx + cx[2] * dy
            local_y = cy[0] + cy[1] * dx + cy[2] * dy
            return (
                geometry["left"] + int(round(local_x)),
                geometry["top"] + int(round(local_y)),
            )

        return None

    def _wait_for_position_settle(
        self,
        start: tuple[int, int],
        timeout: float = 5.0,
    ) -> tuple[int, int]:
        deadline = time.time() + timeout
        previous = start
        last_change = time.time()
        moved = False
        while time.time() < deadline:
            time.sleep(0.10)
            snapshot = authenticated_client_monitor.snapshot()
            world = (snapshot.get("live_state") or {}).get("world") or {}
            x, y = world.get("x"), world.get("y")
            if x is None or y is None:
                continue
            current = (int(x), int(y))
            if current != previous:
                moved = True
                previous = current
                last_change = time.time()
            if moved and time.time() - last_change >= 0.65:
                return current
        return previous

    def _run_calibration(self):
        self._calibration_status = "running"
        self._calibration_message = "Calibrating screen/map transform..."
        try:
            hwnd = self._find_classic_window()
            if not hwnd:
                raise RuntimeError("Classic.exe window not found.")

            geometry = self._client_geometry(hwnd)
            if not geometry:
                raise RuntimeError("Could not read Classic.exe client geometry.")

            # Deliberately click screen-space directions; network X/Y tells us
            # which map displacement each pixel direction actually represents.
            cx = int(geometry["width"] * 0.50)
            cy = int(geometry["height"] * 0.46)
            offsets = [
                (120, 0),
                (-120, 0),
                (0, 90),
                (0, -90),
                (95, 65),
                (-95, -65),
            ]
            samples: list[dict[str, float]] = []

            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.4)

            for ox, oy in offsets:
                snapshot = authenticated_client_monitor.snapshot()
                world = (snapshot.get("live_state") or {}).get("world") or {}
                sx, sy = world.get("x"), world.get("y")
                if sx is None or sy is None:
                    continue

                start = (int(sx), int(sy))
                local_x = cx + ox
                local_y = cy + oy
                screen_x = geometry["left"] + local_x
                screen_y = geometry["top"] + local_y

                self._click_screen(hwnd, screen_x, screen_y)
                end = self._wait_for_position_settle(start)
                dx = end[0] - start[0]
                dy = end[1] - start[1]

                if dx == 0 and dy == 0:
                    continue

                samples.append({
                    "dx": float(dx),
                    "dy": float(dy),
                    "screen_x": float(local_x),
                    "screen_y": float(local_y),
                })
                time.sleep(0.25)

            if len(samples) < 3:
                raise RuntimeError(
                    "Not enough movement samples. Stand in a large open area and calibrate again."
                )

            coeff_x = self._least_squares_affine(samples, "screen_x")
            coeff_y = self._least_squares_affine(samples, "screen_y")

            squared = 0.0
            for sample in samples:
                px = coeff_x[0] + coeff_x[1] * sample["dx"] + coeff_x[2] * sample["dy"]
                py = coeff_y[0] + coeff_y[1] * sample["dx"] + coeff_y[2] * sample["dy"]
                squared += (px - sample["screen_x"]) ** 2 + (py - sample["screen_y"]) ** 2
            rmse = math.sqrt(squared / max(1, len(samples)))

            if rmse > 45:
                raise RuntimeError(
                    f"Calibration was inconsistent (RMSE {rmse:.1f}px). "
                    "Use a more open area and keep the game window unchanged."
                )

            self._calibration = {
                "version": 1,
                "window": {
                    "width": geometry["width"],
                    "height": geometry["height"],
                },
                "coefficients": {
                    "screen_x": [round(v, 6) for v in coeff_x],
                    "screen_y": [round(v, 6) for v in coeff_y],
                },
                "rmse_px": round(rmse, 2),
                "samples": samples,
                "created_at": time.time(),
            }
            self._save_calibration()
            self._calibration_status = "ready"
            self._calibration_message = (
                f"Calibration ready: {len(samples)} samples, RMSE {rmse:.1f}px."
            )
            self._log(
                "calibration_complete",
                samples=len(samples),
                rmse_px=round(rmse, 2),
            )
        except Exception as exc:
            self._calibration_status = "error"
            self._calibration_message = str(exc)
            self._log("calibration_error", message=str(exc))

    def start_calibration(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                raise RuntimeError("Stop active hunt before calibrating.")
            if self._calibration_status == "running":
                return self.calibration_snapshot()
            if not authenticated_client_monitor.snapshot().get("classic_pid"):
                raise RuntimeError("Classic.exe is not detected.")
            self._calibration_thread = threading.Thread(
                target=self._run_calibration,
                daemon=True,
            )
            self._calibration_thread.start()
            return self.calibration_snapshot()

    def clear_calibration(self) -> dict[str, Any]:
        with self._lock:
            self._calibration = None
            self._calibration_status = "not_calibrated"
            self._calibration_message = "Calibration cleared."
            try:
                self._calibration_path.unlink(missing_ok=True)
            except Exception:
                pass
            return self.calibration_snapshot()

    def calibration_snapshot(self) -> dict[str, Any]:
        calibration = self._calibration or {}
        return {
            "status": self._calibration_status,
            "message": self._calibration_message,
            "ready": self._calibration_status == "ready",
            "window": calibration.get("window"),
            "rmse_px": calibration.get("rmse_px"),
            "sample_count": len(calibration.get("samples") or []),
            "coefficients": calibration.get("coefficients"),
        }

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
            self.monster_sprite_y_offset = max(
                -80,
                min(30, int(payload.get("sprite_y_offset", self.monster_sprite_y_offset))),
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

    def _screen_offset_is_safe(self, dx: int, dy: int, hwnd: int | None = None) -> bool:
        if hwnd is not None and self._calibration_valid_for_window(hwnd):
            geometry = self._client_geometry(hwnd)
            point = self._project_map_delta(hwnd, dx, dy)
            if geometry and point:
                local_x = point[0] - geometry["left"]
                local_y = point[1] - geometry["top"]
                margin_x = 70
                margin_y = 55
                return (
                    margin_x <= local_x <= geometry["width"] - margin_x
                    and margin_y <= local_y <= geometry["height"] - margin_y
                )
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

    def _mouse_down_screen(self, hwnd: int, x: int, y: int):
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.02)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

    def _mouse_up(self):
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
        dx = target_x - player_x
        dy = target_y - player_y
        point = self._project_map_delta(hwnd, dx, dy)
        if point is None:
            return False
        if not self._screen_offset_is_safe(dx, dy, hwnd):
            return False

        click_x, click_y = point
        if kind == "attack_click":
            click_y += self.monster_sprite_y_offset
        self._click_screen(hwnd, click_x, click_y)

        self._last_click = {
            "kind": kind,
            "map_from": {"x": player_x, "y": player_y},
            "map_to": {"x": target_x, "y": target_y},
            "screen": {"x": click_x, "y": click_y},
        }
        self._log(kind, **self._last_click)
        return True

    def _screen_point_for_map(
        self,
        hwnd: int,
        player_x: int,
        player_y: int,
        target_x: int,
        target_y: int,
    ) -> tuple[int, int] | None:
        dx = target_x - player_x
        dy = target_y - player_y
        point = self._project_map_delta(hwnd, dx, dy)
        if point is None or not self._screen_offset_is_safe(dx, dy, hwnd):
            return None
        return point

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
        # Stop a few tiles before the target. A movement click should not
        # accidentally land on the monster; the next cycle performs the attack.
        usable = path_preview[1:-2] if len(path_preview) > 4 else path_preview[1:2]
        for item in reversed(usable):
            point = (int(item["x"]), int(item["y"]))
            dx = point[0] - start[0]
            dy = point[1] - start[1]
            if not self._screen_offset_is_safe(dx, dy):
                continue
            if clear_walk_line(grid, start, point):
                return point

        first = path_preview[1]
        return int(first["x"]), int(first["y"])

    def _hold_navigate_to_target(
        self,
        hwnd: int,
        target_id: int,
        target_name: str,
    ):
        """Hold left mouse down and steer along the current clear A* corridor."""
        mouse_is_down = False
        last_screen_point: tuple[int, int] | None = None
        try:
            while not self._stop.is_set():
                snapshot = authenticated_client_monitor.snapshot()
                actor = self._find_actor(snapshot, target_id)
                if actor is None:
                    return

                world = (snapshot.get("live_state") or {}).get("world") or {}
                px, py = world.get("x"), world.get("y")
                map_name = world.get("map")
                tx, ty = actor.get("x"), actor.get("y")
                if None in (px, py, tx, ty) or not map_name:
                    self._stop.wait(self.steer_interval)
                    continue

                start = (int(px), int(py))
                target_point = (int(tx), int(ty))
                dx = target_point[0] - start[0]
                dy = target_point[1] - start[1]
                distance = max(abs(dx), abs(dy))

                try:
                    grid, _ = nav_repository.load(str(map_name))
                except Exception:
                    return

                # Close enough for the dedicated sprite click: stop walking.
                if (
                    distance <= self.direct_click_range
                    and clear_walk_line(grid, start, target_point)
                    and self._screen_offset_is_safe(dx, dy, hwnd)
                ):
                    return

                profile = app_state.get_profile()
                targeting = {
                    "selected": {
                        "id": target_id,
                        "name": target_name,
                        "x": target_point[0],
                        "y": target_point[1],
                    }
                }
                pathing = build_pathing_state(snapshot, targeting, nav_repository)
                path_preview = pathing.get("path_preview") or []
                if not pathing.get("path_found") or len(path_preview) < 2:
                    return

                # Stable short lookahead prevents A* from swinging the mouse
                # between distant waypoints on every network update.
                usable_end = max(1, len(path_preview) - 3)
                index = min(self.steer_lookahead_tiles, usable_end)
                item = path_preview[index]
                move_point = (int(item["x"]), int(item["y"]))
                if not clear_walk_line(grid, start, move_point):
                    move_point = self._furthest_visible_clear_path_point(
                        grid, start, path_preview[: index + 1]
                    )
                if move_point is None:
                    return

                screen_point = self._screen_point_for_map(
                    hwnd,
                    start[0],
                    start[1],
                    move_point[0],
                    move_point[1],
                )
                if screen_point is None:
                    self._stop.wait(self.steer_interval)
                    continue

                if not mouse_is_down:
                    self._mouse_down_screen(hwnd, screen_point[0], screen_point[1])
                    mouse_is_down = True
                    self._log(
                        "move_hold_start",
                        target_id=target_id,
                        target_name=target_name,
                        map_from={"x": start[0], "y": start[1]},
                        map_to={"x": move_point[0], "y": move_point[1]},
                    )
                else:
                    if (
                        last_screen_point is None
                        or abs(screen_point[0] - last_screen_point[0]) >= self.steer_hysteresis_px
                        or abs(screen_point[1] - last_screen_point[1]) >= self.steer_hysteresis_px
                    ):
                        user32.SetCursorPos(int(screen_point[0]), int(screen_point[1]))
                last_screen_point = screen_point

                self._status = "walking"
                self._message = (
                    f"Holding movement toward {target_name} via "
                    f"{move_point[0]},{move_point[1]}"
                )
                app_state.patch_runtime(
                    current_action="Navigating to target",
                    message=self._message,
                )

                self._stop.wait(self.steer_interval)
        finally:
            if mouse_is_down:
                self._mouse_up()
                self._log(
                    "move_hold_end",
                    target_id=target_id,
                    target_name=target_name,
                )

    def _combat_confirmed(self, target_id: int, since: float) -> bool:
        snapshot = authenticated_client_monitor.snapshot()
        world = (snapshot.get("live_state") or {}).get("world") or {}
        combat = world.get("last_combat") or {}
        try:
            return (
                int(combat.get("target_id")) == int(target_id)
                and float(combat.get("timestamp") or 0) >= since
            )
        except Exception:
            return False

    def _wait_for_combat_confirmation(self, target_id: int, clicked_at: float) -> bool:
        deadline = time.time() + self.combat_confirm_timeout
        while not self._stop.is_set() and time.time() < deadline:
            if self._combat_confirmed(target_id, clicked_at):
                return True
            snapshot = authenticated_client_monitor.snapshot()
            if self._find_actor(snapshot, target_id) is None:
                return True
            self._stop.wait(0.08)
        return False

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
            direct_distance = max(abs(dx), abs(dy))
            if (
                direct_distance <= self.direct_click_range
                and self._screen_offset_is_safe(dx, dy)
                and clear_walk_line(grid, start, target_point)
            ):
                self._status = "attacking"
                self._message = f"Clicking {target.get('name')} once"
                app_state.patch_runtime(
                    current_action="Engaging target",
                    message=self._message,
                )

                clicked_at = time.time()
                if self._click_map_position(
                    hwnd,
                    start[0],
                    start[1],
                    tx,
                    ty,
                    kind="attack_click",
                ):
                    if self._wait_for_combat_confirmation(target_id, clicked_at):
                        self._engaged_target_id = target_id
                        self._engaged_target_name = str(target.get("name") or "monster")
                        self._log(
                            "combat_confirmed",
                            target_id=target_id,
                            target_name=self._engaged_target_name,
                        )
                        self._wait_for_locked_target_death()
                        continue
                    else:
                        self._log(
                            "attack_missed",
                            target_id=target_id,
                            target_name=str(target.get("name") or "monster"),
                        )
                        self._status = "running"
                        self._message = "Monster click was not confirmed; repositioning"

            # Not yet in reliable sprite-click range. Hold the left mouse
            # button and steer continuously along the currently clear path.
            self._hold_navigate_to_target(
                hwnd,
                target_id,
                str(target.get("name") or "monster"),
            )
            self._stop.wait(0.08)

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
            hwnd = self._find_classic_window()
            if not hwnd or not self._calibration_valid_for_window(hwnd):
                raise RuntimeError(
                    "Screen calibration is required for this Classic.exe window size. "
                    "Stand in an open area and click Calibrate screen first."
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
        try:
            self._mouse_up()
        except Exception:
            pass
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
                    "direct_click_range": self.direct_click_range,
                    "combat_confirm_timeout": self.combat_confirm_timeout,
                    "sprite_y_offset": self.monster_sprite_y_offset,
                    "movement_mode": "hold_and_steer",
                },
                "last_click": self._last_click,
                "actions": self._actions[-20:],
                "calibration": self.calibration_snapshot(),
                "control_mode": (
                    "Windows mouse input to Classic.exe; authenticated network "
                    "session remains read-only."
                ),
            }


active_hunt_controller = ActiveHuntController()
