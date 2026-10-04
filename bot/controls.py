from __future__ import annotations

import math
import random
import time

import pyautogui
import win32api
import win32con
import win32gui


pyautogui.PAUSE = 0.03
pyautogui.FAILSAFE = True


def _activate(hwnd: int) -> None:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def click_relative(hwnd: int, window_rect, x: int, y: int, clicks: int = 1) -> None:
    left, top, _right, _bottom = window_rect
    sx = int(left + x)
    sy = int(top + y)

    _activate(hwnd)
    time.sleep(0.03)

    # Native Windows cursor/mouse events are more reliable for the windowed
    # Ragnarok client than pyautogui.click alone.
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.02)

    for _ in range(max(1, clicks)):
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.025)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.04)


def press_key(key: str) -> None:
    pyautogui.press(key)


def move_randomly(
    hwnd: int,
    window_rect,
    player_xy,
    excluded_regions,
    min_radius=150,
    max_radius=330,
    click_delay=0.20,
) -> tuple[int, int]:
    left, top, right, bottom = window_rect
    width = right - left
    height = bottom - top
    px, py = player_xy

    for _ in range(20):
        angle = random.uniform(0, math.tau)
        radius = random.randint(min_radius, max_radius)
        x = int(px + radius * math.cos(angle))
        y = int(py + radius * math.sin(angle))

        x = max(45, min(width - 45, x))
        y = max(100, min(height - 70, y))

        blocked = False
        for x1r, y1r, x2r, y2r in excluded_regions:
            if x1r * width <= x <= x2r * width and y1r * height <= y <= y2r * height:
                blocked = True
                break
        if blocked:
            continue

        click_relative(hwnd, window_rect, x, y)
        time.sleep(click_delay)
        return x, y

    return px, py


def loot_sweep(
    hwnd: int,
    window_rect,
    center_xy,
    radius: int = 28,
    rings: int = 2,
    points_per_ring: int = 8,
    delay_seconds: float = 0.055,
) -> None:
    """Click the corpse/drop area in a small spiral after a target disappears."""
    cx, cy = center_xy

    # Click exact death location first.
    click_relative(hwnd, window_rect, cx, cy, clicks=2)
    time.sleep(delay_seconds)

    for ring in range(1, max(1, rings) + 1):
        r = radius * ring
        for i in range(points_per_ring):
            angle = math.tau * i / points_per_ring
            x = int(cx + math.cos(angle) * r)
            y = int(cy + math.sin(angle) * (r * 0.65))
            click_relative(hwnd, window_rect, x, y)
            time.sleep(delay_seconds)
