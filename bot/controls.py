from __future__ import annotations

import math
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


def move_randomly(
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

        click_relative(window_rect, x, y)
        time.sleep(click_delay)
        return x, y

    return px, py
