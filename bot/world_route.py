from __future__ import annotations

import math
import random
from dataclasses import dataclass

from bot.world_nav import WorldDatabase, astar, simplify_path


@dataclass
class WorldRouteState:
    map_name: str
    goal: tuple[int, int] | None = None
    path: list[tuple[int, int]] | None = None
    waypoints: list[tuple[int, int]] | None = None
    waypoint_index: int = 0


class WorldRoutePlanner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.db = WorldDatabase(cfg["database_path"])
        self.state = WorldRouteState(map_name=str(cfg.get("current_map", "")).lower())

    def set_map(self, map_name: str) -> None:
        name = map_name.lower()
        if name != self.state.map_name:
            self.state = WorldRouteState(map_name=name)

    def map_position_from_minimap(
        self,
        nav_map,
        minimap_player: tuple[int, int],
        minimap_shape: tuple[int, int],
    ) -> tuple[int, int]:
        mh, mw = minimap_shape
        px, py = minimap_player

        pad_left = float(self.cfg.get("minimap_padding_left_ratio", 0.0))
        pad_right = float(self.cfg.get("minimap_padding_right_ratio", 0.0))
        pad_top = float(self.cfg.get("minimap_padding_top_ratio", 0.0))
        pad_bottom = float(self.cfg.get("minimap_padding_bottom_ratio", 0.0))

        usable_x0 = mw * pad_left
        usable_x1 = mw * (1.0 - pad_right)
        usable_y0 = mh * pad_top
        usable_y1 = mh * (1.0 - pad_bottom)

        nx = (px - usable_x0) / max(1.0, usable_x1 - usable_x0)
        ny = (py - usable_y0) / max(1.0, usable_y1 - usable_y0)
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))

        gx = int(round(nx * (nav_map.width - 1)))
        if self.cfg.get("invert_y", True):
            gy = int(round((1.0 - ny) * (nav_map.height - 1)))
        else:
            gy = int(round(ny * (nav_map.height - 1)))

        return gx, gy

    def _choose_exploration_goal(self, nav_map, start: tuple[int, int]) -> tuple[int, int]:
        min_dist = int(self.cfg.get("exploration_min_distance_cells", 45))
        max_dist = int(self.cfg.get("exploration_max_distance_cells", 120))
        attempts = int(self.cfg.get("exploration_attempts", 200))

        best = None
        best_dist = -1.0

        for _ in range(attempts):
            angle = random.uniform(0.0, math.tau)
            distance = random.uniform(min_dist, max_dist)
            x = int(round(start[0] + math.cos(angle) * distance))
            y = int(round(start[1] + math.sin(angle) * distance))

            if not nav_map.walkable(x, y):
                continue

            d = math.hypot(x - start[0], y - start[1])
            if d > best_dist:
                best = (x, y)
                best_dist = d

        if best is None:
            raise RuntimeError("Could not find a walkable exploration goal.")
        return best

    def _needs_new_route(self, current: tuple[int, int]) -> bool:
        if not self.state.waypoints:
            return True
        if self.state.waypoint_index >= len(self.state.waypoints):
            return True
        if self.state.goal is None:
            return True

        goal_reached = math.hypot(
            current[0] - self.state.goal[0],
            current[1] - self.state.goal[1],
        ) <= float(self.cfg.get("goal_reached_radius_cells", 4.0))
        return goal_reached

    def plan_heading(
        self,
        minimap_player: tuple[int, int],
        minimap_shape: tuple[int, int],
    ):
        if not self.state.map_name:
            return None, {"status": "world_map_not_set"}

        if not self.db.has_map(self.state.map_name):
            return None, {
                "status": "world_map_unknown",
                "map": self.state.map_name,
            }

        nav_map = self.db.load_map(self.state.map_name)
        current = self.map_position_from_minimap(nav_map, minimap_player, minimap_shape)

        if self._needs_new_route(current):
            goal = self._choose_exploration_goal(nav_map, current)
            dense = astar(
                nav_map,
                current,
                goal,
                snap_radius=int(self.cfg.get("snap_radius_cells", 10)),
            )
            waypoints = simplify_path(
                dense,
                spacing=int(self.cfg.get("waypoint_spacing_cells", 8)),
            )

            self.state.goal = goal
            self.state.path = dense
            self.state.waypoints = waypoints
            self.state.waypoint_index = 1 if len(waypoints) > 1 else 0

        waypoints = self.state.waypoints or []
        if not waypoints:
            return None, {"status": "world_no_waypoints", "position": current}

        reach = float(self.cfg.get("waypoint_reached_radius_cells", 3.5))
        while self.state.waypoint_index < len(waypoints):
            target = waypoints[self.state.waypoint_index]
            if math.hypot(target[0] - current[0], target[1] - current[1]) > reach:
                break
            self.state.waypoint_index += 1

        if self.state.waypoint_index >= len(waypoints):
            self.state.waypoints = None
            return self.plan_heading(minimap_player, minimap_shape)

        target = waypoints[self.state.waypoint_index]
        dx = target[0] - current[0]
        dy = target[1] - current[1]

        # GAT Y grows north/up, while screen/minimap image Y grows downward.
        map_heading = math.atan2(-dy, dx)

        return map_heading, {
            "status": "world_ok",
            "map": self.state.map_name,
            "position": current,
            "target": target,
            "goal": self.state.goal,
            "path_cells": len(self.state.path or []),
            "waypoint": self.state.waypoint_index,
            "waypoint_count": len(waypoints),
        }
