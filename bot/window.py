from __future__ import annotations

import ctypes

import psutil
import win32gui
import win32process


# Keep screenshot coordinates, ClientToScreen coordinates and mouse coordinates
# in the same physical pixel coordinate system on scaled Windows desktops.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _client_rect(hwnd: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    x1, y1 = win32gui.ClientToScreen(hwnd, (left, top))
    x2, y2 = win32gui.ClientToScreen(hwnd, (right, bottom))
    return x1, y1, x2, y2


def find_game_window(
    process_names: list[str],
    title_candidates: list[str],
) -> tuple[int, tuple[int, int, int, int], str]:
    process_names = {name.lower() for name in process_names}
    title_candidates = [value.lower() for value in title_candidates]
    matches: list[tuple[int, int, str]] = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return

        title = win32gui.GetWindowText(hwnd)
        if not title:
            return

        try:
            _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = psutil.Process(pid).name().lower()
        except (psutil.Error, OSError):
            process_name = ""

        score = 0
        if process_name in process_names:
            score += 100

        low_title = title.lower()
        for index, candidate in enumerate(title_candidates):
            if candidate in low_title:
                score += max(1, 20 - index)

        if score:
            try:
                x1, y1, x2, y2 = _client_rect(hwnd)
            except Exception:
                return
            if x2 - x1 >= 800 and y2 - y1 >= 500:
                matches.append((score, hwnd, title))

    win32gui.EnumWindows(callback, None)

    if not matches:
        raise RuntimeError(
            "Ragnarok window not found. Start Classic.exe and make sure it is visible."
        )

    matches.sort(reverse=True, key=lambda item: item[0])
    _score, hwnd, title = matches[0]
    return hwnd, _client_rect(hwnd), title


def focus_window(hwnd: int) -> None:
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
