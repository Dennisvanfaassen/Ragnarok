from __future__ import annotations

import ctypes
import math
import random
import time
from ctypes import wintypes

import pyautogui
import win32con
import win32gui


pyautogui.PAUSE = 0.03
pyautogui.FAILSAFE = True

user32 = ctypes.windll.user32

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", INPUT_UNION),
    ]


def _activate(hwnd: int) -> None:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _send_mouse(flags: int, dx: int = 0, dy: int = 0) -> bool:
    inp = INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(
            dx=dx,
            dy=dy,
            mouseData=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=None,
        ),
    )
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    return sent == 1


def _absolute_coords(screen_x: int, screen_y: int) -> tuple[int, int]:
    vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN

    if vw <= 1 or vh <= 1:
        vw = user32.GetSystemMetrics(0)
        vh = user32.GetSystemMetrics(1)
        vx = 0
        vy = 0

    ax = int((screen_x - vx) * 65535 / max(1, vw - 1))
    ay = int((screen_y - vy) * 65535 / max(1, vh - 1))
    return ax, ay


def _move_cursor(screen_x: int, screen_y: int) -> None:
    ax, ay = _absolute_coords(screen_x, screen_y)
    ok = _send_mouse(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        ax,
        ay,
    )
    if not ok:
        pyautogui.moveTo(int(screen_x), int(screen_y), duration=0.05)


def _left_click() -> None:
    down = _send_mouse(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.035)
    up = _send_mouse(MOUSEEVENTF_LEFTUP)

    if not (down and up):
        pyautogui.mouseDown(button="left")
        time.sleep(0.035)
        pyautogui.mouseUp(button="left")


def click_relative(hwnd: int, window_rect, x: int, y: int, clicks: int = 1) -> None:
    left, top, _right, _bottom = window_rect
    sx = int(left + x)
    sy = int(top + y)

    _activate(hwnd)
    time.sleep(0.06)
    _move_cursor(sx, sy)
    time.sleep(0.05)

    for _ in range(max(1, clicks)):
        _left_click()
        time.sleep(0.065)


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
