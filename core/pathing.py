from __future__ import annotations

import gzip
import heapq
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORLD_RAW_BASE = (
    "https://raw.githubusercontent.com/"
    "Dennisvanfaassen/Ragnarokmap/main/processed/nav"
)


@dataclass
class NavGrid:
    map_name: str
    width: int
    height: int
    cells: bytes

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def walkable(self, x: int, y: int) -> bool:
        if not self.in_bounds(x, y):
            return False
        return self.cells[y * self.width + x] == 1


def normalize_map_name(value: str | None) -> str:
    name = (value or "").strip().lower()
    if name.endswith(".gat"):
        name = name[:-4]
    return name


def _read_nav_bytes(blob: bytes) -> NavGrid:
    raw = gzip.decompress(blob)
    header_line, cells = raw.split(b"\n", 1)
    header = json.loads(header_line.decode("utf-8"))
    width = int(header["width"])
    height = int(header["height"])
    expected = width * height
    if len(cells) != expected:
        raise ValueError(
            f"Navigation grid size mismatch: expected {expected}, got {len(cells)}"
        )
    return NavGrid(
        map_name=str(header.get("map") or ""),
        width=width,
        height=height,
        cells=cells,
    )


class WorldNavRepository:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, NavGrid] = {}

    def _local_candidates(self, map_name: str) -> list[Path]:
        filename = f"{map_name}.nav.gz"
        root = Path(__file__).resolve().parents[1]
        return [
            self.cache_dir / filename,
            root / "world_data" / "nav" / filename,
            root.parent / "Ragnarokmap" / "processed" / "nav" / filename,
            Path(r"C:\Ragnarokmap\processed\nav") / filename,
        ]

    def load(self, map_name: str) -> tuple[NavGrid, str]:
        name = normalize_map_name(map_name)
        if not name:
            raise ValueError("Current map is not known yet.")

        if name in self._memory:
            return self._memory[name], "memory"

        for path in self._local_candidates(name):
            if path.exists():
                grid = _read_nav_bytes(path.read_bytes())
                self._memory[name] = grid
                return grid, str(path)

        url = f"{WORLD_RAW_BASE}/{name}.nav.gz"
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                blob = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(
                    f"No navigation data found for map '{name}'."
                ) from exc
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Could not load navigation data for '{name}': {exc}"
            ) from exc

        cache_path = self.cache_dir / f"{name}.nav.gz"
        cache_path.write_bytes(blob)
        grid = _read_nav_bytes(blob)
        self._memory[name] = grid
        return grid, "github-cache"


def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> float:
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)


def _neighbors(grid: NavGrid, x: int, y: int):
    for dx, dy, cost in (
        (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
        (-1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)),
        (1, -1, math.sqrt(2)), (1, 1, math.sqrt(2)),
    ):
        nx, ny = x + dx, y + dy
        if not grid.walkable(nx, ny):
            continue

        # Never cut diagonally through the corner of blocked cells.
        if dx and dy:
            if not grid.walkable(x + dx, y) or not grid.walkable(x, y + dy):
                continue

        yield nx, ny, cost


def _nearest_walkable(
    grid: NavGrid,
    point: tuple[int, int],
    max_radius: int = 4,
) -> tuple[int, int] | None:
    x0, y0 = point
    if grid.walkable(x0, y0):
        return point

    for radius in range(1, max_radius + 1):
        candidates = []
        for y in range(y0 - radius, y0 + radius + 1):
            for x in range(x0 - radius, x0 + radius + 1):
                if max(abs(x - x0), abs(y - y0)) != radius:
                    continue
                if grid.walkable(x, y):
                    candidates.append((x, y))
        if candidates:
            return min(
                candidates,
                key=lambda p: (abs(p[0] - x0) + abs(p[1] - y0), p[1], p[0]),
            )
    return None


def astar(
    grid: NavGrid,
    start: tuple[int, int],
    goal: tuple[int, int],
    *,
    max_expansions: int = 150000,
) -> list[tuple[int, int]] | None:
    start = _nearest_walkable(grid, start) or start
    goal = _nearest_walkable(grid, goal)
    if goal is None or not grid.walkable(*start):
        return None
    if start == goal:
        return [start]

    open_heap: list[tuple[float, float, tuple[int, int]]] = []
    heapq.heappush(open_heap, (_heuristic(start, goal), 0.0, start))

    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score: dict[tuple[int, int], float] = {start: 0.0}
    closed: set[tuple[int, int]] = set()
    expansions = 0

    while open_heap and expansions < max_expansions:
        _f, current_g, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        if current == goal:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path

        closed.add(current)
        expansions += 1
        x, y = current

        for nx, ny, move_cost in _neighbors(grid, x, y):
            nxt = (nx, ny)
            if nxt in closed:
                continue
            tentative = current_g + move_cost
            if tentative >= g_score.get(nxt, float("inf")):
                continue
            g_score[nxt] = tentative
            came_from[nxt] = current
            heapq.heappush(
                open_heap,
                (tentative + _heuristic(nxt, goal), tentative, nxt),
            )

    return None


def _compress_waypoints(path: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(path) <= 2:
        return path[:]

    result = [path[0]]
    prev_dx = path[1][0] - path[0][0]
    prev_dy = path[1][1] - path[0][1]

    for i in range(2, len(path)):
        dx = path[i][0] - path[i - 1][0]
        dy = path[i][1] - path[i - 1][1]
        if (dx, dy) != (prev_dx, prev_dy):
            result.append(path[i - 1])
            prev_dx, prev_dy = dx, dy

    result.append(path[-1])
    return result


def build_pathing_state(
    live_snapshot: dict[str, Any],
    targeting: dict[str, Any],
    nav_repo: WorldNavRepository,
) -> dict[str, Any]:
    live = live_snapshot.get("live_state") or {}
    world = live.get("world") or {}
    selected = targeting.get("selected")

    map_name = normalize_map_name(world.get("map"))
    start_x = world.get("x")
    start_y = world.get("y")

    base = {
        "map": map_name or None,
        "status": "waiting",
        "message": "",
        "start": None,
        "target": selected,
        "goal": None,
        "path_found": False,
        "path_steps": None,
        "path_cost": None,
        "next_step": None,
        "next_waypoint": None,
        "waypoints": [],
        "path_preview": [],
        "nav_source": None,
        "map_size": None,
    }

    if not map_name:
        base["message"] = "Waiting for current map."
        return base
    if start_x is None or start_y is None:
        base["message"] = "Waiting for current position."
        return base
    if not selected:
        base["message"] = "Waiting for a valid monster target."
        return base

    start = (int(start_x), int(start_y))
    target = (int(selected["x"]), int(selected["y"]))
    base["start"] = {"x": start[0], "y": start[1]}

    try:
        grid, source = nav_repo.load(map_name)
    except Exception as exc:
        base["status"] = "error"
        base["message"] = str(exc)
        return base

    base["nav_source"] = source
    base["map_size"] = {"width": grid.width, "height": grid.height}

    goal = _nearest_walkable(grid, target)
    if goal is None:
        base["status"] = "unreachable"
        base["message"] = "No walkable tile was found near the target."
        return base

    base["goal"] = {"x": goal[0], "y": goal[1]}

    path = astar(grid, start, goal)
    if not path:
        base["status"] = "unreachable"
        base["message"] = "No walkable route to the selected target was found."
        return base

    waypoints = _compress_waypoints(path)
    cost = 0.0
    for a, b in zip(path, path[1:]):
        dx = abs(a[0] - b[0])
        dy = abs(a[1] - b[1])
        cost += math.sqrt(2) if dx and dy else 1.0

    base.update({
        "status": "ready",
        "message": "Walkable path found.",
        "path_found": True,
        "path_steps": max(0, len(path) - 1),
        "path_cost": round(cost, 2),
        "next_step": (
            {"x": path[1][0], "y": path[1][1]} if len(path) > 1 else None
        ),
        "next_waypoint": (
            {"x": waypoints[1][0], "y": waypoints[1][1]}
            if len(waypoints) > 1 else None
        ),
        "waypoints": [{"x": x, "y": y} for x, y in waypoints[:50]],
        "path_preview": [{"x": x, "y": y} for x, y in path[:100]],
    })
    return base


nav_repository = WorldNavRepository(
    Path(__file__).resolve().parents[1] / "world_cache" / "nav"
)
