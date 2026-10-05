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
        self.frontier_bias_enabled = True

        self._map: str | None = None
        self._visits: dict[tuple[int, int], float] = defaultdict(float)
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

    def _score(
        self,
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
            + random.uniform(-0.08, 0.08)
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

                score = self._score(start, walkable_goal, path)
                candidates.append((score, path))

        if not candidates:
            return None

        candidates.sort(key=lambda entry: entry[0], reverse=True)
        best = candidates[0][1]
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
            "recent_goals": [
                {"x": x, "y": y} for x, y in list(self._recent_goals)
            ],
        }


exploration_planner = ExplorationPlanner()
