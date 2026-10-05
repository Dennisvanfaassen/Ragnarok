from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any

from core.full_automation import full_automation_controller
from core.mouse_adapter import mouse_game_adapter


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

# Windows virtual-key code for ] } on a standard keyboard.
VK_OEM_6 = 0xDD
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0x524F


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
    """Global ] toggle for Full Hunt Automation.

    The key is swallowed while RO Control is running. On stop, any held mouse
    button is released synchronously before the full automation shutdown begins.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._hook = None
        self._callback = None
        self._pressed = False
        self._lock = threading.RLock()
        self._status = "stopped"
        self._message = "Global ] hotkey is not running."
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
                name="full-automation-bracket-hotkey",
            )
            self._thread.start()

    def _queue_toggle(self):
        # Emergency responsiveness: release held movement immediately on the
        # keyboard-hook thread if hunting is currently active.
        try:
            if full_automation_controller.snapshot().get("running"):
                mouse_game_adapter.release_hold_move()
        except Exception:
            pass

        threading.Thread(
            target=self._toggle,
            daemon=True,
            name="full-automation-bracket-toggle",
        ).start()

    def _toggle(self):
        # Serialize toggles so a second ] press cannot race a still-starting or
        # still-stopping automation worker.
        with self._lock:
            if getattr(self, "_toggle_busy", False):
                return
            self._toggle_busy = True

        try:
            state = full_automation_controller.snapshot()
            if state.get("running"):
                full_automation_controller.stop()
                action = "stopped"
                message = "Full automation stopped with ]."
            else:
                full_automation_controller.start()
                action = "started"
                message = "Full automation started with ]."

            with self._lock:
                self._last_toggle_at = time.time()
                self._last_action = action
                self._last_error = None
                self._message = message
        except Exception as exc:
            # Even if startup fails, a stop press must never leave mouse held.
            try:
                mouse_game_adapter.release_hold_move()
            except Exception:
                pass
            with self._lock:
                self._last_toggle_at = time.time()
                self._last_action = "error"
                self._last_error = str(exc)
                self._message = f"] toggle failed: {exc}"
        finally:
            with self._lock:
                self._toggle_busy = False

    def _message_loop(self):
        # Prefer RegisterHotKey: Windows delivers WM_HOTKEY globally even when
        # Classic.exe has focus. MOD_NOREPEAT prevents key-repeat toggling.
        if user32.RegisterHotKey(
            None,
            HOTKEY_ID,
            MOD_NOREPEAT,
            VK_OEM_6,
        ):
            with self._lock:
                self._status = "ready"
                self._message = "] toggles Full Hunt Automation globally."

            msg = wintypes.MSG()
            try:
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                    if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                        self._queue_toggle()
                    else:
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
            finally:
                user32.UnregisterHotKey(None, HOTKEY_ID)
                try:
                    mouse_game_adapter.release_hold_move()
                except Exception:
                    pass
                with self._lock:
                    self._status = "stopped"
                    self._message = "Global ] hotkey stopped."
            return

        # Fallback for systems/layouts where VK_OEM_4 cannot be registered.
        @LowLevelKeyboardProc
        def callback(n_code, w_param, l_param):
            if n_code >= 0:
                info = ctypes.cast(
                    l_param,
                    ctypes.POINTER(KBDLLHOOKSTRUCT),
                ).contents

                if int(info.vkCode) == VK_OEM_6:
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
                    "Could not register global ]. Run RO Control as administrator."
                )
                self._last_error = self._message
            return

        self._hook = hook
        with self._lock:
            self._status = "ready"
            self._message = "] toggles Full Hunt Automation globally (keyboard-hook fallback)."

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            try:
                mouse_game_adapter.release_hold_move()
            except Exception:
                pass
            with self._lock:
                self._status = "stopped"
                self._message = "Global ] hotkey stopped."

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "message": self._message,
                "key": "]",
                "virtual_key": "VK_OEM_6",
                "last_toggle_at": self._last_toggle_at,
                "last_action": self._last_action,
                "last_error": self._last_error,
                "full_automation_running": bool(
                    full_automation_controller.snapshot().get("running")
                ),
            }


hunting_hotkey = HuntingHotkey()
