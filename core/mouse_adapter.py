from __future__ import annotations

import ctypes
import json
import math
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.hunt_recorder import hunting_diagnostic_recorder


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


class MouseGameAdapter:
    """Dumb Classic.exe input adapter.

    The hunting AI decides WHAT to do in map coordinates. This class only
    translates one MOVE or ATTACK request into ordinary Windows mouse input.
    """

    def __init__(self):
        self.sprite_y_offset = -24
        self.learned_attack_y_offset: int | None = None
        self.learned_attack_y_offsets: dict[str, int] = {}
        self._calibration_path = (
            Path(__file__).resolve().parents[1] / "screen_calibration.json"
        )
        self._calibration: dict[str, Any] | None = None
        self._calibration_status = "not_calibrated"
        self._calibration_message = "Run screen calibration."
        self._calibration_thread: threading.Thread | None = None
        self._hold_active = False
        self._hold_hwnd: int | None = None
        self._hold_point: tuple[int, int] | None = None
        self._hold_desired_point: tuple[int, int] | None = None
        self._hold_last_update = 0.0
        self._load_calibration()

    def configure(self, *, sprite_y_offset: int | None = None):
        if sprite_y_offset is not None:
            self.sprite_y_offset = max(-80, min(30, int(sprite_y_offset)))

    def _find_window(self) -> int | None:
        snapshot = authenticated_client_monitor.snapshot()
        pid = snapshot.get("classic_pid")
        if not pid:
            return None

        found = {"hwnd": None}
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
        )

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if int(process_id.value) != int(pid):
                return True

            rect = RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            if rect.right - rect.left < 300 or rect.bottom - rect.top < 200:
                return True

            found["hwnd"] = int(hwnd)
            return False

        user32.EnumWindows(enum_proc(callback), 0)
        return found["hwnd"]

    def _geometry(self, hwnd: int) -> dict[str, int] | None:
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

    def _load_calibration(self):
        try:
            data = json.loads(self._calibration_path.read_text(encoding="utf-8"))
            coeffs = data.get("coefficients") or {}
            if (
                len(coeffs.get("screen_x", [])) == 3
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
                raise ValueError("Calibration samples are not independent.")
            a[col], a[pivot] = a[pivot], a[col]
            divisor = a[col][col]
            a[col] = [v / divisor for v in a[col]]
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
    def _fit_affine(
        cls, samples: list[dict[str, float]], key: str
    ) -> list[float]:
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

    def calibration_valid(self) -> bool:
        hwnd = self._find_window()
        if not hwnd or not self._calibration:
            return False
        geometry = self._geometry(hwnd)
        if not geometry:
            return False
        saved = self._calibration.get("window") or {}
        return (
            abs(int(saved.get("width", -1)) - geometry["width"]) <= 2
            and abs(int(saved.get("height", -1)) - geometry["height"]) <= 2
        )

    def calibration_snapshot(self) -> dict[str, Any]:
        calibration = self._calibration or {}
        return {
            "status": self._calibration_status,
            "message": self._calibration_message,
            "ready": self.calibration_valid(),
            "window": calibration.get("window"),
            "rmse_px": calibration.get("rmse_px"),
            "sample_count": len(calibration.get("samples") or []),
            "coefficients": calibration.get("coefficients"),
            "attack_precision": self.precision_snapshot(),
        }

    def clear_calibration(self) -> dict[str, Any]:
        self._calibration = None
        self._calibration_status = "not_calibrated"
        self._calibration_message = "Calibration cleared."
        try:
            self._calibration_path.unlink(missing_ok=True)
        except Exception:
            pass
        return self.calibration_snapshot()

    def _click_screen(self, hwnd: int, x: int, y: int):
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.025)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.035)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def _position(self) -> tuple[int, int] | None:
        snapshot = authenticated_client_monitor.snapshot()
        world = (snapshot.get("live_state") or {}).get("world") or {}
        x, y = world.get("x"), world.get("y")
        if x is None or y is None:
            return None
        return int(x), int(y)

    def _wait_position_settle(
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
            current = self._position()
            if current is None:
                continue
            if current != previous:
                moved = True
                previous = current
                last_change = time.time()
            if moved and time.time() - last_change >= 0.65:
                return current

        return previous

    def _run_calibration(self):
        self._calibration_status = "running"
        self._calibration_message = "Calibrating..."
        try:
            hwnd = self._find_window()
            if not hwnd:
                raise RuntimeError("Classic.exe window not found.")
            geometry = self._geometry(hwnd)
            if not geometry:
                raise RuntimeError("Could not read client geometry.")

            center_x = int(geometry["width"] * 0.50)
            center_y = int(geometry["height"] * 0.46)
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
                start = self._position()
                if start is None:
                    continue

                local_x = center_x + ox
                local_y = center_y + oy
                self._click_screen(
                    hwnd,
                    geometry["left"] + local_x,
                    geometry["top"] + local_y,
                )
                end = self._wait_position_settle(start)
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
                    "Not enough movement samples. Calibrate in a large open area."
                )

            coeff_x = self._fit_affine(samples, "screen_x")
            coeff_y = self._fit_affine(samples, "screen_y")

            squared = 0.0
            for sample in samples:
                px = (
                    coeff_x[0]
                    + coeff_x[1] * sample["dx"]
                    + coeff_x[2] * sample["dy"]
                )
                py = (
                    coeff_y[0]
                    + coeff_y[1] * sample["dx"]
                    + coeff_y[2] * sample["dy"]
                )
                squared += (
                    (px - sample["screen_x"]) ** 2
                    + (py - sample["screen_y"]) ** 2
                )

            rmse = math.sqrt(squared / max(1, len(samples)))
            if rmse > 45:
                raise RuntimeError(
                    f"Calibration inconsistent (RMSE {rmse:.1f}px)."
                )

            self._calibration = {
                "version": 2,
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
                f"Ready: {len(samples)} samples, RMSE {rmse:.1f}px."
            )
        except Exception as exc:
            self._calibration_status = "error"
            self._calibration_message = str(exc)

    def start_calibration(self) -> dict[str, Any]:
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

    def _project(
        self,
        hwnd: int,
        player: tuple[int, int],
        destination: tuple[int, int],
        *,
        sprite: bool = False,
        sprite_y_offset: int | None = None,
    ) -> tuple[int, int] | None:
        if not self.calibration_valid() or not self._calibration:
            return None

        geometry = self._geometry(hwnd)
        if not geometry:
            return None

        dx = destination[0] - player[0]
        dy = destination[1] - player[1]
        coeffs = self._calibration["coefficients"]
        cx = coeffs["screen_x"]
        cy = coeffs["screen_y"]

        local_x = cx[0] + cx[1] * dx + cx[2] * dy
        local_y = cy[0] + cy[1] * dx + cy[2] * dy

        margin_x = 70
        margin_y = 55
        if not (
            margin_x <= local_x <= geometry["width"] - margin_x
            and margin_y <= local_y <= geometry["height"] - margin_y
        ):
            return None

        if sprite:
            local_y += (
                self.sprite_y_offset
                if sprite_y_offset is None
                else int(sprite_y_offset)
            )

        return (
            geometry["left"] + int(round(local_x)),
            geometry["top"] + int(round(local_y)),
        )

    def can_project(
        self,
        player: tuple[int, int],
        destination: tuple[int, int],
        *,
        sprite: bool = False,
    ) -> bool:
        hwnd = self._find_window()
        if not hwnd:
            return False
        return self._project(
            hwnd,
            player,
            destination,
            sprite=sprite,
        ) is not None

    def move(
        self,
        player: tuple[int, int],
        destination: tuple[int, int],
    ) -> dict[str, Any]:
        hwnd = self._find_window()
        if not hwnd:
            return {"ok": False, "reason": "window_not_found"}

        point = self._project(hwnd, player, destination)
        if point is None:
            return {"ok": False, "reason": "destination_not_clickable"}

        self._click_screen(hwnd, point[0], point[1])
        return {
            "ok": True,
            "screen": {"x": point[0], "y": point[1]},
            "map_from": {"x": player[0], "y": player[1]},
            "map_to": {"x": destination[0], "y": destination[1]},
        }

    def _direction_screen_point(
        self,
        hwnd: int,
        dx: int,
        dy: int,
        *,
        radius_px: int = 170,
    ) -> tuple[int, int] | None:
        if not self.calibration_valid() or not self._calibration:
            return None
        if dx == 0 and dy == 0:
            return None

        geometry = self._geometry(hwnd)
        if not geometry:
            return None

        coeffs = self._calibration["coefficients"]
        cx = coeffs["screen_x"]
        cy = coeffs["screen_y"]

        local_origin_x = float(cx[0])
        local_origin_y = float(cy[0])

        # Use the calibrated isometric basis with a stable player-screen origin.
        vx = cx[1] * dx + cx[2] * dy
        vy = cy[1] * dx + cy[2] * dy
        length = math.hypot(vx, vy)
        if length < 1e-6:
            return None

        local_x = local_origin_x + (vx / length) * radius_px
        local_y = local_origin_y + (vy / length) * radius_px

        margin_x = 80
        margin_y = 65
        local_x = max(margin_x, min(geometry["width"] - margin_x, local_x))
        local_y = max(margin_y, min(geometry["height"] - margin_y, local_y))

        return (
            geometry["left"] + int(round(local_x)),
            geometry["top"] + int(round(local_y)),
        )

    def begin_hold_direction(
        self,
        dx: int,
        dy: int,
        *,
        radius_px: int = 170,
    ) -> dict[str, Any]:
        hwnd = self._find_window()
        if not hwnd:
            return {"ok": False, "reason": "window_not_found"}

        point = self._direction_screen_point(
            hwnd,
            dx,
            dy,
            radius_px=radius_px,
        )
        if point is None:
            return {"ok": False, "reason": "direction_not_projectable"}

        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.SetCursorPos(int(point[0]), int(point[1]))
        time.sleep(0.015)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

        self._hold_active = True
        self._hold_hwnd = hwnd
        self._hold_point = point
        self._hold_desired_point = point
        self._hold_last_update = time.time()
        hunting_diagnostic_recorder.event(
            "mouse",
            "hold_begin",
            {
                "screen": {"x": point[0], "y": point[1]},
                "direction": {"dx": dx, "dy": dy},
                "radius_px": radius_px,
            },
        )
        return {
            "ok": True,
            "screen": {"x": point[0], "y": point[1]},
            "direction": {"dx": dx, "dy": dy},
        }

    def update_hold_direction(
        self,
        dx: int,
        dy: int,
        *,
        radius_px: int = 170,
        min_pixel_change: int = 18,
    ) -> dict[str, Any]:
        if not self._hold_active or not self._hold_hwnd:
            return self.begin_hold_direction(dx, dy, radius_px=radius_px)

        point = self._direction_screen_point(
            self._hold_hwnd,
            dx,
            dy,
            radius_px=radius_px,
        )
        if point is None:
            return {"ok": False, "reason": "direction_not_projectable"}

        last = self._hold_point
        self._hold_desired_point = point

        if last is None:
            user32.SetCursorPos(int(point[0]), int(point[1]))
            self._hold_point = point
            self._hold_last_update = time.time()
        else:
            delta_x = point[0] - last[0]
            delta_y = point[1] - last[1]
            distance = math.hypot(delta_x, delta_y)

            # Keep the hand very steady during normal travel. Gentle route
            # curvature only causes tiny cursor movement; actual corners move
            # the cursor much faster so the character turns decisively.
            if distance >= min_pixel_change:
                now = time.time()
                if distance < 45:
                    max_step = 3.0
                    turn_kind = "micro"
                    min_interval = 0.22
                elif distance < 110:
                    max_step = 10.0
                    turn_kind = "bend"
                    min_interval = 0.12
                else:
                    max_step = 75.0
                    turn_kind = "corner"
                    min_interval = 0.035

                if now - self._hold_last_update < min_interval:
                    return {
                        "ok": True,
                        "screen": {"x": last[0], "y": last[1]},
                        "desired_screen": {"x": point[0], "y": point[1]},
                        "direction": {"dx": dx, "dy": dy},
                        "held": True,
                    }

                if distance <= max_step:
                    next_point = point
                else:
                    scale = max_step / distance
                    next_point = (
                        int(round(last[0] + delta_x * scale)),
                        int(round(last[1] + delta_y * scale)),
                    )

                user32.SetCursorPos(
                    int(next_point[0]),
                    int(next_point[1]),
                )
                self._hold_point = next_point
                self._hold_last_update = time.time()

                hunting_diagnostic_recorder.event(
                    "mouse",
                    "steering_turn",
                    {
                        "from_screen": {
                            "x": last[0],
                            "y": last[1],
                        },
                        "desired_screen": {
                            "x": point[0],
                            "y": point[1],
                        },
                        "to_screen": {
                            "x": next_point[0],
                            "y": next_point[1],
                        },
                        "direction": {"dx": dx, "dy": dy},
                        "radius_px": radius_px,
                        "turn_kind": turn_kind,
                        "remaining_px": round(
                            math.hypot(
                                point[0] - next_point[0],
                                point[1] - next_point[1],
                            ),
                            1,
                        ),
                    },
                    screenshot=turn_kind == "corner",
                    screenshot_cooldown=0.40,
                )

        return {
            "ok": True,
            "screen": {"x": point[0], "y": point[1]},
            "direction": {"dx": dx, "dy": dy},
        }

    def begin_hold_move(
        self,
        player: tuple[int, int],
        destination: tuple[int, int],
    ) -> dict[str, Any]:
        hwnd = self._find_window()
        if not hwnd:
            return {"ok": False, "reason": "window_not_found"}

        point = self._project(hwnd, player, destination)
        if point is None:
            return {"ok": False, "reason": "destination_not_clickable"}

        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.SetCursorPos(int(point[0]), int(point[1]))
        time.sleep(0.015)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

        self._hold_active = True
        self._hold_hwnd = hwnd
        self._hold_point = point
        return {
            "ok": True,
            "screen": {"x": point[0], "y": point[1]},
            "map_from": {"x": player[0], "y": player[1]},
            "map_to": {"x": destination[0], "y": destination[1]},
        }

    def update_hold_move(
        self,
        player: tuple[int, int],
        destination: tuple[int, int],
        *,
        min_pixel_change: int = 10,
    ) -> dict[str, Any]:
        if not self._hold_active or not self._hold_hwnd:
            return self.begin_hold_move(player, destination)

        hwnd = self._hold_hwnd
        point = self._project(hwnd, player, destination)
        if point is None:
            return {"ok": False, "reason": "destination_not_clickable"}

        last = self._hold_point
        if (
            last is None
            or abs(point[0] - last[0]) >= min_pixel_change
            or abs(point[1] - last[1]) >= min_pixel_change
        ):
            user32.SetCursorPos(int(point[0]), int(point[1]))
            self._hold_point = point

        return {
            "ok": True,
            "screen": {"x": point[0], "y": point[1]},
            "map_from": {"x": player[0], "y": player[1]},
            "map_to": {"x": destination[0], "y": destination[1]},
        }

    def release_hold_move(self):
        was_active = self._hold_active
        last_point = self._hold_point
        if self._hold_active:
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self._hold_active = False
        self._hold_hwnd = None
        self._hold_point = None
        self._hold_desired_point = None
        self._hold_last_update = 0.0
        if was_active:
            hunting_diagnostic_recorder.event(
                "mouse",
                "hold_release",
                {
                    "last_screen": (
                        {"x": last_point[0], "y": last_point[1]}
                        if last_point else None
                    )
                },
            )

    def loot(
        self,
        player: tuple[int, int],
        item: tuple[int, int],
    ) -> dict[str, Any]:
        hwnd = self._find_window()
        if not hwnd:
            return {"ok": False, "reason": "window_not_found"}

        point = self._project(hwnd, player, item, sprite=False)
        if point is None:
            return {"ok": False, "reason": "item_not_clickable"}

        self.release_hold_move()
        hunting_diagnostic_recorder.event(
            "mouse",
            "loot_click",
            {
                "screen": {"x": point[0], "y": point[1]},
                "map_from": {"x": player[0], "y": player[1]},
                "map_item": {"x": item[0], "y": item[1]},
            },
            screenshot=True,
            screenshot_cooldown=0.20,
        )
        self._click_screen(hwnd, point[0], point[1])
        return {
            "ok": True,
            "screen": {"x": point[0], "y": point[1]},
            "map_from": {"x": player[0], "y": player[1]},
            "map_to": {"x": item[0], "y": item[1]},
        }

    def attack(
        self,
        player: tuple[float, float],
        target: tuple[float, float],
        *,
        retry_index: int = 0,
        target_key: str | None = None,
    ) -> dict[str, Any]:
        hwnd = self._find_window()
        if not hwnd:
            return {"ok": False, "reason": "window_not_found"}

        # Attack input must never compete with held wandering movement.
        self.release_hold_move()

        # Keep aiming deterministic. Retry only changes the vertical sprite
        # offset slightly; it does not scan/probe the screen with the cursor.
        normalized_key = (target_key or "").strip().lower()
        base_offset = self.learned_attack_y_offsets.get(
            normalized_key,
            self.sprite_y_offset,
        )
        offsets = [
            base_offset,
            base_offset - 10,
        ]
        offset = offsets[min(retry_index, len(offsets) - 1)]

        point = self._project(
            hwnd,
            player,
            target,
            sprite=True,
            sprite_y_offset=offset,
        )
        if point is None:
            return {"ok": False, "reason": "target_not_clickable"}

        click_x, click_y = int(point[0]), int(point[1])
        hunting_diagnostic_recorder.event(
            "mouse",
            "attack_click",
            {
                "screen": {"x": click_x, "y": click_y},
                "map_from": {"x": player[0], "y": player[1]},
                "map_target": {"x": target[0], "y": target[1]},
                "sprite_y_offset": offset,
                "retry_index": retry_index,
                "target_key": target_key,
            },
            screenshot=True,
        )
        self._click_screen(hwnd, click_x, click_y)
        return {
            "ok": True,
            "screen": {"x": click_x, "y": click_y},
            "map_from": {"x": player[0], "y": player[1]},
            "map_to": {"x": target[0], "y": target[1]},
            "sprite_y_offset": offset,
            "precision_method": "stable_calibration",
        }

    def note_attack_registered(
        self,
        retry_index: int,
        *,
        target_key: str | None = None,
    ):
        """Learn a vertical click offset for this monster type only."""
        normalized_key = (target_key or "").strip().lower()
        base = self.learned_attack_y_offsets.get(
            normalized_key,
            self.sprite_y_offset,
        )
        accepted = base if retry_index <= 0 else base - 10
        accepted = max(-80, min(30, int(accepted)))
        if normalized_key:
            self.learned_attack_y_offsets[normalized_key] = accepted
        self.learned_attack_y_offset = accepted


    def reset_attack_learning(self):
        self.learned_attack_y_offset = None
        self.learned_attack_y_offsets.clear()

    def precision_snapshot(self) -> dict[str, Any]:
        return {
            "configured_sprite_y_offset": self.sprite_y_offset,
            "learned_attack_y_offset": self.learned_attack_y_offset,
            "learned_attack_y_offsets": dict(self.learned_attack_y_offsets),
            "projection_mode": "stable_calibration",
        }


mouse_game_adapter = MouseGameAdapter()
