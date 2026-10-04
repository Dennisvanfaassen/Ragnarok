from __future__ import annotations

import math
from collections import deque

import cv2
import numpy as np


def _angle_diff(a: float, b: float) -> float:
    return math.atan2(math.sin(a - b), math.cos(a - b))


class MinimapNavigator:
    """Video-informed minimap navigation for the Soulbound map."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rotation_offset = float(cfg.get("rotation_offset_radians", 0.0))
        self.auto_calibrate = bool(cfg.get("auto_calibrate_rotation", True))

        self.last_player = None
        self.last_command_screen_heading = None
        self.last_desired_map_heading = None

        self.still_frames = 0
        self.visited = None
        self.last_mask = None
        self.last_path = None
        self.last_status = "init"

    def _crop(self, frame):
        h, w = frame.shape[:2]
        roi = self.cfg["roi"]
        x = int(float(roi["x_ratio"]) * w)
        y = int(float(roi["y_ratio"]) * h)
        rw = int(float(roi["width_ratio"]) * w)
        rh = int(float(roi["height_ratio"]) * h)
        return frame[y:y + rh, x:x + rw]

    def _find_player(self, minimap):
        hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array([0, 0, int(self.cfg.get("arrow_value_min", 205))], dtype=np.uint8),
            np.array([179, int(self.cfg.get("arrow_saturation_max", 85)), 255], dtype=np.uint8),
        )

        # Ignore UI edges/icons that can also be bright white.
        margin = int(self.cfg.get("arrow_border_margin_px", 5))
        if margin > 0:
            mask[:margin, :] = 0
            mask[-margin:, :] = 0
            mask[:, :margin] = 0
            mask[:, -margin:] = 0

        count, _labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []

        min_area = int(self.cfg.get("arrow_min_area", 5))
        max_area = int(self.cfg.get("arrow_max_area", 95))
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if not (min_area <= area <= max_area):
                continue
            if not (2 <= w <= 18 and 2 <= h <= 18):
                continue

            cx, cy = centers[i]
            aspect = max(w, h) / max(1, min(w, h))
            compact_bonus = 1.0 if aspect <= 3.5 else 0.0

            # Temporal tracking is much more reliable than "largest white blob":
            # during the video the arrow moves smoothly from frame to frame.
            if self.last_player is not None:
                dist = math.hypot(cx - self.last_player[0], cy - self.last_player[1])
                max_jump = float(self.cfg.get("arrow_max_jump_px", 18.0))
                if dist > max_jump:
                    continue
                score = 100.0 - dist * 4.0 + area * 0.3 + compact_bonus * 3.0
            else:
                # On first acquisition, prefer a small isolated marker away from UI edges.
                edge_dist = min(cx, cy, minimap.shape[1] - cx, minimap.shape[0] - cy)
                score = area * 0.3 + edge_dist * 0.1 + compact_bonus * 3.0

            candidates.append((score, int(round(cx)), int(round(cy))))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _, x, y = candidates[0]
        return x, y

    def _walkable_mask(self, minimap, player):
        hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]

        # Video observation: the usable dungeon route is consistently the
        # gray/low-saturation structure, while water/terrain is more saturated.
        mask = (
            (sat < int(self.cfg.get("walkable_saturation_max", 105)))
            & (val > int(self.cfg.get("walkable_value_min", 42)))
            & (val < int(self.cfg.get("walkable_value_max", 225)))
        ).astype(np.uint8) * 255

        # Remove the bottom strip where coordinate/UI icons tend to live.
        bottom_ignore = int(self.cfg.get("bottom_ui_ignore_px", 10))
        if bottom_ignore > 0:
            mask[-bottom_ignore:, :] = 0

        close_k = int(self.cfg.get("close_kernel", 3))
        if close_k > 1:
            kernel = np.ones((close_k, close_k), np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        # Keep only reasonably connected corridor material.
        count, labels, stats, _centers = cv2.connectedComponentsWithStats(mask, 8)
        cleaned = np.zeros_like(mask)
        min_component = int(self.cfg.get("min_walkable_component_area", 18))
        for i in range(1, count):
            if stats[i, cv2.CC_STAT_AREA] >= min_component:
                cleaned[labels == i] = 255

        if player is not None:
            cv2.circle(cleaned, player, int(self.cfg.get("player_connect_radius_px", 4)), 255, -1)

        return cleaned

    @staticmethod
    def _nearest_walkable(mask, player):
        px, py = player
        if 0 <= py < mask.shape[0] and 0 <= px < mask.shape[1] and mask[py, px]:
            return px, py

        ys, xs = np.where(mask > 0)
        if len(xs) == 0:
            return None

        distances = (xs - px) ** 2 + (ys - py) ** 2
        idx = int(np.argmin(distances))
        return int(xs[idx]), int(ys[idx])

    def _route_heading(self, mask, player):
        start = self._nearest_walkable(mask, player)
        if start is None:
            return None

        h, w = mask.shape
        sx, sy = start

        if self.visited is None or self.visited.shape != mask.shape:
            self.visited = np.zeros(mask.shape, dtype=np.float32)

        # Fade old history so the bot can revisit areas later instead of
        # permanently blacklisting them.
        self.visited *= float(self.cfg.get("visited_decay", 0.997))

        px, py = player
        if 0 <= py < h and 0 <= px < w:
            cv2.circle(
                self.visited,
                (px, py),
                int(self.cfg.get("visited_mark_radius_px", 4)),
                1.0,
                -1,
            )

        # Clearance map: staying in the center of a corridor is strongly
        # preferred over scraping water/walls.
        clearance = cv2.distanceTransform((mask > 0).astype(np.uint8), cv2.DIST_L2, 5)

        q = deque([(sx, sy)])
        parent = {(sx, sy): None}
        depth = {(sx, sy): 0}

        directions = [
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1),
        ]

        max_depth = int(self.cfg.get("route_search_pixels", 80))
        min_goal = int(self.cfg.get("minimum_goal_distance_pixels", 16))
        candidates = []

        while q:
            x, y = q.popleft()
            d = depth[(x, y)]

            if d >= min_goal:
                clear = float(clearance[y, x])
                seen = float(self.visited[y, x])

                # Prefer: long route + open corridor center + unexplored branch.
                score = (
                    d * float(self.cfg.get("distance_weight", 1.0))
                    + clear * float(self.cfg.get("clearance_weight", 4.0))
                    - seen * float(self.cfg.get("visited_penalty", 12.0))
                )

                # Avoid instantly turning back unless stuck.
                if self.last_desired_map_heading is not None:
                    angle = math.atan2(y - sy, x - sx)
                    turn = abs(_angle_diff(angle, self.last_desired_map_heading))
                    score -= turn * float(self.cfg.get("turn_penalty", 3.0))

                candidates.append((score, d, x, y))

            if d >= max_depth:
                continue

            for dx, dy in directions:
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if mask[ny, nx] == 0:
                    continue

                # Don't route through razor-thin edge pixels when a safer
                # corridor exists.
                if clearance[ny, nx] < float(self.cfg.get("minimum_clearance_px", 1.0)):
                    continue

                key = (nx, ny)
                if key in parent:
                    continue

                parent[key] = (x, y)
                depth[key] = d + 1
                q.append(key)

        if not candidates:
            return None

        candidates.sort(reverse=True)
        _score, _depth, gx, gy = candidates[0]

        path = []
        cur = (gx, gy)
        while cur is not None:
            path.append(cur)
            cur = parent[cur]
        path.reverse()
        self.last_path = path

        # Look far enough ahead to produce the smooth, long directional
        # movements seen in the supplied gameplay video.
        lookahead = min(
            len(path) - 1,
            int(self.cfg.get("path_lookahead_pixels", 14)),
        )
        tx, ty = path[max(1, lookahead)]

        dx = tx - sx
        dy = ty - sy
        if dx == 0 and dy == 0:
            return None

        return math.atan2(dy, dx)

    def observe_player(self, frame, command_screen_heading=None):
        """Track the minimap arrow without choosing a visual route.

        World-aware navigation uses this to obtain the player's minimap
        position and to keep learning the screen/minimap rotation offset.
        """
        minimap = self._crop(frame)
        if minimap.size == 0:
            self.last_status = "bad_roi"
            return None, {"status": self.last_status}

        player = self._find_player(minimap)
        if player is None:
            self.last_status = "arrow_not_found"
            return None, {"status": self.last_status}

        moved = 0.0
        delta = None
        if self.last_player is not None:
            dx = player[0] - self.last_player[0]
            dy = player[1] - self.last_player[1]
            delta = (float(dx), float(dy))
            moved = math.hypot(dx, dy)

            if moved >= float(self.cfg.get("calibration_min_move_pixels", 1.2)):
                self.still_frames = 0
                if self.auto_calibrate and self.last_command_screen_heading is not None:
                    observed_map_heading = math.atan2(dy, dx)
                    measured_offset = _angle_diff(
                        self.last_command_screen_heading,
                        observed_map_heading,
                    )
                    alpha = float(self.cfg.get("calibration_alpha", 0.18))
                    delta = _angle_diff(measured_offset, self.rotation_offset)
                    self.rotation_offset += alpha * delta
            else:
                self.still_frames += 1

        self.last_player = player
        if command_screen_heading is not None:
            self.last_command_screen_heading = command_screen_heading

        self.last_status = "tracking"
        return player, {
            "status": self.last_status,
            "player": player,
            "moved": moved,
            "delta": delta,
            "rotation_offset": self.rotation_offset,
            "minimap_shape": minimap.shape[:2],
        }

    def plan(self, frame, command_screen_heading=None):
        minimap = self._crop(frame)
        if minimap.size == 0:
            self.last_status = "bad_roi"
            return None, {"status": self.last_status}

        player = self._find_player(minimap)
        if player is None:
            self.last_status = "arrow_not_found"
            return None, {"status": self.last_status}

        moved = 0.0
        if self.last_player is not None:
            dx = player[0] - self.last_player[0]
            dy = player[1] - self.last_player[1]
            moved = math.hypot(dx, dy)

            if moved >= float(self.cfg.get("calibration_min_move_pixels", 1.2)):
                self.still_frames = 0

                if self.auto_calibrate and self.last_command_screen_heading is not None:
                    observed_map_heading = math.atan2(dy, dx)
                    measured_offset = _angle_diff(
                        self.last_command_screen_heading,
                        observed_map_heading,
                    )
                    alpha = float(self.cfg.get("calibration_alpha", 0.18))
                    delta = _angle_diff(measured_offset, self.rotation_offset)
                    self.rotation_offset += alpha * delta
            else:
                self.still_frames += 1

        self.last_player = player

        mask = self._walkable_mask(minimap, player)
        self.last_mask = mask
        map_heading = self._route_heading(mask, player)

        if map_heading is None:
            self.last_status = "no_route"
            return None, {
                "status": self.last_status,
                "player": player,
                "moved": moved,
            }

        stuck_frames = int(self.cfg.get("stuck_frames", 12))
        if self.still_frames >= stuck_frames:
            # Video-informed recovery: release the persistent direction by
            # choosing a strong side/back turn, rather than tiny oscillations
            # against the same shoreline/wall.
            if self.last_desired_map_heading is not None:
                sign = -1.0 if (int(player[0] + player[1]) % 2) else 1.0
                map_heading = (
                    self.last_desired_map_heading
                    + sign * float(self.cfg.get("stuck_turn_radians", 2.15))
                )
            self.still_frames = 0
            self.last_status = "stuck_recovery"
        else:
            self.last_status = "ok"

        # Smooth route changes so the held cursor behaves like the player's
        # continuous steering in the video rather than snapping at junctions.
        if self.last_desired_map_heading is not None:
            max_step = float(self.cfg.get("max_heading_change_radians", 0.32))
            delta = _angle_diff(map_heading, self.last_desired_map_heading)
            delta = max(-max_step, min(max_step, delta))
            map_heading = self.last_desired_map_heading + delta

        self.last_desired_map_heading = map_heading
        screen_heading = map_heading + self.rotation_offset
        self.last_command_screen_heading = screen_heading

        return screen_heading, {
            "status": self.last_status,
            "player": player,
            "moved": moved,
            "map_heading": map_heading,
            "rotation_offset": self.rotation_offset,
        }

    def save_debug(self, frame, path: str) -> None:
        minimap = self._crop(frame)
        if minimap.size == 0:
            return

        debug = minimap.copy()

        if self.last_mask is not None and self.last_mask.shape[:2] == debug.shape[:2]:
            overlay = np.zeros_like(debug)
            overlay[:, :, 1] = self.last_mask
            debug = cv2.addWeighted(debug, 0.72, overlay, 0.28, 0)

        if self.last_path:
            for a, b in zip(self.last_path[:-1], self.last_path[1:]):
                cv2.line(debug, a, b, (255, 255, 255), 1)

        if self.last_player is not None:
            cv2.circle(debug, self.last_player, 4, (255, 255, 255), 1)

        cv2.putText(
            debug,
            f"{self.last_status} off={self.rotation_offset:.2f}",
            (4, 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.33,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        cv2.imwrite(path, debug)
