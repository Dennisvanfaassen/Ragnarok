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
        self._calibration_path = (
            Path(__file__).resolve().parents[1] / "screen_calibration.json"
        )
        self._calibration: dict[str, Any] | None = None
        self._calibration_status = "not_calibrated"
        self._calibration_message = "Run screen calibration."
        self._calibration_thread: threading.Thread | None = None
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

    def attack(
        self,
        player: tuple[int, int],
        target: tuple[int, int],
        *,
        retry_index: int = 0,
    ) -> dict[str, Any]:
        hwnd = self._find_window()
        if not hwnd:
            return {"ok": False, "reason": "window_not_found"}

        # Small vertical sweep on retries, but always within the same actor tile.
        offsets = [
            self.sprite_y_offset,
            self.sprite_y_offset - 10,
            self.sprite_y_offset + 10,
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

        self._click_screen(hwnd, point[0], point[1])
        return {
            "ok": True,
            "screen": {"x": point[0], "y": point[1]},
            "map_from": {"x": player[0], "y": player[1]},
            "map_to": {"x": target[0], "y": target[1]},
            "sprite_y_offset": offset,
        }


mouse_game_adapter = MouseGameAdapter()
