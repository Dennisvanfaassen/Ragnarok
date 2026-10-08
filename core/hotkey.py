from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any

from core.active_control import active_hunt_controller
from core.mouse_adapter import mouse_game_adapter
from diagnostics.native_action_bridge import native_action_bridge


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

# Windows virtual-key code for = + on a standard keyboard.
VK_OEM_PLUS = 0xBB
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
    """Global = toggle for hunting.

    The key is swallowed while RO Control is running. On stop, any held mouse
    button is released synchronously before the hunting shutdown begins.
    """

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._hook = None
        self._callback = None
        self._pressed = False
        self._lock = threading.RLock()
        self._status = "stopped"
        self._message = "Global = hotkey is not running."
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
                name="hunting-equals-hotkey",
            )
            self._thread.start()

    def _queue_toggle(self):
        # Emergency responsiveness: release held movement immediately on the
        # keyboard-hook thread if hunting is currently active.
        try:
            if active_hunt_controller.snapshot().get("running"):
                mouse_game_adapter.release_hold_move()
        except Exception:
            pass

        threading.Thread(
            target=self._toggle,
            daemon=True,
            name="hunting-equals-toggle",
        ).start()

    def _toggle(self):
        # Serialize toggles so a second = press cannot race a still-starting or
        # still-stopping automation worker.
        with self._lock:
            if getattr(self, "_toggle_busy", False):
                return
            self._toggle_busy = True

        try:
            state = active_hunt_controller.snapshot()
            if state.get("running"):
                active_hunt_controller.stop()
                action = "stopped"
                message = "Hunting stopped with =."
            else:
                native = native_action_bridge.snapshot()
                if not native.get("attached"):
                    native_action_bridge.start()

                # Match the reliable dashboard-start preparation: give the
                # authenticated client a short window to learn the map socket
                # before HuntingAI performs its native-only readiness check.
                deadline = time.time() + 2.5
                while time.time() < deadline:
                    agent = native_action_bridge.snapshot().get("agent") or {}
                    if agent.get("socket_learned"):
                        break
                    time.sleep(0.05)

                active_hunt_controller.start({})
                action = "started"
                message = "Hunting started with =."

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
                self._message = f"= toggle failed: {exc}"
        finally:
            with self._lock:
                self._toggle_busy = False

    def _message_loop(self):
        """Run a dedicated low-level keyboard hook for the hunting toggle.

        OEM punctuation keys such as = are not consistently delivered through
        RegisterHotKey on every keyboard layout/focus combination. A WH_KEYBOARD_LL
        hook sees the physical key event even while Classic.exe owns focus.
        """
        @LowLevelKeyboardProc
        def callback(n_code, w_param, l_param):
            if n_code >= 0:
                info = ctypes.cast(
                    l_param,
                    ctypes.POINTER(KBDLLHOOKSTRUCT),
                ).contents

                vk = int(info.vkCode)
                scan = int(info.scanCode)

                # VK_OEM_PLUS is the normal Windows virtual key for =/+.
                # Scan code 0x0D is the physical US/OEM equals/plus key and is
                # accepted as a fallback for layouts that translate OEM keys
                # differently.
                is_toggle_key = (
                    vk == VK_OEM_PLUS
                    or scan == 0x0D
                )

                if is_toggle_key:
                    if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        should_toggle = False
                        with self._lock:
                            if not self._pressed:
                                self._pressed = True
                                should_toggle = True
                        if should_toggle:
                            self._queue_toggle()
                        # Swallow the key so Ragnarok never receives = itself.
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
                    "Could not install the global = hunting hotkey. "
                    "Run RO Control as administrator."
                )
                self._last_error = self._message
            return

        self._hook = hook
        with self._lock:
            self._status = "ready"
            self._message = "= starts/stops hunting globally."
            self._last_error = None

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
                self._pressed = False
                self._status = "stopped"
                self._message = "Global = hunting hotkey stopped."

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "message": self._message,
                "key": "=",
                "virtual_key": "VK_OEM_PLUS",
                "last_toggle_at": self._last_toggle_at,
                "last_action": self._last_action,
                "last_error": self._last_error,
                "hunt_running": bool(
                    active_hunt_controller.snapshot().get("running")
                ),
            }


hunting_hotkey = HuntingHotkey()
