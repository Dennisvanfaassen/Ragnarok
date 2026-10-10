from __future__ import annotations

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
FLAMBERGE_ID = 1129
LANCE_ID = 1410
SHIELD_SLOT1_ID = 2106
PIERCE_SKILL_ID = 56

EQUIP_RIGHT_HAND = 0x02
EQUIP_LEFT_HAND = 0x20
EQUIP_BOTH_HANDS = 0x22


class ManualAssistant:
    """Optional manual-play helper.

    Never walks and never attacks. It may use the configured healing item and
    exposes passive warnings while the user remains in control.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.enabled = False
        self._next_heal_threshold: int | None = None
        self._last_heal_at = 0.0
        self._last_warning_keys: set[str] = set()
        self._warnings: list[dict[str, Any]] = []
        self._heals_used = 0
        self._last_heal_result: dict[str, Any] | None = None
        self._last_equip_stamp: float | None = None
        self._last_skill_stamp: float | None = None
        self._last_kill_seq = 0
        self._pierce_target_id: int | None = None
        self._weapon_macro_state = "idle"
        self._last_weapon_macro_action: dict[str, Any] | None = None

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

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self.enabled = bool(enabled)
            if not self.enabled:
                self._next_heal_threshold = None
                self._warnings = []
                self._last_warning_keys.clear()
                self._pierce_target_id = None
                self._weapon_macro_state = "idle"
        if not self.enabled:
            try:
                native_action_bridge.set_pierce_pre_equip(False)
            except Exception:
                pass
        return self.snapshot()

    @staticmethod
    def _find_inventory_id(
        inventory: list[dict[str, Any]],
        name_id: int,
    ) -> dict[str, Any] | None:
        for row in inventory:
            try:
                if int(row.get("name_id") or -1) == int(name_id):
                    return row
            except Exception:
                continue
        return None

    @staticmethod
    def _ensure_native_bridge() -> bool:
        if native_action_bridge.snapshot().get("attached"):
            return True
        try:
            result = native_action_bridge.start()
            return bool(result.get("attached") or result.get("ok"))
        except Exception:
            return False

    def _equip_inventory_item(
        self,
        inventory: list[dict[str, Any]],
        *,
        name_id: int,
        equip_location: int,
    ) -> dict[str, Any]:
        item = self._find_inventory_id(inventory, name_id)
        if item is None:
            return {
                "ok": False,
                "reason": "item_not_in_inventory",
                "name_id": int(name_id),
            }
        if not self._ensure_native_bridge():
            return {"ok": False, "reason": "native_bridge_not_ready"}
        result = native_action_bridge.equip_item(
            int(item.get("index")),
            int(equip_location),
        )
        return result

    def _sync_pierce_pre_equip(
        self,
        inventory: list[dict[str, Any]],
    ) -> dict[str, Any]:
        lance = self._find_inventory_id(inventory, LANCE_ID)
        if lance is None:
            try:
                return native_action_bridge.set_pierce_pre_equip(False)
            except Exception:
                return {"ok": False, "reason": "lance_not_in_inventory"}

        if not self._ensure_native_bridge():
            return {"ok": False, "reason": "native_bridge_not_ready"}

        return native_action_bridge.set_pierce_pre_equip(
            True,
            int(lance.get("index")),
            EQUIP_BOTH_HANDS,
            PIERCE_SKILL_ID,
        )

    def _restore_flamberge_shield(
        self,
        inventory: list[dict[str, Any]],
        *,
        reason: str,
    ) -> None:
        sword = self._equip_inventory_item(
            inventory,
            name_id=FLAMBERGE_ID,
            equip_location=EQUIP_RIGHT_HAND,
        )
        self._stop.wait(0.05)
        shield = self._equip_inventory_item(
            inventory,
            name_id=SHIELD_SLOT1_ID,
            equip_location=EQUIP_LEFT_HAND,
        )
        with self._lock:
            self._pierce_target_id = None
            self._weapon_macro_state = "sword_shield"
            self._last_weapon_macro_action = {
                "timestamp": time.time(),
                "action": "restore_flamberge_shield",
                "reason": reason,
                "flamberge": sword,
                "shield": shield,
            }

    def _update_weapon_macro(
        self,
        live: dict[str, Any],
        inventory: list[dict[str, Any]],
    ) -> None:
        if not self.enabled:
            return
        if active_hunt_controller.snapshot().get("running"):
            return

        world = live.get("world") or {}

        equip = world.get("last_client_equip") or {}
        equip_stamp = equip.get("timestamp")
        if equip_stamp is not None and float(equip_stamp) != self._last_equip_stamp:
            self._last_equip_stamp = float(equip_stamp)
            name_id = int(equip.get("name_id") or 0)

            if name_id == SHIELD_SLOT1_ID:
                result = self._equip_inventory_item(
                    inventory,
                    name_id=FLAMBERGE_ID,
                    equip_location=EQUIP_RIGHT_HAND,
                )
                with self._lock:
                    self._weapon_macro_state = "sword_shield"
                    self._last_weapon_macro_action = {
                        "timestamp": time.time(),
                        "action": "shield_triggered_flamberge",
                        "shield_index": equip.get("inventory_index"),
                        "flamberge": result,
                    }
            elif name_id == LANCE_ID:
                with self._lock:
                    self._weapon_macro_state = "lance"
            elif name_id == FLAMBERGE_ID:
                with self._lock:
                    if self._weapon_macro_state != "sword_shield":
                        self._weapon_macro_state = "flamberge"

        skill = world.get("last_client_skill_use") or {}
        skill_stamp = skill.get("timestamp")
        if skill_stamp is not None and float(skill_stamp) != self._last_skill_stamp:
            self._last_skill_stamp = float(skill_stamp)
            skill_id = int(skill.get("skill_id") or 0)
            target_id = int(skill.get("target_id") or 0)
            level = max(1, int(skill.get("skill_level") or 1))
            now = time.time()

            if skill_id == PIERCE_SKILL_ID and target_id > 0:
                # Lance was already injected synchronously by the native send
                # hook immediately before Classic.exe's original Pierce packet.
                # Here we only remember the target so the normal sword+shield
                # loadout can be restored after this exact monster dies.
                with self._lock:
                    self._pierce_target_id = target_id
                    self._weapon_macro_state = "pierce_fight"
                    self._last_weapon_macro_action = {
                        "timestamp": time.time(),
                        "action": "pierce_pre_equipped_native",
                        "target_id": target_id,
                        "skill_level": level,
                    }

        kill_seq = int(live.get("monster_kill_seq") or 0)
        if kill_seq > self._last_kill_seq:
            self._last_kill_seq = kill_seq
            killed = live.get("last_monster_kill") or {}
            killed_id = int(killed.get("actor_id") or 0)
            if (
                self._pierce_target_id is not None
                and killed_id == int(self._pierce_target_id)
            ):
                self._restore_flamberge_shield(
                    inventory,
                    reason="pierce_target_confirmed_dead",
                )

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
                        self._sync_pierce_pre_equip(inventory)
                        self._update_weapon_macro(live, inventory)
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
                "weapon_macro": {
                    "state": self._weapon_macro_state,
                    "pierce_target_id": self._pierce_target_id,
                    "last_action": self._last_weapon_macro_action,
                    "shield_item_id": SHIELD_SLOT1_ID,
                    "flamberge_item_id": FLAMBERGE_ID,
                    "lance_item_id": LANCE_ID,
                    "pierce_skill_id": PIERCE_SKILL_ID,
                },
            }


manual_assistant = ManualAssistant()
