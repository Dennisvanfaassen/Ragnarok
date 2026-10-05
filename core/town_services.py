from __future__ import annotations

import json
import math
import re
import urllib.request
from pathlib import Path
from typing import Any

from core.world_route import world_route_planner


NPC_SHOPS_URL = "https://raw.githubusercontent.com/OpenKore/openkore/master/tables/npc_shops.txt"
AWAKENING_POTION_ID = 656
BUTTERFLY_WING_ID = 602


class TownServiceRegistry:
    """Map-agnostic Kafra / tool-dealer registry.

    Tool-dealer locations are discovered from OpenKore's npc_shops table by
    inventory contents, not by town-specific hardcoding. Kafra locations are
    learned from authenticated Classic.exe actor observations and persisted.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.shop_cache = cache_dir / "openkore_npc_shops.txt"
        self.learned_cache = cache_dir / "learned_town_services.json"
        self._shops_loaded = False
        self._shops: list[dict[str, Any]] = []
        self._learned: list[dict[str, Any]] = []
        self._load_learned()

        # Kafra coordinates referenced by OpenKore configuration/examples.
        # Runtime actor verification is still required before interaction, so a
        # server-specific/custom NPC layout will fail safely instead of clicking
        # an unrelated actor.
        for map_name, x, y in [
            ("izlude", 134, 88),
            ("prontera", 151, 29),
            ("geffen", 120, 62),
            ("payon", 181, 104),
            ("morocc", 160, 258),
            ("alberta", 113, 60),
        ]:
            self._seed_service(
                "kafra",
                map_name,
                x,
                y,
                name="Kafra",
                source="openkore_reference",
            )

    @staticmethod
    def _map(value: str | None) -> str:
        name = str(value or "").strip().lower()
        return re.sub(r"\.(gat|rsw)$", "", name)

    def _load_learned(self):
        try:
            rows = json.loads(self.learned_cache.read_text(encoding="utf-8"))
            if isinstance(rows, list):
                self._learned = [dict(row) for row in rows]
        except Exception:
            self._learned = []

    def _save_learned(self):
        try:
            self.learned_cache.write_text(
                json.dumps(self._learned, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _seed_service(
        self,
        service: str,
        map_name: str,
        x: int,
        y: int,
        *,
        name: str | None = None,
        actor_id: int | None = None,
        source: str = "learned",
    ):
        map_name = self._map(map_name)
        for row in self._learned:
            if (
                row.get("service") == service
                and self._map(row.get("map")) == map_name
                and max(abs(int(row.get("x", 0)) - x), abs(int(row.get("y", 0)) - y)) <= 3
            ):
                if actor_id is not None:
                    row["actor_id"] = int(actor_id)
                if name:
                    row["name"] = str(name)
                if source == "observed":
                    row["source"] = source
                return
        self._learned.append({
            "service": service,
            "map": map_name,
            "x": int(x),
            "y": int(y),
            "name": name,
            "actor_id": int(actor_id) if actor_id is not None else None,
            "source": source,
        })
        self._save_learned()

    def observe_live(self, snapshot: dict[str, Any]):
        live = snapshot.get("live_state") or {}
        world = live.get("world") or {}
        map_name = self._map(world.get("map"))
        if not map_name:
            return
        changed = False
        for actor in live.get("actors") or []:
            if actor.get("kind") != "other":
                continue
            name = str(actor.get("name") or "").strip()
            x, y = actor.get("x"), actor.get("y")
            if x is None or y is None or not name:
                continue
            lowered = name.lower()
            service = None
            if "kafra" in lowered:
                service = "kafra"
            elif "tool dealer" in lowered or lowered in {"tool dealer", "tool dealer#"}:
                service = "tool_dealer"
            if service:
                before = len(self._learned)
                self._seed_service(
                    service,
                    map_name,
                    int(x),
                    int(y),
                    name=name,
                    actor_id=int(actor.get("id")) if actor.get("id") is not None else None,
                    source="observed",
                )
                changed = changed or len(self._learned) != before
        if changed:
            self._save_learned()

    def _download_or_cache(self) -> str:
        if self.shop_cache.exists():
            return self.shop_cache.read_text(encoding="utf-8", errors="replace")
        with urllib.request.urlopen(NPC_SHOPS_URL, timeout=10) as response:
            text = response.read().decode("utf-8", errors="replace")
        self.shop_cache.write_text(text, encoding="utf-8")
        return text

    def _ensure_shops(self):
        if self._shops_loaded:
            return
        shops: list[dict[str, Any]] = []
        try:
            text = self._download_or_cache()
        except Exception:
            text = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            try:
                map_name = self._map(parts[0])
                x = int(parts[1])
                y = int(parts[2])
            except Exception:
                continue
            items: dict[int, int] = {}
            for part in parts[3:]:
                if ":" not in part:
                    continue
                item, price = part.split(":", 1)
                try:
                    items[int(item)] = int(price)
                except Exception:
                    continue
            shops.append({
                "service": "shop",
                "map": map_name,
                "x": x,
                "y": y,
                "items": items,
                "source": "openkore_npc_shops",
            })
        self._shops = shops
        self._shops_loaded = True

    def tool_dealers(self) -> list[dict[str, Any]]:
        self._ensure_shops()
        rows = []
        for shop in self._shops:
            items = shop.get("items") or {}
            if AWAKENING_POTION_ID in items and BUTTERFLY_WING_ID in items:
                row = dict(shop)
                row["service"] = "tool_dealer"
                rows.append(row)
        rows.extend(
            dict(row)
            for row in self._learned
            if row.get("service") == "tool_dealer"
        )
        return rows

    def kafras(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._learned
            if row.get("service") == "kafra"
        ]

    def candidates(self, service: str) -> list[dict[str, Any]]:
        if service == "kafra":
            return self.kafras()
        if service == "tool_dealer":
            return self.tool_dealers()
        return []

    def nearest(
        self,
        service: str,
        current_map: str,
        current_pos: tuple[int, int] | None = None,
    ) -> dict[str, Any] | None:
        current = self._map(current_map)
        best: tuple[float, dict[str, Any]] | None = None
        for row in self.candidates(service):
            target_map = self._map(row.get("map"))
            if not target_map:
                continue
            if target_map == current:
                if current_pos is None:
                    cost = 0.0
                else:
                    cost = math.hypot(
                        int(row["x"]) - current_pos[0],
                        int(row["y"]) - current_pos[1],
                    )
            else:
                route = world_route_planner.route_to_map(current, target_map)
                if route.get("status") != "ready":
                    continue
                cost = 1000.0 * int(route.get("portal_count") or 0)
                cost += math.hypot(int(row["x"]), int(row["y"])) / 1000.0
            if best is None or cost < best[0]:
                best = (cost, dict(row))
        return best[1] if best else None

    def snapshot(self) -> dict[str, Any]:
        self._ensure_shops()
        return {
            "learned_services": list(self._learned),
            "kafra_count": len(self.kafras()),
            "tool_dealer_count": len(self.tool_dealers()),
            "shop_rows": len(self._shops),
        }


town_service_registry = TownServiceRegistry(
    Path(__file__).resolve().parents[1] / "world_cache" / "services"
)
