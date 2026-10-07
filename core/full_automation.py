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

        snapshot = self._snapshot()
        player = self._position(snapshot)
        live = snapshot.get("live_state") or {}
        actor = next(
            (
                row for row in (live.get("actors") or [])
                if int(row.get("id") or -1) == int(actor_id)
            ),
            None,
        )
        if player is None or actor is None or actor.get("x") is None or actor.get("y") is None:
            self.last_error = "Kafra is no longer visible for physical interaction."
            return False

        target = (int(actor["x"]), int(actor["y"]))

        # A Windows click being sent does not prove Ragnarok actually opened
        # the NPC dialogue. Use the verified NPC-talk action for this first
        # interaction only, then use real keyboard input for every dialogue
        # step so the server sees human-paced menu progression.
        talk_result = native_action_bridge.talk_npc(actor_id, 1)
        self._log(
            "kafra_interaction",
            step="talk_kafra",
            target={"x": target[0], "y": target[1]},
            result=talk_result,
        )
        if not talk_result.get("ok"):
            self.last_error = f"Could not start Kafra dialogue: {talk_result.get('reason')}"
            return False

        self._stop.wait(0.50)

        first_enter = game_actions.press_hotkey("ENTER")
        self._log("kafra_physical_input", step="advance_initial_dialog", result=first_enter)
        if not first_enter.get("ok"):
            self.last_error = f"Could not advance initial Kafra dialogue: {first_enter.get('reason')}"
            return False

        self._stop.wait(0.30)

        down = game_actions.press_hotkey("DOWN")
        self._log("kafra_physical_input", step="arrow_down_to_storage", result=down)
        if not down.get("ok"):
            self.last_error = f"Could not press Down in Kafra menu: {down.get('reason')}"
            return False

        self._stop.wait(0.10)

        enter_storage = game_actions.press_hotkey("ENTER")
        self._log("kafra_physical_input", step="enter_storage_option", result=enter_storage)
        if not enter_storage.get("ok"):
            self.last_error = f"Could not confirm Kafra storage option: {enter_storage.get('reason')}"
            return False

        self._stop.wait(0.30)

        final_enter = game_actions.press_hotkey("ENTER")
        self._log("kafra_physical_input", step="close_final_kafra_dialog", result=final_enter)
        if not final_enter.get("ok"):
            self.last_error = f"Could not close final Kafra dialogue: {final_enter.get('reason')}"
            return False

        # Wait for the real storage list update before moving any inventory
        # stack. This is the hard synchronization point that prevents commands
        # from racing the still-open NPC dialogue.
        deadline = time.time() + 6.0
        storage_opened = False
        while not self._stop.is_set() and time.time() < deadline:
            after = authenticated_client_monitor.item_state_snapshot().get("updated_at", {}).get("storage")
            if after is not None and after != before:
                storage_opened = True
                break
            self._stop.wait(0.10)

        if not storage_opened:
            self.last_error = "Storage list did not open after the physical Kafra dialogue sequence."
            return False

        # Give the client/server item state a short settle window after the
        # storage list arrives. Manual Classic.exe interaction naturally has
        # this delay; sending a storage move immediately after open can use a
        # stale pre-storage inventory amount.
        settle_deadline = time.time() + 1.20
        last_inventory_update = (
            authenticated_client_monitor.item_state_snapshot()
            .get("updated_at", {})
            .get("inventory")
        )
        stable_since = time.time()
        while not self._stop.is_set() and time.time() < settle_deadline:
            current_update = (
                authenticated_client_monitor.item_state_snapshot()
                .get("updated_at", {})
                .get("inventory")
            )
            if current_update != last_inventory_update:
                last_inventory_update = current_update
                stable_since = time.time()
            if time.time() - stable_since >= 0.45:
                break
            self._stop.wait(0.08)

        self._log(
            "storage_ready",
            storage_updated_at=(
                authenticated_client_monitor.item_state_snapshot()
                .get("updated_at", {})
                .get("storage")
            ),
            inventory_updated_at=last_inventory_update,
        )
        return True

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

    @staticmethod
    def _storage_item_identity(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(item.get("name_id") or -1),
            str(item.get("cards_hex") or ""),
            int(item.get("upgrade") or 0),
            int(item.get("expire") or 0),
            int(item.get("bind_on_equip_type") or 0),
            bool(item.get("stackable", True)),
        )

    def _resolve_live_inventory_item(
        self,
        planned: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Resolve a planned item against the current protocol inventory list.

        Storage moves can cause inventory packet refreshes/reordering. Never trust
        the original list position after another stack has moved; prefer the same
        protocol index only when its identity still matches, otherwise resolve an
        equivalent live item by its wire-level identity.
        """
        inventory = authenticated_client_monitor.item_state_snapshot().get("inventory") or []
        planned_index = int(planned.get("index") or -1)
        identity = self._storage_item_identity(planned)

        exact = next(
            (
                row for row in inventory
                if int(row.get("index") or -1) == planned_index
                and self._storage_item_identity(row) == identity
            ),
            None,
        )
        if exact is not None:
            return exact

        matches = [
            row for row in inventory
            if self._storage_item_identity(row) == identity
            and int(row.get("amount") or 0) > 0
        ]
        if len(matches) == 1:
            return matches[0]

        # Stackable consumables/materials are uniquely represented by nameID on
        # this client. For duplicate non-stackable equipment, never guess.
        if bool(planned.get("stackable", True)):
            name_id = int(planned.get("name_id") or -1)
            same_id = [
                row for row in inventory
                if int(row.get("name_id") or -1) == name_id
                and int(row.get("amount") or 0) > 0
            ]
            if len(same_id) == 1:
                return same_id[0]
        return None

    @staticmethod
    def _storage_total_for_name_id(
        state: dict[str, Any],
        name_id: int,
    ) -> int:
        return sum(
            int(row.get("amount") or 0)
            for row in (state.get("storage") or [])
            if int(row.get("name_id") or -1) == int(name_id)
        )

    def _deposit_all_unequipped(self) -> bool:
        state = authenticated_client_monitor.item_state_snapshot()
        items = list(state.get("inventory") or [])
        deposited = 0
        deposited_units = 0
        kept = 0
        reserved_for_sale = 0
        self._deposited_indices_this_cycle.clear()

        self._log(
            "storage_plan",
            items=[
                {
                    "index": item.get("index"),
                    "name": item.get("name"),
                    "name_id": item.get("name_id"),
                    "amount": item.get("amount"),
                    "equipped": item.get("equipped"),
                    "action": (
                        "keep"
                        if int(item.get("equipped") or 0) != 0
                        else self._town_item_action(item)
                    ),
                }
                for item in items
            ],
        )

        for planned in items:
            equipped = int(planned.get("equipped") or 0)
            planned_index = int(planned.get("index") or -1)
            planned_amount = int(planned.get("amount") or 0)

            if equipped != 0:
                kept += 1
                self._log(
                    "storage_keep_equipped",
                    index=planned_index,
                    name=planned.get("name"),
                    equipped=equipped,
                )
                continue
            if planned_index < 0 or planned_amount <= 0:
                continue

            action = self._town_item_action(planned)
            if action == "keep":
                kept += 1
                self._log(
                    "town_keep_item",
                    index=planned_index,
                    name=planned.get("name"),
                    name_id=planned.get("name_id"),
                    amount=planned_amount,
                )
                continue
            if action == "sell":
                reserved_for_sale += 1
                self._log(
                    "town_reserve_for_sale",
                    index=planned_index,
                    name=planned.get("name"),
                    name_id=planned.get("name_id"),
                    amount=planned_amount,
                )
                continue

            item_units_moved = 0
            last_protocol_index: int | None = None
            hard_unit_cap = 2000

            while not self._stop.is_set() and item_units_moved < hard_unit_cap:
                live_item = self._resolve_live_inventory_item(planned)
                if live_item is None:
                    # The original stack is gone. That is the expected end state
                    # after the final acknowledged unit transfer.
                    break

                protocol_index = int(live_item.get("index") or -1)
                live_amount = int(live_item.get("amount") or 0)
                if protocol_index < 0 or live_amount <= 0:
                    break

                confirmed = False
                last_result: dict[str, Any] | None = None
                last_protocol_index = protocol_index

                # Safety-first transfer: send exactly ONE unit. The manual
                # working capture proved the opcode/layout is correct, while
                # the disconnecting automated capture used a stale stack amount.
                # One-unit moves cannot overrun a live stack, and every unit is
                # followed by a fresh server acknowledgement + inventory resolve.
                for attempt in range(1, 4):
                    before_state = authenticated_client_monitor.item_state_snapshot()
                    live_item = self._resolve_live_inventory_item(planned)
                    if live_item is None:
                        confirmed = True
                        break

                    protocol_index = int(live_item.get("index") or -1)
                    before_inventory_amount = int(live_item.get("amount") or 0)
                    if protocol_index < 0 or before_inventory_amount <= 0:
                        confirmed = True
                        break

                    name_id = int(live_item.get("name_id") or -1)
                    before_storage_total = self._storage_total_for_name_id(
                        before_state,
                        name_id,
                    )

                    last_protocol_index = protocol_index
                    last_result = native_action_bridge.storage_add(
                        protocol_index,
                        1,
                    )
                    self._log(
                        "storage_deposit_unit",
                        attempt=attempt,
                        planned_index=planned_index,
                        protocol_index=protocol_index,
                        name=live_item.get("name") or planned.get("name"),
                        name_id=name_id,
                        amount=1,
                        reported_live_amount=before_inventory_amount,
                        unit_number=item_units_moved + 1,
                        result=last_result,
                    )

                    if not last_result.get("ok"):
                        self._stop.wait(0.30)
                        continue

                    deadline = time.time() + 1.75
                    while not self._stop.is_set() and time.time() < deadline:
                        current_state = authenticated_client_monitor.item_state_snapshot()
                        live_inventory = current_state.get("inventory") or []
                        current = next(
                            (
                                row for row in live_inventory
                                if int(row.get("index") or -1) == protocol_index
                                and self._storage_item_identity(row)
                                    == self._storage_item_identity(live_item)
                            ),
                            None,
                        )
                        current_amount = (
                            int(current.get("amount") or 0)
                            if current is not None
                            else 0
                        )
                        storage_total = self._storage_total_for_name_id(
                            current_state,
                            name_id,
                        )

                        inventory_confirmed = (
                            current is None
                            or current_amount < before_inventory_amount
                        )
                        storage_confirmed = (
                            storage_total >= before_storage_total + 1
                        )
                        if inventory_confirmed or storage_confirmed:
                            confirmed = True
                            self._log(
                                "storage_deposit_ack",
                                attempt=attempt,
                                protocol_index=protocol_index,
                                name=live_item.get("name") or planned.get("name"),
                                inventory_before=before_inventory_amount,
                                inventory_after=current_amount,
                                storage_before=before_storage_total,
                                storage_after=storage_total,
                                inventory_confirmed=inventory_confirmed,
                                storage_confirmed=storage_confirmed,
                                amount=1,
                            )
                            break
                        self._stop.wait(0.08)

                    if confirmed:
                        break

                    # Re-read and re-resolve before each retry. Never retry an
                    # index/amount snapshot captured before a storage update.
                    self._stop.wait(0.40)

                if not confirmed:
                    cleanup = native_action_bridge.storage_close()
                    self._log(
                        "storage_close_after_failed_deposit",
                        planned_index=planned_index,
                        protocol_index=last_protocol_index,
                        name=planned.get("name"),
                        moved_units=item_units_moved,
                        result=cleanup,
                    )
                    self.last_error = (
                        f"Storage could not move {planned.get('name') or planned_index} "
                        f"(live index {last_protocol_index}) after 3 safe unit attempts. "
                        "Kafra storage was closed and the town cycle was paused."
                    )
                    return False

                # If the item vanished during resolution, there was nothing left
                # to send. Otherwise one unit was acknowledged.
                after = self._resolve_live_inventory_item(planned)
                if last_result and last_result.get("ok"):
                    item_units_moved += 1
                    deposited_units += 1
                    if last_protocol_index is not None:
                        self._deposited_indices_this_cycle.add(last_protocol_index)

                if after is None:
                    break

                # A short human-scale gap also gives the server's inventory and
                # storage list packets time to land before the next unit.
                self._stop.wait(0.12)

            if item_units_moved >= hard_unit_cap:
                self.last_error = (
                    f"Storage safety limit reached while moving "
                    f"{planned.get('name') or planned_index}; cycle paused."
                )
                return False

            self._deposited_indices_this_cycle.add(planned_index)
            if item_units_moved > 0:
                deposited += 1

        self._log(
            "storage_deposit_complete",
            deposited_stacks=deposited,
            deposited_units=deposited_units,
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
                profile = app_state.get_profile()

                # Explicit manual town-cycle requests are always honored. This
                # gives Manual mode a safe "run now" workflow without enabling
                # automatic returns.
                if self._force_cycle.is_set():
                    self._force_cycle.clear()
                    active_hunt_controller.stop()
                    self._set("TOWN_CYCLE", "Running manually requested town cycle.")
                    if not self._town_cycle(forced=True, resume_hunt=True):
                        self._set("PAUSED", self.last_error or "Town cycle failed.")
                        break
                    self._set("HUNTING", f"Hunting on {hunt_map}.")
                    self._stop.wait(0.20)
                    continue

                # Automatic mode watches the configured return conditions. In
                # Manual mode hunting continues normally and no automatic town
                # action is ever started.
                if bool(profile.town.auto_town_cycle):
                    world = self._world(self._snapshot())
                    weight_percent = world.get("weight_percent")
                    threshold = int(profile.town.return_weight_percent or 70)
                    trigger = self._automatic_town_trigger(
                        weight_percent=weight_percent,
                        threshold=threshold,
                    )
                    if trigger:
                        self._log(
                            "automatic_town_cycle_trigger",
                            reason=trigger,
                            weight_percent=weight_percent,
                            threshold=threshold,
                        )
                        active_hunt_controller.stop()
                        self._set("TOWN_CYCLE", f"Returning to town: {trigger}.")
                        if not self._town_cycle(
                            forced=False,
                            resume_hunt=True,
                            trigger_reason=trigger,
                        ):
                            self._set("PAUSED", self.last_error or "Automatic town cycle failed.")
                            break
                        self._set("HUNTING", f"Hunting on {hunt_map}.")
                        self._stop.wait(0.20)
                        continue

                if not active_hunt_controller.snapshot().get("running"):
                    active_hunt_controller.start({})
                    self._set("HUNTING", f"Hunting on {hunt_map}.")

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

            profile = app_state.get_profile()
            hunt_map = str(profile.hunt.map or "").strip()
            enabled_monsters = [
                rule.monster
                for rule in profile.hunt.monster_rules
                if rule.enabled and str(rule.behavior or "attack").strip().lower() != "ignore"
            ]
            if not enabled_monsters:
                enabled_monsters = [
                    name for name in profile.hunt.monsters
                    if str(name or "").strip()
                ]

            if not hunt_map:
                raise RuntimeError(
                    "No hunting map is saved. Configure Hunting & Loot and save settings first."
                )
            if not enabled_monsters:
                raise RuntimeError(
                    "No monsters are enabled in the saved Hunt profile. "
                    "Select at least one monster and save settings first."
                )

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
        """Run a town cycle now, regardless of Automatic/Manual mode."""
        with self._lock:
            if self.running:
                self._force_cycle.set()
                self._log("manual_town_cycle_requested", while_running=True)
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
            self._log("manual_town_cycle_requested", while_running=False)
            self._thread = threading.Thread(
                target=self._run_standalone_town_cycle_test,
                daemon=True,
            )
            self._thread.start()
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
                "auto_town_cycle": profile.town.auto_town_cycle,
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
