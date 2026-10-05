from __future__ import annotations

import threading
import time
from typing import Any

from core.active_control import active_hunt_controller
from core.game_actions import game_actions
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
        self._log("butterfly_landed", map=landed)
        return True

    def _open_kafra_storage(self, actor_id: int) -> bool:
        before = authenticated_client_monitor.item_state_snapshot().get("updated_at", {}).get("storage")
        steps = [
            ("talk", lambda: native_action_bridge.talk_npc(actor_id, 1)),
            ("continue", lambda: native_action_bridge.continue_npc(actor_id)),
            ("storage_option", lambda: native_action_bridge.choose_npc_option(actor_id, 2)),
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

    def _deposit_all_unequipped(self) -> bool:
        state = authenticated_client_monitor.item_state_snapshot()
        items = list(state.get("inventory") or [])
        for item in items:
            if bool(item.get("equipped")):
                continue
            amount = int(item.get("amount") or 0)
            index = int(item.get("index") or -1)
            if index < 0 or amount <= 0:
                continue
            result = native_action_bridge.storage_add(index, amount)
            self._log(
                "storage_deposit",
                index=index,
                name=item.get("name"),
                amount=amount,
                result=result,
            )
            if not result.get("ok"):
                self.last_error = (
                    f"Storage deposit failed for {item.get('name') or index}: "
                    f"{result.get('reason')}"
                )
                return False
            self._stop.wait(0.08)
        return True

    def _restock(self, actor_id: int) -> bool:
        profile = app_state.get_profile()
        supplies = profile.town.supplies
        desired = [
            {
                "item_id": AWAKENING_POTION_ID,
                "amount": max(0, int(supplies.awakening_potions)),
            },
            {
                "item_id": BUTTERFLY_WING_ID,
                "amount": max(0, int(supplies.butterfly_wings)),
            },
        ]
        desired = [row for row in desired if row["amount"] > 0]

        result = native_action_bridge.talk_npc(actor_id, 1)
        self._log("tool_dealer_talk", result=result)
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

        result = native_action_bridge.buy_bulk(desired)
        self._log("tool_dealer_buy", items=desired, result=result)
        if not result.get("ok"):
            self.last_error = f"Supply purchase failed: {result.get('reason')}"
            return False
        return True

    def _town_cycle(self) -> bool:
        profile = app_state.get_profile()
        self._set(
            "RETURNING",
            f"{profile.town.return_weight_percent}% weight reached; returning with Butterfly Wing.",
        )
        active_hunt_controller.stop()
        game_actions.release_hold_move()

        if not self._ensure_native_ready():
            return False
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

        try:
            native_action_bridge.close_npc(int(kafra["id"]))
        except Exception:
            pass
        self._stop.wait(0.20)

        self._set("FINDING_TOOL_DEALER", "Finding nearest Tool Dealer.")
        found = self._go_to_service("tool_dealer")
        if found is None:
            return False
        _, dealer = found

        self._set("RESTOCKING", "Buying configured Awakening Potions and Butterfly Wings.")
        if not self._restock(int(dealer["id"])):
            return False

        hunt_map = str(profile.hunt.map or "").strip().lower()
        if not hunt_map:
            self.last_error = "No hunt map configured."
            return False

        self._set("RETURNING_TO_HUNT", f"Returning to {hunt_map}.")
        if not self._travel_to_map(hunt_map):
            return False

        self._set("RESUMING_HUNT", f"Arrived at {hunt_map}; resuming hunting.")
        active_hunt_controller.start({})
        return True

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

                if (
                    self._force_cycle.is_set()
                    or (
                        weight_percent is not None
                        and float(weight_percent) >= float(threshold)
                    )
                ):
                    self._force_cycle.clear()
                    if not self._town_cycle():
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
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            return self.snapshot()

    def force_town_cycle(self) -> dict[str, Any]:
        if not self.running:
            raise RuntimeError("Start full automation before forcing a town cycle.")
        self._force_cycle.set()
        self._log("force_town_cycle_requested")
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
                "healing": {
                    "enabled": profile.healing.enabled,
                    "hotkey": profile.healing.hotkey,
                    "hp_below_percent": profile.healing.hp_below_percent,
                },
                "supplies": profile.town.supplies.model_dump(),
                "services": town_service_registry.snapshot(),
                "actions": self.actions[-30:],
            }


full_automation_controller = FullAutomationController()
