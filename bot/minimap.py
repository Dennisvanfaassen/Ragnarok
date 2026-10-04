from __future__ import annotations

import math
from collections import deque

import cv2
import numpy as np


def _angle_diff(a: float, b: float) -> float:
    return math.atan2(math.sin(a - b), math.cos(a - b))


class MinimapNavigator:
    """Reads Ragnarok's minimap and returns a local walkable heading."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rotation_offset = float(cfg.get("rotation_offset_radians", 0.0))
        self.auto_calibrate = bool(cfg.get("auto_calibrate_rotation", True))
        self.last_player = None
        self.last_command_screen_heading = None
        self.still_frames = 0
        self.visited = None
        self.last_desired_map_heading = None

    def _crop(self, frame):
        h, w = frame.shape[:2]
        roi = self.cfg["roi"]
        x = int(float(roi["x_ratio"]) * w)
        y = int(float(roi["y_ratio"]) * h)
        rw = int(float(roi["width_ratio"]) * w)
        rh = int(float(roi["height_ratio"]) * h)
        return frame[y:y + rh, x:x + rw]

    @staticmethod
    def _find_player(minimap):
        hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)

        # The player arrow is the bright white object on this Soulbound minimap.
        mask = cv2.inRange(
            hsv,
            np.array([0, 0, 205], dtype=np.uint8),
            np.array([179, 90, 255], dtype=np.uint8),
        )

        count, _labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []
        for i in range(1, count):
            x, y, w, h, area = stats[i]
            if 5 <= area <= 100 and 3 <= w <= 18 and 3 <= h <= 18:
                candidates.append((area, centers[i]))

        if not candidates:
            return None

        # The arrow is normally the largest small bright component.
        candidates.sort(key=lambda item: item[0], reverse=True)
        cx, cy = candidates[0][1]
        return int(round(cx)), int(round(cy))

    @staticmethod
    def _walkable_mask(minimap, player):
        hsv = cv2.cvtColor(minimap, cv2.COLOR_BGR2HSV)
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]

        # Soulbound's dungeon minimap: walkable corridors are gray/low-saturation,
        # while inaccessible terrain/background is brown and therefore saturated.
        mask = ((sat < 92) & (val > 48) & (val < 210)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)

        if player is not None:
            cv2.circle(mask, player, 3, 255, -1)
        return mask

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

        # Remember areas already traversed so junction choice slowly favors
        # unexplored corridors rather than oscillating between two branches.
        px, py = player
        if 0 <= py < h and 0 <= px < w:
            cv2.circle(self.visited, (px, py), 3, 1.0, -1)

        q = deque([(sx, sy)])
        parent = {(sx, sy): None}
        depth = {(sx, sy): 0}

        directions = [
            (1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1),
        ]

        max_depth = int(self.cfg.get("route_search_pixels", 70))
        candidates = []

        while q:
            x, y = q.popleft()
            d = depth[(x, y)]

            if d >= int(self.cfg.get("minimum_goal_distance_pixels", 14)):
                visit_penalty = float(self.visited[y, x]) * float(
                    self.cfg.get("visited_penalty", 9.0)
                )
                candidates.append((d - visit_penalty, d, x, y))

            if d >= max_depth:
                continue

            for dx, dy in directions:
                nx, ny = x + dx, y + dy
                if not (0 <= nx < w and 0 <= ny < h):
                    continue
                if mask[ny, nx] == 0:
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
        _score, _d, gx, gy = candidates[0]

        path = []
        cur = (gx, gy)
        while cur is not None:
            path.append(cur)
            cur = parent[cur]
        path.reverse()

        lookahead = min(
            len(path) - 1,
            int(self.cfg.get("path_lookahead_pixels", 10)),
        )
        tx, ty = path[max(1, lookahead)]
        dx = tx - sx
        dy = ty - sy

        if dx == 0 and dy == 0:
            return None
        return math.atan2(dy, dx)

    def plan(self, frame, command_screen_heading=None):
        minimap = self._crop(frame)
        if minimap.size == 0:
            return None, {"status": "bad_roi"}

        player = self._find_player(minimap)
        if player is None:
            return None, {"status": "arrow_not_found"}

        # Learn how screen direction maps to north-up minimap direction from
        # actual movement. This automatically compensates for camera rotation.
        if self.last_player is not None:
            dx = player[0] - self.last_player[0]
            dy = player[1] - self.last_player[1]
            distance = math.hypot(dx, dy)

            if distance >= float(self.cfg.get("calibration_min_move_pixels", 1.5)):
                self.still_frames = 0
                if (
                    self.auto_calibrate
                    and self.last_command_screen_heading is not None
                ):
                    observed_map_heading = math.atan2(dy, dx)
                    measured_offset = _angle_diff(
                        self.last_command_screen_heading,
                        observed_map_heading,
                    )
                    alpha = float(self.cfg.get("calibration_alpha", 0.22))
                    delta = _angle_diff(measured_offset, self.rotation_offset)
                    self.rotation_offset += alpha * delta
            else:
                self.still_frames += 1

        self.last_player = player

        mask = self._walkable_mask(minimap, player)
        map_heading = self._route_heading(mask, player)
        if map_heading is None:
            return None, {"status": "no_route", "player": player}

        # If we have not moved for a while, bias away from the previous heading
        # so the bot backs out of corners rather than holding into a wall.
        stuck_frames = int(self.cfg.get("stuck_frames", 10))
        if self.still_frames >= stuck_frames and self.last_desired_map_heading is not None:
            map_heading = self.last_desired_map_heading + math.pi * 0.75
            self.still_frames = 0

        self.last_desired_map_heading = map_heading
        screen_heading = map_heading + self.rotation_offset
        self.last_command_screen_heading = screen_heading

        return screen_heading, {
            "status": "ok",
            "player": player,
            "map_heading": map_heading,
            "rotation_offset": self.rotation_offset,
        }
