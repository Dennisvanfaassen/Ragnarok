from __future__ import annotations

import gzip
import heapq
import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NavMap:
    name: str
    width: int
    height: int
    cells: bytes
    source: str = ""

    def in_bounds(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def index(self, x: int, y: int) -> int:
        return y * self.width + x

    def walkable(self, x: int, y: int) -> bool:
        return self.in_bounds(x, y) and self.cells[self.index(x, y)] == 1


class WorldDatabase:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        manifest_path = self.root / "maps.json"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"World database not found: {manifest_path}. "
                "Build/copy the Ragnarokmap processed folder first."
            )

        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.maps = self.manifest.get("maps", {})
        self._cache: dict[str, NavMap] = {}

    def has_map(self, name: str) -> bool:
        return name.lower() in self.maps

    def load_map(self, name: str) -> NavMap:
        key = name.lower()
        if key in self._cache:
            return self._cache[key]

        info = self.maps.get(key)
        if info is None:
            raise KeyError(f"Unknown map: {name}")

        path = self.root / info["nav_file"]
        with gzip.open(path, "rb") as f:
            header = json.loads(f.readline().decode("utf-8"))
            cells = f.read()

        width = int(header["width"])
        height = int(header["height"])
        expected = width * height
        if len(cells) != expected:
            raise ValueError(
                f"Invalid nav grid for {key}: expected {expected} bytes, got {len(cells)}"
            )

        nav = NavMap(
            name=key,
            width=width,
            height=height,
            cells=cells,
            source=header.get("source", ""),
        )
        self._cache[key] = nav
        return nav


def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> float:
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    # Octile distance for 8-direction movement.
    return max(dx, dy) + (math.sqrt(2.0) - 1.0) * min(dx, dy)


def _neighbors(nav: NavMap, x: int, y: int):
    directions = (
        (-1, 0, 1.0),
        (1, 0, 1.0),
        (0, -1, 1.0),
        (0, 1, 1.0),
        (-1, -1, math.sqrt(2.0)),
        (-1, 1, math.sqrt(2.0)),
        (1, -1, math.sqrt(2.0)),
        (1, 1, math.sqrt(2.0)),
    )

    for dx, dy, cost in directions:
        nx, ny = x + dx, y + dy
        if not nav.walkable(nx, ny):
            continue

        # Prevent diagonal corner cutting through two blocked orthogonal cells.
        if dx and dy:
            if not nav.walkable(x + dx, y) or not nav.walkable(x, y + dy):
                continue

        yield nx, ny, cost


def nearest_walkable(
    nav: NavMap,
    point: tuple[int, int],
    max_radius: int = 12,
) -> tuple[int, int] | None:
    x0, y0 = point
    if nav.walkable(x0, y0):
        return point

    for radius in range(1, max_radius + 1):
        x_min, x_max = x0 - radius, x0 + radius
        y_min, y_max = y0 - radius, y0 + radius

        for x in range(x_min, x_max + 1):
            for y in (y_min, y_max):
                if nav.walkable(x, y):
                    return x, y

        for y in range(y_min + 1, y_max):
            for x in (x_min, x_max):
                if nav.walkable(x, y):
                    return x, y

    return None


def astar(
    nav: NavMap,
    start: tuple[int, int],
    goal: tuple[int, int],
    *,
    snap_radius: int = 12,
    max_expansions: int = 500_000,
) -> list[tuple[int, int]]:
    start = nearest_walkable(nav, start, snap_radius)
    goal = nearest_walkable(nav, goal, snap_radius)

    if start is None:
        raise ValueError("No walkable cell near start.")
    if goal is None:
        raise ValueError("No walkable cell near goal.")

    if start == goal:
        return [start]

    open_heap: list[tuple[float, float, tuple[int, int]]] = []
    heapq.heappush(open_heap, (_heuristic(start, goal), 0.0, start))

    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_score = {start: 0.0}
    closed: set[tuple[int, int]] = set()

    expansions = 0

    while open_heap:
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
        if expansions > max_expansions:
            raise RuntimeError("A* expansion limit reached.")

        cx, cy = current
        for nx, ny, step_cost in _neighbors(nav, cx, cy):
            neighbor = (nx, ny)
            if neighbor in closed:
                continue

            tentative = current_g + step_cost
            if tentative >= g_score.get(neighbor, float("inf")):
                continue

            came_from[neighbor] = current
            g_score[neighbor] = tentative
            f = tentative + _heuristic(neighbor, goal)
            heapq.heappush(open_heap, (f, tentative, neighbor))

    raise RuntimeError(f"No path from {start} to {goal} on {nav.name}.")


def simplify_path(
    path: list[tuple[int, int]],
    spacing: int = 8,
) -> list[tuple[int, int]]:
    """Reduce a dense tile path to useful steering waypoints."""
    if len(path) <= 2:
        return path[:]

    result = [path[0]]
    last_direction = None
    since_last = 0

    for i in range(1, len(path)):
        dx = path[i][0] - path[i - 1][0]
        dy = path[i][1] - path[i - 1][1]
        direction = (dx, dy)
        since_last += 1

        if last_direction is not None and direction != last_direction:
            if result[-1] != path[i - 1]:
                result.append(path[i - 1])
            since_last = 0
        elif since_last >= spacing:
            result.append(path[i])
            since_last = 0

        last_direction = direction

    if result[-1] != path[-1]:
        result.append(path[-1])

    return result
