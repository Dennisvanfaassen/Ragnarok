from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path
import os

import numpy as np

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
        db_path = self._resolve_database_path(cfg.get("database_path"))
        self.db = WorldDatabase(db_path)
        self.state = WorldRouteState(map_name=str(cfg.get("current_map", "")).lower())

        # World-vector -> screen-vector calibration. Ragnarok uses an
        # isometric projection, so a pure angle/rotation is not sufficient.
        # Seed with the standard RO isometric basis and refine it from actual
        # minimap movement while the bot walks.
        self._screen_matrix = np.array(
            cfg.get(
                "world_to_screen_matrix",
                [[1.0, 0.52], [-1.0, 0.52]],
            ),
            dtype=np.float64,
        )
        self._calibration_world = []
        self._calibration_screen = []
        self._goal_visits: dict[tuple[int, int], int] = {}

        destination = cfg.get("destination")
        self.fixed_destination = None
        if destination:
            self.fixed_destination = (
                int(destination["x"]),
                int(destination["y"]),
            )

    @staticmethod
    def _resolve_database_path(configured):
        candidates = []
        if configured:
            candidates.append(Path(configured))

        here = Path(__file__).resolve().parent
        repo_root = here.parent
        candidates.extend([
            repo_root / "world_db" / "processed",
            repo_root.parent / "Ragnarokmap" / "processed",
            Path(os.path.expandvars(r"%USERPROFILE%")) / "Desktop" / "Ragnarokmap" / "processed",
        ])

        for candidate in candidates:
            candidate = candidate.expanduser()
            if (candidate / "maps.json").exists():
                return candidate

        pretty = "\n  - ".join(str(p) for p in candidates)
        raise FileNotFoundError(
            "Could not find the processed Ragnarok world database. Tried:\n  - "
            + pretty
        )

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
        """Convert minimap pixels to GAT cells using aspect-fit scaling.

        Ragnarok fits the full map into the minimap while preserving aspect
        ratio. Using the actual GAT width/height is much more accurate than
        guessed percentage padding.
        """
        mh, mw = minimap_shape
        px, py = minimap_player

        scale = min(
            mw / max(1.0, float(nav_map.width)),
            mh / max(1.0, float(nav_map.height)),
        )
        display_w = nav_map.width * scale
        display_h = nav_map.height * scale
        offset_x = (mw - display_w) * 0.5
        offset_y = (mh - display_h) * 0.5

        gx = int(round((px - offset_x) / max(scale, 1e-6)))
        image_gy = (py - offset_y) / max(scale, 1e-6)

        if self.cfg.get("invert_y", True):
            gy = int(round((nav_map.height - 1) - image_gy))
        else:
            gy = int(round(image_gy))

        gx = max(0, min(nav_map.width - 1, gx))
        gy = max(0, min(nav_map.height - 1, gy))
        return gx, gy

    def observe_motion(
        self,
        minimap_delta: tuple[float, float] | None,
        commanded_screen_heading: float | None,
    ) -> None:
        """Learn the isometric/camera transform from real movement."""
        if minimap_delta is None or commanded_screen_heading is None:
            return

        dx, dy_image = minimap_delta
        if math.hypot(dx, dy_image) < float(
            self.cfg.get("transform_min_motion_pixels", 1.0)
        ):
            return

        # Minimap image Y increases downward; GAT/world Y increases upward.
        world = np.array([dx, -dy_image], dtype=np.float64)
        norm = np.linalg.norm(world)
        if norm <= 1e-6:
            return
        world /= norm

        screen = np.array(
            [math.cos(commanded_screen_heading), math.sin(commanded_screen_heading)],
            dtype=np.float64,
        )

        self._calibration_world.append(world)
        self._calibration_screen.append(screen)
        keep = int(self.cfg.get("transform_sample_count", 40))
        self._calibration_world = self._calibration_world[-keep:]
        self._calibration_screen = self._calibration_screen[-keep:]

        if len(self._calibration_world) < 6:
            return

        w = np.vstack(self._calibration_world)
        scr = np.vstack(self._calibration_screen)

        # Only fit when movement samples cover at least two independent axes.
        if np.linalg.matrix_rank(w) < 2:
            return

        fitted, _residuals, _rank, _singular = np.linalg.lstsq(w, scr, rcond=None)
        alpha = float(self.cfg.get("transform_learning_alpha", 0.15))
        self._screen_matrix = (
            (1.0 - alpha) * self._screen_matrix
            + alpha * fitted
        )

    def world_vector_to_screen_heading(self, dx: float, dy: float) -> float:
        vec = np.array([dx, dy], dtype=np.float64)
        screen = vec @ self._screen_matrix
        if np.linalg.norm(screen) <= 1e-6:
            return 0.0
        return math.atan2(float(screen[1]), float(screen[0]))

    def _choose_exploration_goal(self, nav_map, start: tuple[int, int]) -> tuple[int, int]:
        """Choose a sensible patrol destination instead of a random cell.

        We sample the real walkable grid and prefer destinations that are far
        enough away and have been used least often. This produces long,
        map-covering patrol routes rather than visibly random wandering.
        """
        min_dist = int(self.cfg.get("exploration_min_distance_cells", 45))
        max_dist = int(self.cfg.get("exploration_max_distance_cells", 120))
        stride = max(4, int(self.cfg.get("exploration_sample_stride", 12)))

        candidates = []
        for y in range(0, nav_map.height, stride):
            for x in range(0, nav_map.width, stride):
                if not nav_map.walkable(x, y):
                    continue
                dist = math.hypot(x - start[0], y - start[1])
                if dist < min_dist or dist > max_dist:
                    continue

                visits = self._goal_visits.get((x, y), 0)
                score = dist - visits * float(
                    self.cfg.get("exploration_repeat_penalty", 80.0)
                )
                candidates.append((score, -visits, dist, x, y))

        if not candidates:
            # Fall back to any distant walkable point if the radius ring is
            # sparse on an unusual map.
            for y in range(0, nav_map.height, stride):
                for x in range(0, nav_map.width, stride):
                    if not nav_map.walkable(x, y):
                        continue
                    dist = math.hypot(x - start[0], y - start[1])
                    if dist >= min_dist:
                        visits = self._goal_visits.get((x, y), 0)
                        score = dist - visits * float(
                            self.cfg.get("exploration_repeat_penalty", 80.0)
                        )
                        candidates.append((score, -visits, dist, x, y))

        if not candidates:
            raise RuntimeError("Could not find a walkable exploration goal.")

        candidates.sort(reverse=True)
        _score, _neg_visits, _dist, x, y = candidates[0]
        self._goal_visits[(x, y)] = self._goal_visits.get((x, y), 0) + 1
        return x, y

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

        if goal_reached and self.fixed_destination is not None:
            return False

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

        if self.fixed_destination is not None:
            if math.hypot(
                current[0] - self.fixed_destination[0],
                current[1] - self.fixed_destination[1],
            ) <= float(self.cfg.get("goal_reached_radius_cells", 4.0)):
                return None, {
                    "status": "world_arrived",
                    "map": self.state.map_name,
                    "position": current,
                    "goal": self.fixed_destination,
                }

        if self._needs_new_route(current):
            goal = (
                self.fixed_destination
                if self.fixed_destination is not None
                else self._choose_exploration_goal(nav_map, current)
            )
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
            if self.fixed_destination is not None:
                return None, {
                    "status": "world_arrived",
                    "map": self.state.map_name,
                    "position": current,
                    "goal": self.fixed_destination,
                }
            self.state.waypoints = None
            return self.plan_heading(minimap_player, minimap_shape)

        target = waypoints[self.state.waypoint_index]
        dx = target[0] - current[0]
        dy = target[1] - current[1]

        screen_heading = self.world_vector_to_screen_heading(dx, dy)

        return screen_heading, {
            "status": "world_ok",
            "map": self.state.map_name,
            "position": current,
            "target": target,
            "goal": self.state.goal,
            "path_cells": len(self.state.path or []),
            "waypoint": self.state.waypoint_index,
            "waypoint_count": len(waypoints),
            "world_delta": (dx, dy),
        }
