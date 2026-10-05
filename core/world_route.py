from __future__ import annotations

import heapq
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PORTALS_URL = "https://raw.githubusercontent.com/OpenKore/openkore/master/tables/portals.txt"
CITIES_URL = "https://raw.githubusercontent.com/OpenKore/openkore/master/tables/cities.txt"

# Prefer outdoor/main town maps, not interiors that happen to be in cities.txt.
DEFAULT_TOWNS = {
    "prontera", "izlude", "geffen", "payon", "morocc", "alberta",
    "aldebaran", "yuno", "comodo", "umbala", "amatsu", "gonryun",
    "louyang", "rachel", "lighthalzen", "einbroch", "einbech",
    "hugel", "veins", "xmas", "moscovia", "ayothaya", "brasilis",
    "dewata", "manuk", "eclage", "malangdo", "malaya", "mora",
    "lasagna", "harboro1",
}


@dataclass(frozen=True)
class PortalEdge:
    source_map: str
    source_x: int
    source_y: int
    dest_map: str
    dest_x: int
    dest_y: int
    source: str = "openkore"
    interactive: bool = False
    interaction_steps: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_map": self.source_map,
            "source_x": self.source_x,
            "source_y": self.source_y,
            "dest_map": self.dest_map,
            "dest_x": self.dest_x,
            "dest_y": self.dest_y,
            "source": self.source,
            "interactive": self.interactive,
            "interaction_steps": list(self.interaction_steps),
        }


class WorldRoutePlanner:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.portal_cache = cache_dir / "openkore_portals.txt"
        self.city_cache = cache_dir / "openkore_cities.txt"
        self.learned_cache = cache_dir / "learned_portals.json"

        self._loaded = False
        self._edges: list[PortalEdge] = []
        self._graph: dict[str, list[PortalEdge]] = {}
        self._towns = set(DEFAULT_TOWNS)
        self._learned: list[PortalEdge] = []
        self._load_learned()

    @staticmethod
    def _map(value: str | None) -> str:
        name = (value or "").strip().lower()
        if name.endswith(".rsw"):
            name = name[:-4]
        if name.endswith(".gat"):
            name = name[:-4]
        return name

    def _download_or_cache(self, url: str, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        with urllib.request.urlopen(url, timeout=10) as response:
            text = response.read().decode("utf-8", errors="replace")
        path.write_text(text, encoding="utf-8")
        return text

    def _load_learned(self):
        try:
            rows = json.loads(self.learned_cache.read_text(encoding="utf-8"))
            self._learned = [
                PortalEdge(
                    source_map=self._map(row["source_map"]),
                    source_x=int(row["source_x"]),
                    source_y=int(row["source_y"]),
                    dest_map=self._map(row["dest_map"]),
                    dest_x=int(row["dest_x"]),
                    dest_y=int(row["dest_y"]),
                    source="learned",
                    interactive=bool(row.get("interactive", False)),
                    interaction_steps=tuple(row.get("interaction_steps") or ()),
                )
                for row in rows
            ]
        except Exception:
            self._learned = []

    def _save_learned(self):
        try:
            self.learned_cache.write_text(
                json.dumps([edge.as_dict() for edge in self._learned], indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _parse_portals(self, text: str) -> list[PortalEdge]:
        edges: list[PortalEdge] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            if not all(re.fullmatch(r"-?\d+", part) for part in (parts[1], parts[2], parts[4], parts[5])):
                continue
            source_map = self._map(parts[0])
            dest_map = self._map(parts[3])
            if not source_map or not dest_map:
                continue
            edges.append(
                PortalEdge(
                    source_map=source_map,
                    source_x=int(parts[1]),
                    source_y=int(parts[2]),
                    dest_map=dest_map,
                    dest_x=int(parts[4]),
                    dest_y=int(parts[5]),
                    interactive=len(parts) > 6,
                    interaction_steps=tuple(parts[6:]) if len(parts) > 6 else (),
                )
            )
        return edges

    def _parse_cities(self, text: str):
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            map_part = raw.split("#", 1)[0].strip()
            map_name = self._map(map_part)
            if not map_name:
                continue
            # Only add obvious main city maps automatically. Interiors are
            # deliberately excluded from nearest-town destination selection.
            if map_name in DEFAULT_TOWNS:
                self._towns.add(map_name)

    def ensure_loaded(self):
        if self._loaded:
            return
        try:
            portals_text = self._download_or_cache(PORTALS_URL, self.portal_cache)
            self._edges = self._parse_portals(portals_text)
        except Exception:
            self._edges = []

        try:
            city_text = self._download_or_cache(CITIES_URL, self.city_cache)
            self._parse_cities(city_text)
        except Exception:
            pass

        # Server-observed edges take precedence by being inserted first.
        graph: dict[str, list[PortalEdge]] = {}
        for edge in [*self._learned, *self._edges]:
            bucket = graph.setdefault(edge.source_map, [])
            duplicate = any(
                current.source_x == edge.source_x
                and current.source_y == edge.source_y
                and current.dest_map == edge.dest_map
                for current in bucket
            )
            if not duplicate:
                bucket.append(edge)
        self._graph = graph
        self._loaded = True

    def learn_transition(
        self,
        source_map: str,
        source_x: int,
        source_y: int,
        dest_map: str,
        dest_x: int,
        dest_y: int,
    ):
        src = self._map(source_map)
        dst = self._map(dest_map)
        if not src or not dst or src == dst:
            return

        # Merge close observations of the same map transition rather than
        # creating a new record for every tile around a portal trigger.
        for edge in self._learned:
            if (
                edge.source_map == src
                and edge.dest_map == dst
                and max(abs(edge.source_x - source_x), abs(edge.source_y - source_y)) <= 4
            ):
                return

        self._learned.append(
            PortalEdge(
                source_map=src,
                source_x=int(source_x),
                source_y=int(source_y),
                dest_map=dst,
                dest_x=int(dest_x),
                dest_y=int(dest_y),
                source="learned",
                interactive=False,
            )
        )
        self._save_learned()
        self._loaded = False

    def _map_routes(
        self,
        start_map: str,
        destinations: set[str],
    ) -> tuple[str, list[PortalEdge]] | None:
        self.ensure_loaded()
        start = self._map(start_map)
        if start in destinations:
            return start, []

        queue: list[tuple[int, int, str, list[PortalEdge]]] = []
        counter = 0
        heapq.heappush(queue, (0, counter, start, []))
        best: dict[str, int] = {start: 0}

        while queue:
            cost, _, map_name, route = heapq.heappop(queue)
            if cost != best.get(map_name):
                continue
            if map_name in destinations:
                return map_name, route

            for edge in self._graph.get(map_name, []):
                # Prefer learned/server-observed physical transitions.
                # NPC/dialog warps remain available as a last resort but are
                # intentionally expensive for the current mouse-only client.
                edge_cost = 90 if edge.source == "learned" else 100
                if edge.interactive:
                    edge_cost += 1200
                new_cost = cost + edge_cost
                if new_cost >= best.get(edge.dest_map, 10**9):
                    continue
                best[edge.dest_map] = new_cost
                counter += 1
                heapq.heappush(
                    queue,
                    (new_cost, counter, edge.dest_map, [*route, edge]),
                )

        return None

    def route_to_map(
        self,
        current_map: str,
        target_map: str,
    ) -> dict[str, Any]:
        current = self._map(current_map)
        target = self._map(target_map)
        if not current or not target:
            return {
                "status": "waiting",
                "message": "Current map or target map is unknown.",
                "current_map": current or None,
                "target_map": target or None,
                "legs": [],
                "next_portal": None,
            }

        result = self._map_routes(current, {target})
        if result is None:
            return {
                "status": "unreachable",
                "message": f"No known portal route from {current} to {target}.",
                "current_map": current,
                "target_map": target,
                "legs": [],
                "next_portal": None,
            }

        _, route = result
        return {
            "status": "ready",
            "message": (
                f"Already in {target}." if not route
                else f"{len(route)} portal transition(s) to {target}."
            ),
            "current_map": current,
            "target_map": target,
            "portal_count": len(route),
            "legs": [edge.as_dict() for edge in route],
            "next_portal": route[0].as_dict() if route else None,
        }

    def route_to_town(
        self,
        current_map: str,
        *,
        preferred_town: str | None = None,
    ) -> dict[str, Any]:
        current = self._map(current_map)
        if not current:
            return {
                "status": "waiting",
                "message": "Current map is unknown.",
                "current_map": None,
                "town": None,
                "legs": [],
            }

        if preferred_town:
            destinations = {self._map(preferred_town)}
        else:
            destinations = set(self._towns)

        result = self._map_routes(current, destinations)
        if result is None:
            return {
                "status": "unreachable",
                "message": f"No known portal route from {current} to a town.",
                "current_map": current,
                "town": None,
                "legs": [],
            }

        town, route = result
        return {
            "status": "ready",
            "message": (
                f"Already in {town}." if not route
                else f"{len(route)} portal transition(s) to {town}."
            ),
            "current_map": current,
            "town": town,
            "portal_count": len(route),
            "legs": [edge.as_dict() for edge in route],
            "next_portal": route[0].as_dict() if route else None,
        }

    def snapshot(self) -> dict[str, Any]:
        self.ensure_loaded()
        return {
            "portal_edges": sum(len(v) for v in self._graph.values()),
            "maps_with_portals": len(self._graph),
            "towns": sorted(self._towns),
            "learned_edges": len(self._learned),
        }


world_route_planner = WorldRoutePlanner(
    Path(__file__).resolve().parents[1] / "world_cache" / "world"
)
