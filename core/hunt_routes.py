from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core.pathing import astar, nav_repository, normalize_map_name


class HuntRouteStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._path = Path(__file__).resolve().parents[1] / "hunt_routes.json"
        self._routes: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self):
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._routes = raw
        except Exception:
            self._routes = {}

    def _save(self):
        self._path.write_text(
            json.dumps(self._routes, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    def _nearest_walkable(grid, x: int, y: int, max_radius: int = 8):
        if grid.walkable(x, y):
            return x, y

        for radius in range(1, max_radius + 1):
            candidates = []
            for yy in range(y - radius, y + radius + 1):
                for xx in range(x - radius, x + radius + 1):
                    if max(abs(xx - x), abs(yy - y)) != radius:
                        continue
                    if grid.walkable(xx, yy):
                        candidates.append((xx, yy))
            if candidates:
                return min(
                    candidates,
                    key=lambda p: (
                        abs(p[0] - x) + abs(p[1] - y),
                        p[1],
                        p[0],
                    ),
                )
        return None

    def get(self, map_name: str | None) -> dict[str, Any]:
        name = normalize_map_name(map_name)
        if not name:
            return {
                "map": None,
                "mode": "loop",
                "waypoints": [],
                "exists": False,
            }

        with self._lock:
            route = self._routes.get(name) or {}
            points = route.get("waypoints") or []
            return {
                "map": name,
                "mode": route.get("mode") or "loop",
                "waypoints": [
                    {"x": int(p["x"]), "y": int(p["y"])}
                    for p in points
                    if "x" in p and "y" in p
                ],
                "exists": bool(points),
            }

    def save(
        self,
        map_name: str,
        waypoints: list[dict[str, Any]],
        *,
        mode: str = "loop",
    ) -> dict[str, Any]:
        name = normalize_map_name(map_name)
        if not name:
            raise ValueError("A valid map name is required.")
        if mode not in {"loop", "pingpong"}:
            raise ValueError("Route mode must be loop or pingpong.")
        if len(waypoints) < 2:
            raise ValueError("Add at least two hunting-route points.")

        grid, _ = nav_repository.load(name)
        snapped = []
        for index, point in enumerate(waypoints):
            try:
                x = int(point["x"])
                y = int(point["y"])
            except Exception as exc:
                raise ValueError(f"Waypoint {index + 1} has invalid coordinates.") from exc

            valid = self._nearest_walkable(grid, x, y)
            if valid is None:
                raise ValueError(
                    f"Waypoint {index + 1} could not be snapped to walkable terrain."
                )
            snapped.append({"x": valid[0], "y": valid[1]})

        # Validate that every segment is actually connected through the GAT grid.
        pairs = list(zip(snapped, snapped[1:]))
        if mode == "loop":
            pairs.append((snapped[-1], snapped[0]))

        for index, (a, b) in enumerate(pairs, start=1):
            path = astar(
                grid,
                (a["x"], a["y"]),
                (b["x"], b["y"]),
                max_expansions=150000,
            )
            if not path:
                raise ValueError(
                    f"Route segment {index} has no walkable path between its points."
                )

        payload = {
            "mode": mode,
            "waypoints": snapped,
        }
        with self._lock:
            self._routes[name] = payload
            self._save()

        return self.get(name)

    def delete(self, map_name: str) -> dict[str, Any]:
        name = normalize_map_name(map_name)
        if not name:
            return self.get(name)

        with self._lock:
            self._routes.pop(name, None)
            self._save()
        return self.get(name)


hunt_route_store = HuntRouteStore()
