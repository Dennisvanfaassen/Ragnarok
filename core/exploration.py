from __future__ import annotations

import math
import random
import time
from collections import defaultdict, deque
from typing import Any

from core.pathing import NavGrid, astar


class ExplorationPlanner:
    """Persistent, coverage-aware map exploration for hunting.

    The planner prefers continuing the current heading, visiting low-coverage
    regions and avoiding recent destinations. It operates only in map
    coordinates; the mouse adapter handles smooth directional movement.
    """

    def __init__(self):
        self.cell_size = 12
        self.min_goal_distance = 24
        self.max_goal_distance = 48
        self.heading_weight = 2.8
        self.coverage_weight = 4.0
        self.distance_weight = 0.7
        self.turn_penalty = 2.6
        self.reverse_penalty = 8.0
        self.path_coverage_weight = 1.8
        # Free-roaming should feel like a player sweeping useful open ground,
        # not like a wall-following robot. These are deliberately soft goal
        # preferences only: A* paths are never rejected for passing through
        # bridges, corridors, gates, or other narrow terrain.
        self.open_space_weight = 2.2
        self.wall_hug_penalty = 2.0
        self.dead_end_penalty = 2.6
        self.frontier_bias_enabled = True

        self._map: str | None = None
        self._visits: dict[tuple[int, int], float] = defaultdict(float)
        self._last_visit: dict[tuple[int, int], float] = {}
        self._heading: tuple[float, float] | None = None
        self._recent_goals: deque[tuple[int, int]] = deque(maxlen=14)
        self._recent_positions: deque[tuple[int, int]] = deque(maxlen=120)
        self._last_pos: tuple[int, int] | None = None
        self._last_update = time.time()

    def reset_for_map(self, map_name: str):
        if map_name == self._map:
            return
        self._map = map_name
        self._visits.clear()
        self._last_visit.clear()
        self._heading = None
        self._recent_goals.clear()
        self._recent_positions.clear()
        self._last_pos = None
        self._last_update = time.time()

    def _cell(self, point: tuple[int, int]) -> tuple[int, int]:
        return point[0] // self.cell_size, point[1] // self.cell_size

    def observe(self, map_name: str, position: tuple[int, int]):
        self.reset_for_map(map_name)

        now = time.time()
        dt = max(0.05, now - self._last_update)
        self._last_update = now

        cell = self._cell(position)
        self._visits[cell] += min(2.0, dt)
        self._last_visit[cell] = now

        if self._last_pos and position != self._last_pos:
            dx = position[0] - self._last_pos[0]
            dy = position[1] - self._last_pos[1]
            length = math.hypot(dx, dy)
            if length > 0:
                observed = (dx / length, dy / length)
                if self._heading is None:
                    self._heading = observed
                else:
                    # Keep momentum; don't let a tiny turn erase the overall sweep.
                    hx = self._heading[0] * 0.82 + observed[0] * 0.18
                    hy = self._heading[1] * 0.82 + observed[1] * 0.18
                    hlen = math.hypot(hx, hy)
                    if hlen > 0:
                        self._heading = (hx / hlen, hy / hlen)

        if not self._recent_positions or self._recent_positions[-1] != position:
            self._recent_positions.append(position)
        self._last_pos = position

    def note_goal(self, goal: tuple[int, int]):
        self._recent_goals.append(goal)

    def _coverage(self, point: tuple[int, int]) -> float:
        cx, cy = self._cell(point)
        total = 0.0
        weight = 0.0
        for ox in range(-1, 2):
            for oy in range(-1, 2):
                distance = abs(ox) + abs(oy)
                w = 1.0 / (1.0 + distance)
                total += self._visits.get((cx + ox, cy + oy), 0.0) * w
                weight += w
        return total / max(weight, 1e-6)

    def _goal_recent_penalty(self, point: tuple[int, int]) -> float:
        if not self._recent_goals:
            return 0.0
        penalty = 0.0
        for goal in self._recent_goals:
            d = math.hypot(point[0] - goal[0], point[1] - goal[1])
            if d < 20:
                penalty += (20 - d) / 20.0 * 4.0
        return penalty

    def _local_openness(
        self,
        grid: NavGrid,
        point: tuple[int, int],
        *,
        radius: int = 5,
    ) -> float:
        """Return how much usable space surrounds a roaming destination."""
        x, y = point
        walkable = 0
        total = 0
        for ox in range(-radius, radius + 1):
            for oy in range(-radius, radius + 1):
                if ox == 0 and oy == 0:
                    continue
                # Circular-ish sample so map corners do not dominate the score.
                if (ox * ox + oy * oy) > radius * radius:
                    continue
                total += 1
                if grid.walkable(x + ox, y + oy):
                    walkable += 1
        return walkable / max(1, total)

    def _wall_proximity(
        self,
        grid: NavGrid,
        point: tuple[int, int],
        *,
        max_radius: int = 6,
    ) -> float:
        """0=open, 1=immediately next to blocked/out-of-bounds terrain."""
        x, y = point
        for radius in range(1, max_radius + 1):
            for ox in range(-radius, radius + 1):
                for oy in range(-radius, radius + 1):
                    if max(abs(ox), abs(oy)) != radius:
                        continue
                    if not grid.walkable(x + ox, y + oy):
                        return (max_radius - radius + 1) / max_radius
        return 0.0

    def _escape_directions(
        self,
        grid: NavGrid,
        point: tuple[int, int],
        *,
        distance: int = 5,
    ) -> int:
        """Approximate whether a goal sits in open terrain or a dead-end pocket.

        This does not inspect or penalize the route used to reach the goal.
        Narrow bridges/corridors therefore remain fully usable when they are
        needed to reach another worthwhile area.
        """
        x, y = point
        directions = (
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1),
        )
        open_dirs = 0
        for dx, dy in directions:
            clear = 0
            for step in range(1, distance + 1):
                if not grid.walkable(x + dx * step, y + dy * step):
                    break
                clear += 1
            if clear >= max(2, distance - 1):
                open_dirs += 1
        return open_dirs

    def _score(
        self,
        grid: NavGrid,
        start: tuple[int, int],
        goal: tuple[int, int],
        path: list[tuple[int, int]],
    ) -> float:
        dx = goal[0] - start[0]
        dy = goal[1] - start[1]
        distance = math.hypot(dx, dy)
        if distance <= 0:
            return -999999.0

        direction = (dx / distance, dy / distance)
        heading_score = 0.0
        turn_penalty = 0.0
        if self._heading is not None:
            dot = max(-1.0, min(1.0, self._heading[0] * direction[0] + self._heading[1] * direction[1]))
            heading_score = dot
            turn_penalty = max(0.0, 1.0 - dot)

        coverage = self._coverage(goal)
        sampled_path = path[::max(1, len(path) // 12)]
        path_coverage = (
            sum(self._coverage(point) for point in sampled_path)
            / max(1, len(sampled_path))
        )
        route_efficiency = distance / max(1.0, len(path) - 1)

        # Only the *destination* gets an open-space preference. The route itself
        # may freely hug walls or cross narrow terrain when topology requires it.
        openness = self._local_openness(grid, goal)
        wall_proximity = self._wall_proximity(grid, goal)
        escape_directions = self._escape_directions(grid, goal)
        dead_end = max(0.0, (3 - escape_directions) / 3.0)

        reverse = 0.0
        if self.frontier_bias_enabled and self._heading is not None:
            dot = max(-1.0, min(1.0, self._heading[0] * direction[0] + self._heading[1] * direction[1]))
            if dot < -0.20:
                reverse = (-dot - 0.20) / 0.80

        recent_path_penalty = 0.0
        if self.frontier_bias_enabled and self._recent_positions:
            for point in sampled_path:
                nearest = min(
                    math.hypot(point[0] - p[0], point[1] - p[1])
                    for p in self._recent_positions
                )
                if nearest < 8.0:
                    recent_path_penalty += (8.0 - nearest) / 8.0
            recent_path_penalty /= max(1, len(sampled_path))

        return (
            heading_score * self.heading_weight
            - turn_penalty * self.turn_penalty
            - reverse * self.reverse_penalty
            - coverage * self.coverage_weight
            - path_coverage * (self.path_coverage_weight if self.frontier_bias_enabled else 0.0)
            - recent_path_penalty * 5.0
            - self._goal_recent_penalty(goal)
            + route_efficiency * self.distance_weight
            + openness * self.open_space_weight
            - wall_proximity * self.wall_hug_penalty
            - dead_end * self.dead_end_penalty
            + random.uniform(-0.16, 0.16)
        )

    def choose_route(
        self,
        map_name: str,
        grid: NavGrid,
        start: tuple[int, int],
        *,
        frontier_bias: bool = True,
    ) -> list[tuple[int, int]] | None:
        self.frontier_bias_enabled = bool(frontier_bias)
        self.observe(map_name, start)

        candidates: list[tuple[float, list[tuple[int, int]]]] = []
        base_heading = self._heading

        # If no heading exists yet, pick one once. Afterwards direction persists.
        if base_heading is None:
            angle = random.random() * math.tau
            base_heading = (math.cos(angle), math.sin(angle))
            self._heading = base_heading

        # Search mostly ahead, with wider alternatives for terrain/coverage.
        angle_offsets = [
            0,
            math.radians(18),
            -math.radians(18),
            math.radians(35),
            -math.radians(35),
            math.radians(60),
            -math.radians(60),
            math.radians(90),
            -math.radians(90),
            math.pi,
        ]

        for angle_offset in angle_offsets:
            ca = math.cos(angle_offset)
            sa = math.sin(angle_offset)
            hx, hy = base_heading
            direction = (hx * ca - hy * sa, hx * sa + hy * ca)

            for distance in (
                self.max_goal_distance,
                int((self.min_goal_distance + self.max_goal_distance) / 2),
                self.min_goal_distance,
            ):
                gx = int(round(start[0] + direction[0] * distance))
                gy = int(round(start[1] + direction[1] * distance))

                # Find a nearby walkable goal instead of abandoning the heading
                # just because the exact sampled tile is blocked.
                walkable_goal = None
                for radius in range(0, 9):
                    found = []
                    for ox in range(-radius, radius + 1):
                        for oy in range(-radius, radius + 1):
                            if radius and max(abs(ox), abs(oy)) != radius:
                                continue
                            x, y = gx + ox, gy + oy
                            if grid.walkable(x, y):
                                found.append((x, y))
                    if found:
                        walkable_goal = min(
                            found,
                            key=lambda p: math.hypot(p[0] - gx, p[1] - gy),
                        )
                        break

                if walkable_goal is None:
                    continue

                path = astar(grid, start, walkable_goal, max_expansions=90000)
                if not path or len(path) < 8:
                    continue

                score = self._score(grid, start, walkable_goal, path)
                candidates.append((score, path))

        if not candidates:
            return None

        candidates.sort(key=lambda entry: entry[0], reverse=True)

        # Avoid a mechanically identical "perfect" destination every time.
        # Choose among near-equal top routes, while never allowing a clearly
        # worse edge/dead-end candidate to win through randomness alone.
        best_score = candidates[0][0]
        near_best = [
            entry for entry in candidates[:5]
            if entry[0] >= best_score - 0.9
        ]
        if len(near_best) == 1:
            best = near_best[0][1]
        else:
            weights = [1.0 / (1.0 + rank * 0.75) for rank in range(len(near_best))]
            best = random.choices(near_best, weights=weights, k=1)[0][1]
        goal = best[-1]

        # Slowly steer the persistent heading toward the chosen sweep direction.
        dx = goal[0] - start[0]
        dy = goal[1] - start[1]
        length = math.hypot(dx, dy)
        if length > 0:
            chosen = (dx / length, dy / length)
            if self._heading is None:
                self._heading = chosen
            else:
                hx = self._heading[0] * 0.68 + chosen[0] * 0.32
                hy = self._heading[1] * 0.68 + chosen[1] * 0.32
                hlen = math.hypot(hx, hy)
                if hlen:
                    self._heading = (hx / hlen, hy / hlen)

        self.note_goal(goal)
        return best

    def heatmap_snapshot(self, grid: NavGrid | None = None) -> dict[str, Any]:
        """Detailed session exploration state for the dashboard map."""
        now = time.time()
        cells: list[dict[str, Any]] = []

        if grid is not None:
            max_cx = max(0, (grid.width - 1) // self.cell_size)
            max_cy = max(0, (grid.height - 1) // self.cell_size)
            for cy in range(max_cy + 1):
                for cx in range(max_cx + 1):
                    x0 = cx * self.cell_size
                    y0 = cy * self.cell_size
                    x1 = min(grid.width, x0 + self.cell_size)
                    y1 = min(grid.height, y0 + self.cell_size)
                    walkable = 0
                    total = max(1, (x1 - x0) * (y1 - y0))
                    for y in range(y0, y1):
                        row = y * grid.width
                        for x in range(x0, x1):
                            if grid.cells[row + x] == 1:
                                walkable += 1
                    if walkable == 0:
                        continue

                    key = (cx, cy)
                    last = self._last_visit.get(key)
                    visits = float(self._visits.get(key, 0.0))
                    center = (
                        min(grid.width - 1, x0 + max(0, (x1 - x0 - 1) // 2)),
                        min(grid.height - 1, y0 + max(0, (y1 - y0 - 1) // 2)),
                    )
                    cells.append({
                        "cx": cx, "cy": cy, "x": x0, "y": y0,
                        "width": x1 - x0, "height": y1 - y0,
                        "center_x": center[0], "center_y": center[1],
                        "walkable_ratio": round(walkable / total, 3),
                        "visited": last is not None,
                        "last_visited_at": round(last, 3) if last is not None else None,
                        "age_seconds": round(max(0.0, now - last), 2) if last is not None else None,
                        "visit_seconds": round(visits, 2),
                        "coverage": round(self._coverage(center), 3),
                        "openness": round(self._local_openness(grid, center), 3),
                        "wall_proximity": round(self._wall_proximity(grid, center), 3),
                        "escape_directions": self._escape_directions(grid, center),
                    })

        visited = [row for row in cells if row["visited"]]
        ages = [float(row["age_seconds"]) for row in visited if row["age_seconds"] is not None]
        return {
            "map": self._map,
            "cell_size": self.cell_size,
            "generated_at": round(now, 3),
            "cells": cells,
            "summary": {
                "walkable_cells": len(cells),
                "visited_cells": len(visited),
                "unvisited_cells": len(cells) - len(visited),
                "visited_percent": round((len(visited) / max(1, len(cells))) * 100, 1),
                "oldest_visit_age_seconds": round(max(ages), 1) if ages else None,
            },
            "recent_positions": [{"x": x, "y": y} for x, y in list(self._recent_positions)],
            "recent_goals": [{"x": x, "y": y} for x, y in list(self._recent_goals)],
            "heading": (
                {"x": round(self._heading[0], 3), "y": round(self._heading[1], 3)}
                if self._heading else None
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "map": self._map,
            "heading": (
                {"x": round(self._heading[0], 3), "y": round(self._heading[1], 3)}
                if self._heading else None
            ),
            "visited_cells": len(self._visits),
            "recent_position_count": len(self._recent_positions),
            "frontier_bias_enabled": self.frontier_bias_enabled,
            "open_space_bias": {
                "weight": self.open_space_weight,
                "wall_hug_penalty": self.wall_hug_penalty,
                "dead_end_penalty": self.dead_end_penalty,
                "goal_only": True,
            },
            "recent_goals": [
                {"x": x, "y": y} for x, y in list(self._recent_goals)
            ],
        }


exploration_planner = ExplorationPlanner()
