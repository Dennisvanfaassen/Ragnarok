from __future__ import annotations

import ctypes
import random
import threading
import time

try:
    import winsound
except Exception:  # pragma: no cover - non-Windows development environments
    winsound = None
from typing import Any

from core.state import app_state
from core.active_control import active_hunt_controller
from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.native_action_bridge import native_action_bridge


MEAT_ID = 517
FLY_WING_ID = 601
PIERCE_SKILL_ID = 56
VK_Q = 0x51
VK_W = 0x57
VK_E = 0x45
VK_R = 0x52
KEYEVENTF_KEYUP = 0x0002


class ManualAssistant:
    """Optional manual-play helper.

    Never walks and never attacks. It may use the configured healing item and
    exposes passive warnings while the user remains in control.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._hotkey_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.enabled = False
        self._next_heal_threshold: int | None = None
        self._last_heal_at = 0.0
        self._last_warning_keys: set[str] = set()
        self._warnings: list[dict[str, Any]] = []
        self._heals_used = 0
        self._last_heal_result: dict[str, Any] | None = None
        self._pierce_armed_at: float | None = None
        self._pierce_target_id: int | None = None
        self._pierce_last_skill_stamp: float | None = None
        self._pierce_last_kill_seq = 0
        self._pierce_macro_state = "idle"
        self._pierce_last_action: dict[str, Any] | None = None
        self._r_was_down = False

    def start_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="manual-play-assistant",
            )
            self._thread.start()
            if not self._hotkey_thread or not self._hotkey_thread.is_alive():
                self._hotkey_thread = threading.Thread(
                    target=self._hotkey_loop,
                    daemon=True,
                    name="manual-pierce-hotkey",
                )
                self._hotkey_thread.start()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self.enabled = bool(enabled)
            if not self.enabled:
                self._next_heal_threshold = None
                self._warnings = []
                self._last_warning_keys.clear()
                self._pierce_armed_at = None
                self._pierce_target_id = None
                self._pierce_macro_state = "idle"
        return self.snapshot()

    @staticmethod
    def _press_virtual_key(vk: int) -> bool:
        """Send a normal Windows key press to the foreground game client."""
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(int(vk), 0, 0, 0)
            time.sleep(0.012)
            user32.keybd_event(int(vk), 0, KEYEVENTF_KEYUP, 0)
            return True
        except Exception:
            return False

    def _restore_sword_and_shield(self, reason: str) -> None:
        # W = Flamberge, Q = Shield in the user's configured RO hotkeys.
        w_ok = self._press_virtual_key(VK_W)
        time.sleep(0.055)
        q_ok = self._press_virtual_key(VK_Q)
        with self._lock:
            self._pierce_last_action = {
                "timestamp": time.time(),
                "action": "restore_flambere_shield",
                "reason": reason,
                "w_ok": w_ok,
                "q_ok": q_ok,
                "target_id": self._pierce_target_id,
            }
            self._pierce_armed_at = None
            self._pierce_target_id = None
            self._pierce_macro_state = "idle"

    def _arm_pierce_macro(self) -> None:
        if not self.enabled:
            return
        if active_hunt_controller.snapshot().get("running"):
            return

        # E = Lance. We do this as soon as R is physically pressed. The player
        # still chooses the monster normally with Ragnarok's Pierce target
        # cursor; no screen coordinates or target automation are involved.
        ok = self._press_virtual_key(VK_E)
        with self._lock:
            self._pierce_armed_at = time.time()
            self._pierce_macro_state = "lance_armed"
            self._pierce_last_action = {
                "timestamp": self._pierce_armed_at,
                "action": "equip_lance",
                "e_ok": ok,
            }

    def _hotkey_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.enabled and not active_hunt_controller.snapshot().get("running"):
                    down = bool(ctypes.windll.user32.GetAsyncKeyState(VK_R) & 0x8000)
                    if down and not self._r_was_down:
                        self._arm_pierce_macro()
                    self._r_was_down = down
                else:
                    self._r_was_down = False
            except Exception:
                self._r_was_down = False
            self._stop.wait(0.01)

    def _update_pierce_macro(self, live: dict[str, Any]) -> None:
        if not self.enabled or active_hunt_controller.snapshot().get("running"):
            return

        world = live.get("world") or {}
        skill = world.get("last_client_skill_use") or {}
        skill_stamp = skill.get("timestamp")
        if skill_stamp is not None and float(skill_stamp) != self._pierce_last_skill_stamp:
            self._pierce_last_skill_stamp = float(skill_stamp)
            if (
                int(skill.get("skill_id") or 0) == PIERCE_SKILL_ID
                and self._pierce_armed_at is not None
                and float(skill_stamp) >= self._pierce_armed_at - 0.5
            ):
                target_id = int(skill.get("target_id") or 0)
                if target_id > 0:
                    with self._lock:
                        self._pierce_target_id = target_id
                        self._pierce_macro_state = "pierce_fight"
                        self._pierce_last_action = {
                            "timestamp": time.time(),
                            "action": "pierce_target_locked",
                            "target_id": target_id,
                            "skill_level": int(skill.get("skill_level") or 0),
                        }

        kill_seq = int(live.get("monster_kill_seq") or 0)
        if kill_seq > self._pierce_last_kill_seq:
            self._pierce_last_kill_seq = kill_seq
            killed = live.get("last_monster_kill") or {}
            killed_id = int(killed.get("actor_id") or 0)
            if (
                self._pierce_target_id is not None
                and killed_id == int(self._pierce_target_id)
            ):
                self._restore_sword_and_shield("pierce_target_dead")
                return

        # If R was pressed but the player cancelled the targeting cursor, there
        # is no active monster fight to wait for. Restore after a short timeout.
        if (
            self._pierce_armed_at is not None
            and self._pierce_target_id is None
            and time.time() - self._pierce_armed_at > 8.0
        ):
            self._restore_sword_and_shield("pierce_target_cancelled")

    @staticmethod
    def _self_target(world: dict[str, Any]) -> int | None:
        value = world.get("self_account_id") or world.get("self_char_id")
        try:
            return int(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _find_inventory_item(
        inventory: list[dict[str, Any]],
        *,
        name: str,
        fallback_name_id: int | None = None,
    ) -> dict[str, Any] | None:
        wanted = str(name or "").strip().lower()
        for row in inventory:
            if wanted and str(row.get("name") or "").strip().lower() == wanted:
                return row
            if fallback_name_id is not None and int(row.get("name_id") or -1) == fallback_name_id:
                return row
        return None

    def _maybe_heal(
        self,
        world: dict[str, Any],
        inventory: list[dict[str, Any]],
    ) -> None:
        profile = app_state.get_profile()
        healing = profile.healing
        if not profile.manual_assistant.auto_heal:
            return
        if active_hunt_controller.snapshot().get("running"):
            return
        if not healing.enabled:
            return

        hp_percent = world.get("hp_percent")
        if hp_percent is None:
            return

        low = max(1, min(99, int(healing.hp_trigger_min_percent)))
        high = max(low, min(99, int(healing.hp_trigger_max_percent)))
        if self._next_heal_threshold is None:
            self._next_heal_threshold = random.randint(low, high)

        if float(hp_percent) > float(self._next_heal_threshold):
            return
        if time.time() - self._last_heal_at < max(0.25, float(healing.cooldown_seconds)):
            return

        item = self._find_inventory_item(
            inventory,
            name=healing.item or "Meat",
            fallback_name_id=MEAT_ID if str(healing.item or "Meat").strip().lower() == "meat" else None,
        )
        target_id = self._self_target(world)
        if item is None or target_id is None:
            return

        if not native_action_bridge.snapshot().get("attached"):
            try:
                native_action_bridge.start()
            except Exception:
                return

        burst_min = max(1, int(healing.burst_min_items))
        burst_max = max(burst_min, int(healing.burst_max_items))
        burst = random.randint(burst_min, burst_max)
        used = 0
        result: dict[str, Any] | None = None

        for _ in range(burst):
            live_items = (
                authenticated_client_monitor.item_state_snapshot().get("inventory")
                or []
            )
            live = self._find_inventory_item(
                live_items,
                name=healing.item or "Meat",
                fallback_name_id=MEAT_ID if str(healing.item or "Meat").strip().lower() == "meat" else None,
            )
            if live is None or int(live.get("amount") or 0) <= 0:
                break
            result = native_action_bridge.item_use(
                int(live.get("index")),
                target_id,
            )
            if not result.get("ok"):
                break
            used += 1
            if used < burst:
                self._stop.wait(max(0.0, float(healing.burst_delay_seconds)))

        if used:
            self._last_heal_at = time.time()
            self._heals_used += used
            self._next_heal_threshold = random.randint(low, high)
            self._last_heal_result = {
                "timestamp": self._last_heal_at,
                "item": healing.item or "Meat",
                "used": used,
                "result": result,
            }

    @staticmethod
    def _status_remaining_seconds(status: dict[str, Any]) -> float | None:
        tick = status.get("tick")
        updated_at = status.get("updated_at")
        if tick is None or updated_at is None:
            return None
        try:
            duration = float(tick) / 1000.0
            if duration <= 0 or duration > 24 * 3600:
                return None
            return max(0.0, float(updated_at) + duration - time.time())
        except Exception:
            return None

    def _build_warnings(
        self,
        world: dict[str, Any],
        inventory: list[dict[str, Any]],
        character: dict[str, Any],
    ) -> list[dict[str, Any]]:
        profile = app_state.get_profile()
        settings = profile.manual_assistant
        warnings: list[dict[str, Any]] = []

        weight = world.get("weight_percent")
        if weight is not None and float(weight) >= float(settings.warn_weight_percent):
            warnings.append({
                "key": "overweight",
                "level": "warning",
                "message": f"Weight is {float(weight):.1f}% (warning at {settings.warn_weight_percent}%).",
            })

        healing_name = str(profile.healing.item or "Meat").strip()
        healing_key = healing_name.lower()
        food_amount = sum(
            int(row.get("amount") or 0)
            for row in inventory
            if str(row.get("name") or "").strip().lower() == healing_key
            or (
                healing_key == "meat"
                and int(row.get("name_id") or -1) == MEAT_ID
            )
        )
        if food_amount <= int(settings.warn_meat_below):
            warnings.append({
                "key": "low_food",
                "level": "warning",
                "message": f"{healing_name} is low: {food_amount} left.",
            })

        fly = sum(
            int(row.get("amount") or 0)
            for row in inventory
            if int(row.get("name_id") or -1) == FLY_WING_ID
            or str(row.get("name") or "").strip().lower() == "fly wing"
        )
        if fly <= int(settings.warn_fly_wings_below):
            warnings.append({
                "key": "low_fly_wings",
                "level": "warning",
                "message": f"Fly Wings are low: {fly} left.",
            })

        if profile.aspd.status_effect_id is not None:
            wanted = int(profile.aspd.status_effect_id)
            status = next(
                (
                    row for row in (character.get("active_statuses") or [])
                    if int(row.get("type") or -1) == wanted
                ),
                None,
            )
            if status is not None:
                remaining = self._status_remaining_seconds(status)
                if remaining is not None and remaining <= float(settings.aspd_warning_seconds):
                    warnings.append({
                        "key": "aspd_expiring",
                        "level": "warning",
                        "message": f"{profile.aspd.item} expires in about {max(0, int(round(remaining)))} seconds.",
                    })

        return warnings

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.enabled:
                    snapshot = authenticated_client_monitor.snapshot()
                    if snapshot.get("classic_pid"):
                        live = snapshot.get("live_state") or {}
                        world = live.get("world") or {}
                        inventory = live.get("inventory") or []
                        character = authenticated_client_monitor.character_snapshot()
                        self._update_pierce_macro(live)
                        self._maybe_heal(world, inventory)
                        warnings = self._build_warnings(world, inventory, character)
                        new_keys = {str(row.get("key")) for row in warnings}
                        with self._lock:
                            previous_keys = set(self._last_warning_keys)
                            self._warnings = warnings
                            self._last_warning_keys = new_keys
                        if new_keys - previous_keys and winsound is not None:
                            try:
                                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                            except Exception:
                                pass
            except Exception:
                pass
            self._stop.wait(0.25)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "mode": "manual_assist" if self.enabled else "off",
                "movement_control": False,
                "attack_control": False,
                "auto_healing": (
                    self.enabled
                    and app_state.get_profile().manual_assistant.auto_heal
                    and not active_hunt_controller.snapshot().get("running")
                ),
                "warnings": list(self._warnings),
                "healing_items_used": self._heals_used,
                "next_heal_threshold": self._next_heal_threshold,
                "last_heal_result": self._last_heal_result,
                "pierce_macro": {
                    "enabled": self.enabled,
                    "state": self._pierce_macro_state,
                    "target_id": self._pierce_target_id,
                    "armed_at": self._pierce_armed_at,
                    "last_action": self._pierce_last_action,
                    "hotkeys": {
                        "shield": "Q",
                        "flamberge": "W",
                        "lance": "E",
                        "pierce": "R",
                    },
                },
            }


manual_assistant = ManualAssistant()
