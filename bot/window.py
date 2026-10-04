from __future__ import annotations

import win32gui


def find_game_window(title_candidates: list[str]) -> tuple[int, tuple[int, int, int, int]]:
    matches: list[tuple[int, str]] = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        low = title.lower()
        if any(candidate.lower() in low for candidate in title_candidates):
            matches.append((hwnd, title))

    win32gui.EnumWindows(callback, None)
    if not matches:
        raise RuntimeError(
            "Ragnarok window not found. Update window.title_contains in config.yaml."
        )

    hwnd, _title = matches[0]
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x1, y1 = win32gui.ClientToScreen(hwnd, (left, top))
    x2, y2 = win32gui.ClientToScreen(hwnd, (right, bottom))
    return hwnd, (x1, y1, x2, y2)


def focus_window(hwnd: int) -> None:
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
