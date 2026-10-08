from __future__ import annotations

import random
import threading
import time
from typing import Any

from core.state import app_state
from core.active_control import active_hunt_controller
from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.native_action_bridge import native_action_bridge


MEAT_ID = 517
FLY_WING_ID = 601


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
        return self.snapshot()

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

        meat = sum(
            int(row.get("amount") or 0)
            for row in inventory
            if int(row.get("name_id") or -1) == MEAT_ID
            or str(row.get("name") or "").strip().lower() == "meat"
        )
        if meat <= int(settings.warn_meat_below):
            warnings.append({
                "key": "low_meat",
                "level": "warning",
                "message": f"Meat is low: {meat} left.",
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

        if profile.aspd.enabled and profile.aspd.status_effect_id is not None:
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
                        self._maybe_heal(world, inventory)
                        warnings = self._build_warnings(world, inventory, character)
                        with self._lock:
                            self._warnings = warnings
                            self._last_warning_keys = {
                                str(row.get("key")) for row in warnings
                            }
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
            }


manual_assistant = ManualAssistant()
