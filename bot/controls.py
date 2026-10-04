from __future__ import annotations

import random
import time

import pyautogui


pyautogui.PAUSE = 0.03
pyautogui.FAILSAFE = True


def click_relative(window_rect, x: int, y: int) -> None:
    left, top, _right, _bottom = window_rect
    pyautogui.click(left + x, top + y)


def press_key(key: str) -> None:
    pyautogui.press(key)


def move_randomly(window_rect, player_xy, min_radius=120, max_radius=260) -> None:
    left, top, right, bottom = window_rect
    width = right - left
    height = bottom - top
    px, py = player_xy

    angle = random.uniform(0, 6.28318530718)
    radius = random.randint(min_radius, max_radius)

    x = int(px + radius * __import__("math").cos(angle))
    y = int(py + radius * __import__("math").sin(angle))

    x = max(40, min(width - 40, x))
    y = max(90, min(height - 60, y))

    click_relative(window_rect, x, y)
    time.sleep(0.15)
