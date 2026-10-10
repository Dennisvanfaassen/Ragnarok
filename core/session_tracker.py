from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any

from diagnostics.authenticated_client import authenticated_client_monitor


class SessionTracker:
    """Always-on session telemetry for both manual and bot play."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.reset()

    def reset(self) -> dict[str, Any]:
        # Take a synchronous live snapshot first. Resetting sequence counters to
        # zero would make the next observer pass re-count all historical kills,
        # EXP events and inventory as if they happened in the new session.
        snapshot = authenticated_client_monitor.snapshot()
        live = snapshot.get("live_state") or {}
        world = live.get("world") or {}
        inventory = self._inventory_by_index(snapshot)

        current_totals: dict[int, int] = {}
        current_names: dict[int, str] = {}
        for row in inventory.values():
            try:
                name_id = int(row.get("name_id") or 0)
            except Exception:
                name_id = 0
            if name_id <= 0:
                continue
            current_totals[name_id] = (
                int(current_totals.get(name_id) or 0)
                + int(row.get("amount") or 0)
            )
            current_names[name_id] = str(
                row.get("name") or f"Item {name_id}"
            )

        with self._lock:
            now = time.time()
            self.started_at = now
            self.last_seen_at = now
            self.kills = 0
            self.deaths = 0
            self.items_found: Counter[str] = Counter()
            self.cards_found: Counter[str] = Counter()
            self.items_used: Counter[str] = Counter()
            self._known_actors: dict[int, dict[str, Any]] = {}
            self._inventory: dict[int, dict[str, Any]] = inventory
            self._inventory_totals: dict[int, int] = current_totals
            self._inventory_names: dict[int, str] = current_names
            self._inventory_baseline_totals: dict[int, int] = dict(current_totals)
            self._inventory_baseline_ready = bool(current_totals or inventory)
            self._last_exp_gain_seq = int(live.get("exp_gain_seq") or 0)
            self._last_combat_stamp: float | None = None
            self._last_combat_target: int | None = None
            self._last_monster_kill_seq = int(live.get("monster_kill_seq") or 0)
            self._last_inventory_gain_seq = int(live.get("inventory_gain_seq") or 0)
            item_use = world.get("last_client_item_use") or {}
            use_stamp = item_use.get("timestamp")
            self._last_item_use_stamp = (
                float(use_stamp) if use_stamp is not None else None
            )
            hp = world.get("hp")
            self._last_hp = int(hp) if hp is not None else None
            base_exp = world.get("base_exp")
            self._last_base_exp = int(base_exp) if base_exp is not None else None
            base_exp_next = world.get("base_exp_next")
            self._last_base_exp_next = (
                int(base_exp_next) if base_exp_next is not None else None
            )
            base_level = world.get("base_level")
            self._last_base_level = int(base_level) if base_level is not None else None
            zeny = world.get("zeny")
            self._last_zeny = int(zeny) if zeny is not None else None
            self.xp_gained = 0
            self.zeny_gained = 0
            self._last_map = str(world.get("map") or "").strip() or None
            self._map_started_at = now
            self._time_by_map: dict[str, float] = {}
        return self.snapshot()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="session-tracker",
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _inventory_by_index(snapshot: dict[str, Any]) -> dict[int, dict[str, Any]]:
        rows = ((snapshot.get("live_state") or {}).get("inventory") or [])
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                out[int(row.get("index"))] = dict(row)
            except Exception:
                continue
        return out

    def _observe(self, snapshot: dict[str, Any]) -> None:
        now = time.time()
        live = snapshot.get("live_state") or {}
        world = live.get("world") or {}
        actors = {
            int(row.get("id")): dict(row)
            for row in (live.get("actors") or [])
            if row.get("id") is not None
        }
        inventory = self._inventory_by_index(snapshot)

        with self._lock:
            self.last_seen_at = now

            current_map = str(world.get("map") or "").strip() or None
            if current_map != self._last_map:
                if self._last_map:
                    self._time_by_map[self._last_map] = (
                        float(self._time_by_map.get(self._last_map) or 0.0)
                        + max(0.0, now - self._map_started_at)
                    )
                self._last_map = current_map
                self._map_started_at = now

            last_combat = world.get("last_combat") or {}
            stamp = last_combat.get("timestamp")
            if stamp is not None and stamp != self._last_combat_stamp:
                self._last_combat_stamp = float(stamp)
                try:
                    self._last_combat_target = int(last_combat.get("target_id"))
                except Exception:
                    self._last_combat_target = None

            # Use confirmed server death events instead of actor-list
            # disappearance. This avoids missing fast kills between polling
            # samples and does not count teleport/out-of-sight removals.
            kill_seq = int(live.get("monster_kill_seq") or 0)
            if kill_seq > self._last_monster_kill_seq:
                self.kills += kill_seq - self._last_monster_kill_seq
                self._last_monster_kill_seq = kill_seq

            # Derive found-item deltas from the same live inventory snapshot
            # used by the dashboard supply counters. Aggregate by name_id so
            # stack merges and inventory-slot changes cannot hide gains.
            current_totals: dict[int, int] = {}
            current_names: dict[int, str] = {}
            for row in inventory.values():
                try:
                    name_id = int(row.get("name_id") or 0)
                except Exception:
                    name_id = 0
                if name_id <= 0:
                    continue
                current_totals[name_id] = (
                    int(current_totals.get(name_id) or 0)
                    + int(row.get("amount") or 0)
                )
                current_names[name_id] = str(
                    row.get("name") or f"Item {name_id}"
                )

            inventory_snapshot_usable = bool(inventory)
            if (
                self._inventory_baseline_ready
                and self._inventory_totals
                and not inventory_snapshot_usable
            ):
                # Inventory list refreshes can briefly expose an empty list.
                # Never treat that transient gap as "all items removed", because
                # the following full refresh would count the entire inventory as
                # newly found loot.
                pass
            else:
                if self._inventory_baseline_ready:
                    for name_id, current_amount in current_totals.items():
                        previous_amount = int(self._inventory_totals.get(name_id) or 0)
                        gained = int(current_amount) - previous_amount
                        if gained <= 0:
                            continue
                        name = current_names.get(name_id) or f"Item {name_id}"
                        self.items_found[name] += gained
                        if "card" in name.lower():
                            self.cards_found[name] += gained

                self._inventory_totals = current_totals
                self._inventory_names = current_names
                self._inventory_baseline_ready = True

            item_use = world.get("last_client_item_use") or {}
            use_stamp = item_use.get("timestamp")
            if use_stamp is not None and use_stamp != self._last_item_use_stamp:
                self._last_item_use_stamp = float(use_stamp)
                name = str(item_use.get("name") or f"Item {item_use.get('name_id') or '?'}")
                self.items_used[name] += 1

            hp = world.get("hp")
            if hp is not None:
                hp = int(hp)
                if self._last_hp is not None and self._last_hp > 0 and hp <= 0:
                    self.deaths += 1
                self._last_hp = hp

            exp_gain_seq = int(live.get("exp_gain_seq") or 0)
            if exp_gain_seq > self._last_exp_gain_seq:
                events = live.get("exp_gain_events") or []
                pending_exp = [
                    row for row in events
                    if int(row.get("seq") or 0) > self._last_exp_gain_seq
                ]
                pending_exp.sort(key=lambda row: int(row.get("seq") or 0))
                for event in pending_exp:
                    if int(event.get("type") or 0) != 1:
                        continue
                    amount = int(event.get("amount") or 0)
                    if amount > 0:
                        self.xp_gained += amount
                self._last_exp_gain_seq = exp_gain_seq

            base_exp = world.get("base_exp")
            base_exp_next = world.get("base_exp_next")
            base_level = world.get("base_level")
            if base_exp is not None and self._last_exp_gain_seq == 0:
                current_exp = int(base_exp)
                current_level = int(base_level) if base_level is not None else self._last_base_level
                if self._last_base_exp is not None:
                    if current_exp >= self._last_base_exp:
                        self.xp_gained += current_exp - self._last_base_exp
                    elif (
                        current_level is not None
                        and self._last_base_level is not None
                        and current_level > self._last_base_level
                        and self._last_base_exp_next is not None
                    ):
                        self.xp_gained += max(
                            0,
                            int(self._last_base_exp_next) - int(self._last_base_exp)
                        ) + max(0, current_exp)
                self._last_base_exp = current_exp
                self._last_base_exp_next = (
                    int(base_exp_next) if base_exp_next is not None else self._last_base_exp_next
                )
                self._last_base_level = current_level

            zeny = world.get("zeny")
            if zeny is not None:
                current_zeny = int(zeny)
                if self._last_zeny is not None and current_zeny > self._last_zeny:
                    self.zeny_gained += current_zeny - self._last_zeny
                self._last_zeny = current_zeny

            self._known_actors = actors
            self._inventory = inventory

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = authenticated_client_monitor.snapshot()
                if snapshot.get("classic_pid"):
                    self._observe(snapshot)
            except Exception:
                pass
            self._stop.wait(0.25)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            elapsed = max(0.0, now - self.started_at)
            hours = elapsed / 3600.0
            current_map_elapsed = (
                max(0.0, now - self._map_started_at)
                if self._last_map else 0.0
            )
            per_map = dict(self._time_by_map)
            if self._last_map:
                per_map[self._last_map] = (
                    float(per_map.get(self._last_map) or 0.0)
                    + current_map_elapsed
                )

            return {
                "status": "running",
                "started_at": self.started_at,
                "elapsed_seconds": round(elapsed, 1),
                "current_map": self._last_map,
                "time_on_current_map_seconds": round(current_map_elapsed, 1),
                "time_by_map_seconds": {
                    k: round(v, 1) for k, v in sorted(per_map.items())
                },
                "kills": self.kills,
                "monsters_per_hour": round(self.kills / hours, 1) if hours > 0 else 0.0,
                "deaths": self.deaths,
                "items_found_total": int(sum(self.items_found.values())),
                "items_found": [
                    {
                        "name": name,
                        "amount": amount,
                        "current_amount": int(sum(
                            qty
                            for name_id, qty in self._inventory_totals.items()
                            if self._inventory_names.get(name_id) == name
                        )),
                    }
                    for name, amount in self.items_found.most_common(30)
                ],
                "inventory_current": [
                    {
                        "name_id": int(name_id),
                        "name": self._inventory_names.get(name_id) or f"Item {name_id}",
                        "amount": int(amount),
                        "session_start_amount": int(
                            self._inventory_baseline_totals.get(name_id) or 0
                        ),
                    }
                    for name_id, amount in sorted(
                        self._inventory_totals.items(),
                        key=lambda row: (
                            (self._inventory_names.get(row[0]) or "").lower(),
                            row[0],
                        ),
                    )
                ],
                "cards_found_total": int(sum(self.cards_found.values())),
                "cards_found": [
                    {"name": name, "amount": amount}
                    for name, amount in self.cards_found.most_common(20)
                ],
                "items_used_total": int(sum(self.items_used.values())),
                "items_used": [
                    {"name": name, "amount": amount}
                    for name, amount in self.items_used.most_common(20)
                ],
                "xp_gained": int(self.xp_gained),
                "xp_per_hour": round(self.xp_gained / hours, 1) if hours > 0 else 0.0,
                "zeny_gained": int(self.zeny_gained),
                "zeny_per_hour": round(self.zeny_gained / hours, 1) if hours > 0 else 0.0,
                "loot_value": None,
                "loot_value_note": "Item pricing is not configured yet.",
            }


session_tracker = SessionTracker()
