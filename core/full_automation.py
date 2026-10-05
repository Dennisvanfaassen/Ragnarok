from __future__ import annotations

import threading
import time
from typing import Any

from core.active_control import active_hunt_controller
from core.game_actions import game_actions
from core.openkore_data import item_id_for_name
from core.pathing import astar, nav_repository
from core.state import app_state
from core.town_services import (
    AWAKENING_POTION_ID,
    BUTTERFLY_WING_ID,
    town_service_registry,
)
from core.town_travel import town_travel_controller
from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.native_action_bridge import native_action_bridge


MEAT_ID = 517
FLY_WING_ID = 601


class FullAutomationController:
    """End-to-end hunt -> town -> storage -> restock -> hunt controller."""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._force_cycle = threading.Event()
        self.running = False
        self.phase = "IDLE"
        self.message = "Idle"
        self.last_error: str | None = None
        self.saved_town_map: str | None = None
        self.current_service: dict[str, Any] | None = None
        self._butterfly_used_this_cycle = False
        self._deposited_indices_this_cycle: set[int] = set()
        self._sold_indices_this_cycle: set[int] = set()
        self.actions: list[dict[str, Any]] = []

    def _log(self, action: str, **details):
        with self._lock:
            self.actions.append({"time": time.time(), "action": action, **details})
            self.actions = self.actions[-100:]

    def _set(self, phase: str, message: str):
        with self._lock:
            self.phase = phase
            self.message = message
        app_state.patch_runtime(
            current_action=phase.replace("_", " ").title(),
            message=message,
        )
        self._log("phase", phase=phase, message=message)

    @staticmethod
    def _snapshot() -> dict[str, Any]:
        return authenticated_client_monitor.snapshot()

    @staticmethod
    def _world(snapshot: dict[str, Any]) -> dict[str, Any]:
        return (snapshot.get("live_state") or {}).get("world") or {}

    @staticmethod
    def _position(snapshot: dict[str, Any]) -> tuple[int, int] | None:
        world = FullAutomationController._world(snapshot)
        x, y = world.get("x"), world.get("y")
        if x is None or y is None:
            return None
        return int(x), int(y)

    def _ensure_native_ready(self, timeout: float = 8.0) -> bool:
        state = native_action_bridge.snapshot()
        if not state.get("attached"):
            try:
                native_action_bridge.start()
            except Exception as exc:
                self.last_error = f"Could not start native action bridge: {exc}"
                return False

        deadline = time.time() + timeout
        while not self._stop.is_set() and time.time() < deadline:
            state = native_action_bridge.snapshot()
            if (
                state.get("attached")
                and state.get("status") == "ready"
                and (state.get("agent") or {}).get("socket_learned")
            ):
                return True
            self._stop.wait(0.10)

        self.last_error = (
            "Native action bridge is attached but has not learned the map socket yet. "
            "Move or attack once in Classic.exe."
        )
        return False

    def _wait_map_change(self, before: str, timeout: float = 12.0) -> str | None:
        deadline = time.time() + timeout
        while not self._stop.is_set() and time.time() < deadline:
            current = str(self._world(self._snapshot()).get("map") or "")
            if current and current != before:
                return current
            self._stop.wait(0.10)
        return None

    def _walk_same_map(
        self,
        map_name: str,
        goal: tuple[int, int],
        *,
        tolerance: int = 4,
        timeout: float = 60.0,
    ) -> bool:
        snapshot = self._snapshot()
        player = self._position(snapshot)
        if player is None:
            return False
        if str(self._world(snapshot).get("map") or "") != map_name:
            return False
        try:
            grid, _ = nav_repository.load(map_name)
            path = astar(grid, player, goal, max_expansions=180000)
        except Exception:
            path = None
        if not path:
            self.last_error = f"No route on {map_name} to {goal[0]},{goal[1]}"
            return False

        deadline = time.time() + timeout
        while not self._stop.is_set() and time.time() < deadline:
            snapshot = self._snapshot()
            if str(self._world(snapshot).get("map") or "") != map_name:
                return True
            player = self._position(snapshot)
            if player is None:
                self._stop.wait(0.05)
                continue
            if max(abs(player[0] - goal[0]), abs(player[1] - goal[1])) <= tolerance:
                game_actions.release_hold_move()
                return True

            best_i = min(
                range(len(path)),
                key=lambda i: max(
                    abs(path[i][0] - player[0]),
                    abs(path[i][1] - player[1]),
                ),
            )
            destination = path[min(len(path) - 1, best_i + 9)]
            if game_actions.native_move_ready():
                result = game_actions.move_to(destination)
            else:
                result = game_actions.move(
                    player,
                    destination,
                )
            if not result.get("ok"):
                self._stop.wait(0.15)
            else:
                self._stop.wait(0.10)

        game_actions.release_hold_move()
        self.last_error = f"Timed out walking to {goal[0]},{goal[1]}"
        return False

    def _travel_to_map(self, target_map: str) -> bool:
        current = str(self._world(self._snapshot()).get("map") or "")
        if current == target_map:
            return True
        try:
            town_travel_controller.start_to_map(target_map)
        except Exception as exc:
            self.last_error = str(exc)
            return False

        while not self._stop.is_set():
            state = town_travel_controller.snapshot()
            if state.get("state") == "ARRIVED":
                return True
            if state.get("state") == "ERROR":
                self.last_error = str(state.get("message") or "Travel failed")
                return False
            self._stop.wait(0.15)
        return False

    def _find_actor_near(
        self,
        point: tuple[int, int],
        *,
        name_contains: str | None = None,
        max_distance: int = 12,
    ) -> dict[str, Any] | None:
        live = self._snapshot().get("live_state") or {}
        candidates = []
        for actor in live.get("actors") or []:
            if actor.get("kind") != "other":
                continue
            x, y = actor.get("x"), actor.get("y")
            if x is None or y is None:
                continue
            if name_contains:
                if name_contains.lower() not in str(actor.get("name") or "").lower():
                    continue
            d = max(abs(int(x) - point[0]), abs(int(y) - point[1]))
            if d <= max_distance:
                candidates.append((d, actor))
        if not candidates:
            return None
        candidates.sort(key=lambda row: row[0])
        return dict(candidates[0][1])

    def _service(self, kind: str) -> dict[str, Any] | None:
        snapshot = self._snapshot()
        town_service_registry.observe_live(snapshot)
        world = self._world(snapshot)
        current_map = str(world.get("map") or "")
        pos = self._position(snapshot)
        return town_service_registry.nearest(kind, current_map, pos)

    def _go_to_service(self, kind: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        service = self._service(kind)
        if service is None:
            self.last_error = f"No known {kind.replace('_', ' ')} reachable from current map."
            return None
        self.current_service = service
        target_map = str(service["map"])
        if not self._travel_to_map(target_map):
            return None
        goal = (int(service["x"]), int(service["y"]))
        if not self._walk_same_map(target_map, goal):
            return None
        town_service_registry.observe_live(self._snapshot())

        actor = self._find_actor_near(
            goal,
            name_contains=("kafra" if kind == "kafra" else None),
            max_distance=12,
        )
        if actor is None:
            # Shop coordinates from OpenKore are authoritative enough to pick
            # the nearest NPC actor at the exact shop location.
            actor = self._find_actor_near(goal, max_distance=12)
        if actor is None:
            self.last_error = (
                f"{kind.replace('_', ' ').title()} NPC not visible near "
                f"{target_map} {goal[0]},{goal[1]}"
            )
            return None
        return service, actor

    def _use_butterfly_wing(self) -> bool:
        items = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
        wing = next(
            (
                row for row in items
                if int(row.get("name_id") or -1) == BUTTERFLY_WING_ID
                or str(row.get("name") or "").lower() == "butterfly wing"
            ),
            None,
        )
        if wing is None:
            self.last_error = "No Butterfly Wing found in live inventory."
            return False

        world = self._world(self._snapshot())
        target_id = world.get("self_account_id") or world.get("self_char_id")
        if target_id is None:
            self.last_error = "Character target ID is not known."
            return False

        before_map = str(world.get("map") or "")
        result = native_action_bridge.item_use(int(wing["index"]), int(target_id))
        self._log("use_butterfly_wing", item=wing, result=result)
        if not result.get("ok"):
            self.last_error = f"Butterfly Wing use failed: {result.get('reason')}"
            return False

        landed = self._wait_map_change(before_map)
        if not landed:
            self.last_error = "Butterfly Wing did not produce a map change."
            return False
        self.saved_town_map = landed
        self._butterfly_used_this_cycle = True
        self._log("butterfly_landed", map=landed)
        return True

    def _open_kafra_storage(self, actor_id: int) -> bool:
        before = authenticated_client_monitor.item_state_snapshot().get("updated_at", {}).get("storage")
        steps = [
            ("talk", lambda: native_action_bridge.talk_npc(actor_id, 1)),
            ("continue", lambda: native_action_bridge.continue_npc(actor_id)),
            ("storage_option", lambda: native_action_bridge.choose_npc_option(actor_id, 2)),
            # The observed SoulBound Kafra flow closes the dialogue immediately
            # after selecting Storage; the storage window remains active.
            ("close_dialog", lambda: native_action_bridge.close_npc(actor_id)),
        ]
        for name, fn in steps:
            result = fn()
            self._log("kafra_dialog", step=name, result=result)
            if not result.get("ok"):
                self.last_error = f"Kafra dialogue failed at {name}: {result.get('reason')}"
                return False
            self._stop.wait(0.30)

        deadline = time.time() + 5.0
        while not self._stop.is_set() and time.time() < deadline:
            after = authenticated_client_monitor.item_state_snapshot().get("updated_at", {}).get("storage")
            if after is not None and after != before:
                return True
            self._stop.wait(0.10)
        self.last_error = "Storage list did not open after Kafra dialogue."
        return False

    @staticmethod
    def _town_item_rule(item: dict[str, Any]):
        profile = app_state.get_profile()
        name_id = int(item.get("name_id") or -1)
        name = str(item.get("name") or "").strip().casefold()
        for rule in profile.town.item_rules:
            if rule.name_id is not None and int(rule.name_id) == name_id:
                return rule
            if str(rule.item_name or "").strip().casefold() == name and name:
                return rule
        return None

    def _town_item_action(self, item: dict[str, Any]) -> str:
        rule = self._town_item_rule(item)
        if rule is not None:
            action = str(rule.action or "store").strip().lower()
            return action if action in {"keep", "store", "sell"} else "store"

        # Preserve core consumables unless the user explicitly creates a rule
        # for them in the Selling / Storage tab.
        keep_ids = {
            AWAKENING_POTION_ID,
            517,                  # Meat
            601,                  # Fly Wing
            BUTTERFLY_WING_ID,
        }
        name_id = int(item.get("name_id") or -1)
        healing_name = str(app_state.get_profile().healing.item or "").strip().casefold()
        item_name = str(item.get("name") or "").strip().casefold()
        if name_id in keep_ids or (healing_name and item_name == healing_name):
            return "keep"

        default_action = str(
            app_state.get_profile().town.default_item_action or "store"
        ).strip().lower()
        return default_action if default_action in {"keep", "store"} else "store"

    def _deposit_all_unequipped(self) -> bool:
        state = authenticated_client_monitor.item_state_snapshot()
        items = list(state.get("inventory") or [])
        deposited = 0
        kept = 0
        reserved_for_sale = 0
        self._deposited_indices_this_cycle.clear()

        for item in items:
            equipped = int(item.get("equipped") or 0)
            index = int(item.get("index") or -1)
            amount = int(item.get("amount") or 0)
            if equipped != 0:
                kept += 1
                self._log(
                    "storage_keep_equipped",
                    index=index,
                    name=item.get("name"),
                    equipped=equipped,
                )
                continue
            if index < 0 or amount <= 0:
                continue

            action = self._town_item_action(item)
            if action == "keep":
                kept += 1
                self._log(
                    "town_keep_item",
                    index=index,
                    name=item.get("name"),
                    name_id=item.get("name_id"),
                    amount=amount,
                )
                continue
            if action == "sell":
                reserved_for_sale += 1
                self._log(
                    "town_reserve_for_sale",
                    index=index,
                    name=item.get("name"),
                    name_id=item.get("name_id"),
                    amount=amount,
                )
                continue

            before_amount = int(item.get("amount") or 0)
            result = native_action_bridge.storage_add(index, amount)
            self._log(
                "storage_deposit",
                index=index,
                name=item.get("name"),
                name_id=item.get("name_id"),
                amount=amount,
                stackable=item.get("stackable"),
                result=result,
            )
            if not result.get("ok"):
                self.last_error = (
                    f"Storage deposit failed for {item.get('name') or index}: "
                    f"{result.get('reason')}"
                )
                return False

            # Bytes-sent only means the request reached Classic.exe's socket.
            # Wait for the server to confirm the inventory amount changed before
            # advancing to the next item or closing storage.
            confirmed = False
            deadline = time.time() + 2.5
            while not self._stop.is_set() and time.time() < deadline:
                live_inventory = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
                current = next(
                    (
                        row for row in live_inventory
                        if int(row.get("index") or -1) == index
                    ),
                    None,
                )
                current_amount = int(current.get("amount") or 0) if current is not None else 0
                if current is None or current_amount < before_amount:
                    confirmed = True
                    break
                self._stop.wait(0.08)

            self._log(
                "storage_deposit_confirmation",
                index=index,
                name=item.get("name"),
                confirmed=confirmed,
            )
            if not confirmed:
                self.last_error = (
                    f"Storage did not confirm moving {item.get('name') or index}. "
                    "Storage will remain open so the item is not silently skipped."
                )
                return False

            self._deposited_indices_this_cycle.add(index)
            deposited += 1
            self._stop.wait(0.12)

        self._log(
            "storage_deposit_complete",
            deposited=deposited,
            kept=kept,
            reserved_for_sale=reserved_for_sale,
        )
        return True

    def _sell_configured_items(self, actor_id: int) -> bool:
        inventory = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
        rows: list[dict[str, int]] = []
        details: list[dict[str, Any]] = []
        self._sold_indices_this_cycle.clear()

        for item in inventory:
            index = int(item.get("index") or -1)
            amount = int(item.get("amount") or 0)
            equipped = int(item.get("equipped") or 0)
            if (
                index < 0
                or amount <= 0
                or equipped != 0
                or index in self._deposited_indices_this_cycle
            ):
                continue
            if self._town_item_action(item) != "sell":
                continue
            rows.append({"index": index, "amount": amount})
            details.append({
                "index": index,
                "amount": amount,
                "name": item.get("name"),
                "name_id": item.get("name_id"),
            })

        if not rows:
            self._log("sell_not_needed")
            return True

        result = native_action_bridge.talk_npc(actor_id, 1)
        self._log("tool_dealer_talk_for_sell", result=result)
        if not result.get("ok"):
            self.last_error = f"Tool Dealer talk for selling failed: {result.get('reason')}"
            return False
        self._stop.wait(0.25)

        result = native_action_bridge.request_npc_sell(actor_id)
        self._log("tool_dealer_sell_list", result=result)
        if not result.get("ok"):
            self.last_error = f"Could not request Tool Dealer sell list: {result.get('reason')}"
            return False
        self._stop.wait(0.35)

        result = native_action_bridge.sell_bulk(rows)
        self._log("tool_dealer_sell", items=details, result=result)
        if not result.get("ok"):
            self.last_error = f"Configured item sale failed: {result.get('reason')}"
            return False

        self._sold_indices_this_cycle.update(row["index"] for row in rows)
        self._stop.wait(0.35)
        return True

    def _configured_buy_targets(self) -> list[dict[str, Any]]:
        profile = app_state.get_profile()
        rules = list(profile.town.buy_rules or [])
        inventory = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
        desired: list[dict[str, Any]] = []
        for rule in rules:
            target = max(0, int(rule.target_quantity or 0))
            if target <= 0:
                continue
            item_id = int(rule.name_id) if rule.name_id is not None else item_id_for_name(rule.item_name)
            if item_id is None:
                self._log(
                    "buy_rule_unresolved",
                    item_name=rule.item_name,
                    target_quantity=target,
                )
                continue

            current = sum(
                int(row.get("amount") or 0)
                for row in inventory
                if int(row.get("name_id") or -1) == int(item_id)
                and int(row.get("index") or -1) not in self._sold_indices_this_cycle
                and int(row.get("index") or -1) not in self._deposited_indices_this_cycle
            )
            if int(item_id) == BUTTERFLY_WING_ID and self._butterfly_used_this_cycle:
                current = max(0, current - 1)
            amount = max(0, target - current)
            if amount > 0:
                desired.append({
                    "item_id": int(item_id),
                    "amount": amount,
                    "item_name": str(rule.item_name or ""),
                    "target_quantity": target,
                    "current_quantity": current,
                })
        return desired

    def _restock(self, actor_id: int) -> bool:
        desired = self._configured_buy_targets()
        if not desired:
            self._log("restock_not_needed")
            return True

        result = native_action_bridge.talk_npc(actor_id, 1)
        self._log("tool_dealer_talk_for_buy", result=result)
        if not result.get("ok"):
            self.last_error = f"Tool Dealer talk failed: {result.get('reason')}"
            return False
        self._stop.wait(0.25)

        result = native_action_bridge.request_npc_buy(actor_id)
        self._log("tool_dealer_buy_list", result=result)
        if not result.get("ok"):
            self.last_error = f"Could not request Tool Dealer buy list: {result.get('reason')}"
            return False
        self._stop.wait(0.35)

        packet_rows = [
            {"item_id": row["item_id"], "amount": row["amount"]}
            for row in desired
        ]
        result = native_action_bridge.buy_bulk(packet_rows)
        self._log("tool_dealer_buy", items=desired, result=result)
        if not result.get("ok"):
            self.last_error = f"Configured purchase failed: {result.get('reason')}"
            return False
        return True

    def _town_cycle(
        self,
        *,
        forced: bool = False,
        resume_hunt: bool = True,
        trigger_reason: str | None = None,
    ) -> bool:
        profile = app_state.get_profile()
        self._deposited_indices_this_cycle.clear()
        self._sold_indices_this_cycle.clear()
        active_hunt_controller.stop()
        game_actions.release_hold_move()

        if not self._ensure_native_ready():
            return False

        current_map = str(self._world(self._snapshot()).get("map") or "").strip().lower()
        hunt_map = str(profile.hunt.map or "").strip().lower()

        # Manual/test town cycles should work when the character is already in
        # town. If we are not on the configured hunt map, treat the current map
        # as the town starting point and skip wasting a Butterfly Wing.
        already_in_town = bool(forced and current_map and current_map != hunt_map)
        if already_in_town:
            self.saved_town_map = current_map
            self._butterfly_used_this_cycle = False
            self._set(
                "RETURNING",
                f"Town-cycle test starting from current map {current_map}; Butterfly Wing skipped.",
            )
            self._log("town_cycle_start_in_place", map=current_map)
        else:
            reason = trigger_reason or (
                f"{profile.town.return_weight_percent}% weight reached"
            )
            self._set(
                "RETURNING",
                f"{reason}; returning with Butterfly Wing.",
            )
            if not self._use_butterfly_wing():
                return False

        self._set("FINDING_KAFRA", f"Finding nearest Kafra from {self.saved_town_map}.")
        found = self._go_to_service("kafra")
        if found is None:
            return False
        _, kafra = found

        self._set("STORING", "Opening Kafra storage and depositing loot.")
        if not self._open_kafra_storage(int(kafra["id"])):
            return False
        if not self._deposit_all_unequipped():
            return False

        self._set(
            "CLOSING_STORAGE",
            "Storage complete; closing Kafra storage before going to the Tool Dealer.",
        )
        close_result = native_action_bridge.storage_close()
        self._log("storage_close", result=close_result)
        if not close_result.get("ok"):
            self.last_error = (
                f"Could not close Kafra storage: {close_result.get('reason')}"
            )
            return False
        self._stop.wait(0.35)

        self._set("FINDING_TOOL_DEALER", "Finding nearest Tool Dealer.")
        found = self._go_to_service("tool_dealer")
        if found is None:
            return False
        _, dealer = found

        self._set("SELLING", "Selling items configured for sale.")
        if not self._sell_configured_items(int(dealer["id"])):
            return False

        self._set("RESTOCKING", "Buying configured town supplies.")
        if not self._restock(int(dealer["id"])):
            return False
        self._butterfly_used_this_cycle = False

        hunt_map = str(profile.hunt.map or "").strip().lower()
        if not resume_hunt:
            self._set("TOWN_TEST_COMPLETE", "Town-cycle test completed.")
            return True

        if not hunt_map:
            self.last_error = "No hunt map configured."
            return False

        self._set("RETURNING_TO_HUNT", f"Returning to {hunt_map}.")
        if not self._travel_to_map(hunt_map):
            return False

        self._set("RESUMING_HUNT", f"Arrived at {hunt_map}; resuming hunting.")
        active_hunt_controller.start({})
        return True

    def _automatic_town_trigger(
        self,
        *,
        weight_percent: float | int | None,
        threshold: int,
    ) -> str | None:
        profile = app_state.get_profile()

        if (
            weight_percent is not None
            and float(weight_percent) >= float(threshold)
        ):
            return f"{float(weight_percent):.1f}% weight reached (limit {threshold}%)"

        item_state = authenticated_client_monitor.item_state_snapshot()
        # Do not treat an uninitialized/unknown inventory as empty.
        if (item_state.get("updated_at") or {}).get("inventory") is None:
            return None

        inventory = item_state.get("inventory") or []
        meat_amount = sum(
            int(row.get("amount") or 0)
            for row in inventory
            if int(row.get("name_id") or -1) == MEAT_ID
        )
        fly_wing_amount = sum(
            int(row.get("amount") or 0)
            for row in inventory
            if int(row.get("name_id") or -1) == FLY_WING_ID
        )

        if profile.town.return_when_out_of_meat and meat_amount <= 0:
            return "Out of Meat"
        if profile.town.return_when_out_of_fly_wings and fly_wing_amount <= 0:
            return "Out of Fly Wings"
        return None

    def _loop(self):
        try:
            profile = app_state.get_profile()
            hunt_map = str(profile.hunt.map or "").strip().lower()
            if not hunt_map:
                raise RuntimeError("Choose a hunt map in RO Control first.")

            current_map = str(self._world(self._snapshot()).get("map") or "").strip().lower()
            if current_map != hunt_map:
                self._set("TRAVEL_TO_HUNT", f"Travelling to configured hunt map {hunt_map}.")
                if not self._travel_to_map(hunt_map):
                    raise RuntimeError(self.last_error or f"Could not reach {hunt_map}.")

            if not active_hunt_controller.snapshot().get("running"):
                active_hunt_controller.start({})
            self._set("HUNTING", f"Hunting on {hunt_map}.")

            while not self._stop.is_set():
                snapshot = self._snapshot()
                town_service_registry.observe_live(snapshot)
                world = self._world(snapshot)
                profile = app_state.get_profile()
                weight_percent = world.get("weight_percent")
                threshold = max(1, min(99, int(profile.town.return_weight_percent)))

                forced_cycle = self._force_cycle.is_set()
                automatic_reason = self._automatic_town_trigger(
                    weight_percent=weight_percent,
                    threshold=threshold,
                )
                if forced_cycle or automatic_reason is not None:
                    self._force_cycle.clear()
                    trigger_reason = (
                        "Manual town-cycle request"
                        if forced_cycle
                        else automatic_reason
                    )
                    self._log(
                        "town_cycle_triggered",
                        reason=trigger_reason,
                        weight_percent=weight_percent,
                    )
                    if not self._town_cycle(
                        forced=forced_cycle,
                        resume_hunt=True,
                        trigger_reason=trigger_reason,
                    ):
                        self._set(
                            "PAUSED",
                            self.last_error or "Town cycle failed.",
                        )
                        return
                    self._set("HUNTING", "Town cycle complete; hunting resumed.")

                self._stop.wait(0.20)
        except Exception as exc:
            self.last_error = str(exc)
            self._set("ERROR", str(exc))
        finally:
            self.running = False

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return self.snapshot()
            if not authenticated_client_monitor.snapshot().get("classic_pid"):
                raise RuntimeError("Classic.exe is not detected.")
            native = native_action_bridge.snapshot()
            if not native.get("attached"):
                native_action_bridge.start()
            self._stop.clear()
            self._force_cycle.clear()
            self.running = True
            self.last_error = None
            self.saved_town_map = None
            self.current_service = None
            self._butterfly_used_this_cycle = False
            self._deposited_indices_this_cycle.clear()
            self._sold_indices_this_cycle.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return self.snapshot()

    def _run_standalone_town_cycle_test(self):
        try:
            if not self._town_cycle(forced=True, resume_hunt=False):
                self._set("PAUSED", self.last_error or "Town-cycle test failed.")
        except Exception as exc:
            self.last_error = str(exc)
            self._set("ERROR", str(exc))
        finally:
            self.running = False

    def force_town_cycle(self) -> dict[str, Any]:
        if self.running:
            self._force_cycle.set()
            self._log("force_town_cycle_requested")
            return self.snapshot()

        if not authenticated_client_monitor.snapshot().get("classic_pid"):
            raise RuntimeError("Classic.exe is not detected.")

        self._stop.clear()
        self.last_error = None
        self.running = True
        self._thread = threading.Thread(
            target=self._run_standalone_town_cycle_test,
            daemon=True,
            name="standalone-town-cycle-test",
        )
        self._thread.start()
        self._log("standalone_town_cycle_requested")
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        town_travel_controller.stop()
        active_hunt_controller.stop()
        game_actions.release_hold_move()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self.running = False
        self.phase = "IDLE"
        self.message = "Full automation stopped."
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        profile = app_state.get_profile()
        world = self._world(self._snapshot())
        item_state = authenticated_client_monitor.item_state_snapshot()
        inventory = item_state.get("inventory") or []
        meat_amount = sum(
            int(row.get("amount") or 0)
            for row in inventory
            if int(row.get("name_id") or -1) == MEAT_ID
        )
        fly_wing_amount = sum(
            int(row.get("amount") or 0)
            for row in inventory
            if int(row.get("name_id") or -1) == FLY_WING_ID
        )
        with self._lock:
            return {
                "running": self.running,
                "phase": self.phase,
                "message": self.message,
                "last_error": self.last_error,
                "saved_town_map": self.saved_town_map,
                "current_service": self.current_service,
                "hunt_map": profile.hunt.map,
                "monsters": profile.hunt.monsters,
                "loot_all": profile.hunt.loot_all,
                "weight_percent": world.get("weight_percent"),
                "return_weight_percent": profile.town.return_weight_percent,
                "return_when_out_of_meat": profile.town.return_when_out_of_meat,
                "return_when_out_of_fly_wings": profile.town.return_when_out_of_fly_wings,
                "meat_amount": meat_amount,
                "fly_wing_amount": fly_wing_amount,
                "healing": {
                    "enabled": profile.healing.enabled,
                    "hotkey": profile.healing.hotkey,
                    "hp_below_percent": profile.healing.hp_below_percent,
                },
                "supplies": profile.town.supplies.model_dump(),
                "town_item_rules": [row.model_dump() for row in profile.town.item_rules],
                "town_buy_rules": [row.model_dump() for row in profile.town.buy_rules],
                "default_item_action": profile.town.default_item_action,
                "services": town_service_registry.snapshot(),
                "actions": self.actions[-30:],
            }


full_automation_controller = FullAutomationController()
