from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any

from core.active_control import active_hunt_controller


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_TAB = 0x09


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
    ctypes.c_longlong,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class HuntingHotkey:
    """Global Tab toggle for the Hunting AI.

    Tab is suppressed while this hook is active so the same keypress does not
    also trigger an in-game Tab action.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._hook = None
        self._callback = None
        self._pressed = False
        self._lock = threading.RLock()
        self._status = "stopped"
        self._message = "Global Tab hotkey is not running."
        self._last_toggle_at: float | None = None
        self._last_action: str | None = None
        self._last_error: str | None = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._message_loop,
                daemon=True,
                name="hunting-tab-hotkey",
            )
            self._thread.start()

    def _queue_toggle(self):
        threading.Thread(
            target=self._toggle,
            daemon=True,
            name="hunting-tab-toggle",
        ).start()

    def _toggle(self):
        try:
            state = active_hunt_controller.snapshot()
            if state.get("running"):
                active_hunt_controller.stop()
                action = "stopped"
                message = "Hunting stopped with Tab."
            else:
                active_hunt_controller.start({})
                action = "started"
                message = "Hunting started with Tab."

            with self._lock:
                self._last_toggle_at = time.time()
                self._last_action = action
                self._last_error = None
                self._message = message
        except Exception as exc:
            with self._lock:
                self._last_toggle_at = time.time()
                self._last_action = "error"
                self._last_error = str(exc)
                self._message = f"Tab toggle failed: {exc}"

    def _message_loop(self):
        @LowLevelKeyboardProc
        def callback(n_code, w_param, l_param):
            if n_code >= 0:
                info = ctypes.cast(
                    l_param,
                    ctypes.POINTER(KBDLLHOOKSTRUCT),
                ).contents

                if int(info.vkCode) == VK_TAB:
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        with self._lock:
                            if not self._pressed:
                                self._pressed = True
                                self._queue_toggle()
                        return 1

                    if w_param in (WM_KEYUP, WM_SYSKEYUP):
                        with self._lock:
                            self._pressed = False
                        return 1

            return user32.CallNextHookEx(
                self._hook,
                n_code,
                w_param,
                l_param,
            )

        self._callback = callback
        module = kernel32.GetModuleHandleW(None)
        hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._callback,
            module,
            0,
        )

        if not hook:
            with self._lock:
                self._status = "error"
                self._message = (
                    "Could not install the global Tab hotkey. "
                    "Run RO Control as administrator."
                )
                self._last_error = self._message
            return

        self._hook = hook
        with self._lock:
            self._status = "ready"
            self._message = "Tab toggles hunting on/off globally."

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            with self._lock:
                self._status = "stopped"
                self._message = "Global Tab hotkey stopped."

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "message": self._message,
                "key": "Tab",
                "last_toggle_at": self._last_toggle_at,
                "last_action": self._last_action,
                "last_error": self._last_error,
                "hunting_running": bool(
                    active_hunt_controller.snapshot().get("running")
                ),
            }


hunting_hotkey = HuntingHotkey()
