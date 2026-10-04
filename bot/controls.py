from __future__ import annotations

import ctypes
import math
import random
import time

import pyautogui
import win32con
import win32gui


pyautogui.PAUSE = 0.03
pyautogui.FAILSAFE = True

user32 = ctypes.windll.user32


def _activate(hwnd: int) -> None:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _move_cursor(screen_x: int, screen_y: int) -> None:
    # Use the raw Win32 call first. Unlike win32api.SetCursorPos this does not
    # raise the '(0, SetCursorPos, No error message is available)' pywin32 error
    # seen on some Windows setups.
    ok = user32.SetCursorPos(int(screen_x), int(screen_y))
    if ok:
        return

    # Fallback if Windows rejects the direct call.
    pyautogui.moveTo(int(screen_x), int(screen_y), duration=0.05)


def _left_click() -> None:
    # mouse_event is widely compatible with older DirectX-era clients.
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.025)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def click_relative(hwnd: int, window_rect, x: int, y: int, clicks: int = 1) -> None:
    left, top, _right, _bottom = window_rect
    sx = int(left + x)
    sy = int(top + y)

    _activate(hwnd)
    time.sleep(0.04)
    _move_cursor(sx, sy)
    time.sleep(0.03)

    for _ in range(max(1, clicks)):
        _left_click()
        time.sleep(0.045)


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
    cx, cy = center_xy

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
