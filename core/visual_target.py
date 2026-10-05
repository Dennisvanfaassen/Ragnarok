from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes
from typing import Any

import mss
import numpy as np


user32 = ctypes.windll.user32
CURSOR_SHOWING = 0x00000001


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", POINT),
    ]


class DynamicProjectionTracker:
    """Visual correction + cursor-hitbox probing for Classic.exe.

    The network still supplies exact map coordinates. Vision is used only to
    correct the screen-space origin and refine the final click point.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._baseline_bar: tuple[float, float] | None = None
        self._last_bar: tuple[float, float] | None = None
        self._last_anchor_at = 0.0
        self._last_anchor_confidence = 0.0
        self._last_hitbox: dict[str, Any] | None = None

    @staticmethod
    def _cursor_handle() -> int | None:
        info = CURSORINFO()
        info.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(info)):
            return None
        if not (info.flags & CURSOR_SHOWING):
            return None
        return int(ctypes.cast(info.hCursor, ctypes.c_void_p).value or 0)

    @staticmethod
    def _longest_run(mask_row: np.ndarray) -> tuple[int, int] | None:
        idx = np.flatnonzero(mask_row)
        if idx.size == 0:
            return None

        best_start = int(idx[0])
        best_end = int(idx[0])
        start = int(idx[0])
        previous = int(idx[0])

        for raw in idx[1:]:
            current = int(raw)
            if current == previous + 1:
                previous = current
                continue
            if previous - start > best_end - best_start:
                best_start, best_end = start, previous
            start = current
            previous = current

        if previous - start > best_end - best_start:
            best_start, best_end = start, previous

        return best_start, best_end

    def detect_player_bar(
        self,
        geometry: dict[str, int],
    ) -> tuple[float, float, float] | None:
        """Locate the player's stacked HP/SP bars near the viewport centre."""
        left = geometry["left"]
        top = geometry["top"]
        width = geometry["width"]
        height = geometry["height"]

        # Keep the search away from UI edges, chat and status windows.
        roi_left = int(width * 0.28)
        roi_right = int(width * 0.72)
        roi_top = int(height * 0.24)
        roi_bottom = int(height * 0.70)

        monitor = {
            "left": left + roi_left,
            "top": top + roi_top,
            "width": max(1, roi_right - roi_left),
            "height": max(1, roi_bottom - roi_top),
        }

        try:
            with mss.mss() as sct:
                image = np.asarray(sct.grab(monitor), dtype=np.uint8)
        except Exception:
            return None

        # MSS is BGRA.
        b = image[:, :, 0].astype(np.int16)
        g = image[:, :, 1].astype(np.int16)
        r = image[:, :, 2].astype(np.int16)

        green = (
            (g > 125)
            & (g > r + 35)
            & (g > b + 20)
        )
        blue = (
            (b > 120)
            & (b > r + 35)
            & (b > g + 10)
        )

        centre_x = monitor["width"] / 2.0
        centre_y = monitor["height"] / 2.0
        candidates: list[tuple[float, float, float]] = []

        h = image.shape[0]
        for y in range(2, h - 6):
            gr = self._longest_run(green[y])
            if gr is None:
                continue
            gx0, gx1 = gr
            glen = gx1 - gx0 + 1
            if not 12 <= glen <= 80:
                continue

            for by in range(y + 1, min(h, y + 7)):
                br = self._longest_run(blue[by])
                if br is None:
                    continue
                bx0, bx1 = br
                blen = bx1 - bx0 + 1
                if not 10 <= blen <= 80:
                    continue

                overlap = max(0, min(gx1, bx1) - max(gx0, bx0) + 1)
                if overlap < min(glen, blen) * 0.45:
                    continue

                x = ((gx0 + gx1) + (bx0 + bx1)) / 4.0
                yy = (y + by) / 2.0

                # Prefer the candidate nearest the central play area.
                dist = math.hypot(
                    (x - centre_x) / max(1.0, monitor["width"]),
                    (yy - centre_y) / max(1.0, monitor["height"]),
                )
                confidence = max(0.0, 1.0 - dist * 2.2)
                candidates.append((x, yy, confidence))

        if not candidates:
            return None

        x, y, confidence = max(candidates, key=lambda item: item[2])
        screen_x = monitor["left"] + x
        screen_y = monitor["top"] + y

        with self._lock:
            current = (screen_x, screen_y)
            if self._baseline_bar is None:
                self._baseline_bar = current
            self._last_bar = current
            self._last_anchor_at = time.time()
            self._last_anchor_confidence = confidence

        return screen_x, screen_y, confidence

    def corrected_origin(
        self,
        geometry: dict[str, int],
        base_x: float,
        base_y: float,
    ) -> tuple[float, float, dict[str, Any]]:
        detection = self.detect_player_bar(geometry)
        if detection is None:
            return base_x, base_y, {
                "dynamic": False,
                "confidence": 0.0,
            }

        current_x, current_y, confidence = detection
        with self._lock:
            baseline = self._baseline_bar

        if baseline is None:
            return base_x, base_y, {
                "dynamic": False,
                "confidence": confidence,
            }

        dx = current_x - baseline[0]
        dy = current_y - baseline[1]

        # Clamp visual correction. A wild detection should never fling the
        # cursor across the window.
        dx = max(-90.0, min(90.0, dx))
        dy = max(-70.0, min(70.0, dy))

        return base_x + dx, base_y + dy, {
            "dynamic": True,
            "confidence": round(confidence, 3),
            "bar_shift": {"x": round(dx, 1), "y": round(dy, 1)},
        }

    def probe_clickable_hitbox(
        self,
        expected_x: int,
        expected_y: int,
        *,
        roi_width: int = 56,
        roi_height: int = 82,
    ) -> dict[str, Any] | None:
        """Probe a small sprite ROI for a cursor-state change.

        Ragnarok commonly changes its cursor over clickable actors. We first
        sample likely-background points to establish the normal cursor handle,
        then rapidly probe the expected sprite body.
        """
        half_w = roi_width // 2

        background_points = [
            (expected_x - half_w, expected_y + 18),
            (expected_x + half_w, expected_y + 18),
            (expected_x - half_w, expected_y - roi_height),
            (expected_x + half_w, expected_y - roi_height),
        ]

        handles: list[int] = []
        original = POINT()
        user32.GetCursorPos(ctypes.byref(original))

        try:
            for x, y in background_points:
                user32.SetCursorPos(int(x), int(y))
                time.sleep(0.002)
                handle = self._cursor_handle()
                if handle is not None:
                    handles.append(handle)

            baseline = None
            if handles:
                baseline = max(set(handles), key=handles.count)

            # Prioritize the body centre, then a tight vertical/horizontal sweep.
            offsets = [
                (0, 0),
                (0, -10),
                (0, 10),
                (0, -20),
                (-10, 0),
                (10, 0),
                (-10, -12),
                (10, -12),
                (-18, 4),
                (18, 4),
                (0, -30),
                (-18, -20),
                (18, -20),
            ]

            for ox, oy in offsets:
                x = expected_x + ox
                y = expected_y + oy
                user32.SetCursorPos(int(x), int(y))
                time.sleep(0.002)
                handle = self._cursor_handle()
                if (
                    handle is not None
                    and baseline is not None
                    and handle != baseline
                ):
                    result = {
                        "x": int(x),
                        "y": int(y),
                        "method": "cursor_hitbox",
                        "cursor_handle": handle,
                        "baseline_cursor": baseline,
                    }
                    with self._lock:
                        self._last_hitbox = result
                    return result
        finally:
            # Caller will move/click immediately if a hit was found. If not,
            # restore the user's previous pointer location.
            pass

        return None

    def reset_anchor(self):
        with self._lock:
            self._baseline_bar = None
            self._last_bar = None
            self._last_anchor_at = 0.0
            self._last_anchor_confidence = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "baseline_bar": (
                    {"x": round(self._baseline_bar[0], 1), "y": round(self._baseline_bar[1], 1)}
                    if self._baseline_bar else None
                ),
                "last_bar": (
                    {"x": round(self._last_bar[0], 1), "y": round(self._last_bar[1], 1)}
                    if self._last_bar else None
                ),
                "last_anchor_at": self._last_anchor_at,
                "anchor_confidence": round(self._last_anchor_confidence, 3),
                "last_hitbox": self._last_hitbox,
            }


dynamic_projection_tracker = DynamicProjectionTracker()
