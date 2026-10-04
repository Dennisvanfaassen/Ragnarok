from __future__ import annotations

import math
from pathlib import Path

import cv2
import mss
import numpy as np


class Vision:
    def __init__(
        self,
        color_threshold: float,
        grayscale_threshold: float,
        edge_threshold: float,
        scales: list[float],
    ):
        self.color_threshold = color_threshold
        self.grayscale_threshold = grayscale_threshold
        self.edge_threshold = edge_threshold
        self.scales = scales
        self.sct = mss.mss()
        self.templates = self._load_templates()

    def _load_templates(self):
        templates = []
        template_dir = Path("templates")
        template_dir.mkdir(exist_ok=True)

        for path in sorted(template_dir.glob("*.png")):
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                continue

            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 45, 120)
            templates.append(
                {
                    "name": path.name,
                    "color": image,
                    "gray": gray,
                    "edges": edges,
                }
            )
        return templates

    def capture(self, rect: tuple[int, int, int, int]) -> np.ndarray:
        x1, y1, x2, y2 = rect
        shot = self.sct.grab(
            {
                "left": x1,
                "top": y1,
                "width": x2 - x1,
                "height": y2 - y1,
            }
        )
        return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)

    @staticmethod
    def _inside_excluded(x, y, width, height, excluded_regions) -> bool:
        for x1r, y1r, x2r, y2r in excluded_regions:
            if x1r * width <= x <= x2r * width and y1r * height <= y <= y2r * height:
                return True
        return False

    @staticmethod
    def _score_map(frame, template, method=cv2.TM_CCOEFF_NORMED):
        if (
            template.shape[0] >= frame.shape[0]
            or template.shape[1] >= frame.shape[1]
        ):
            return None
        return cv2.matchTemplate(frame, template, method)

    def find_targets(
        self,
        frame: np.ndarray,
        player_xy: tuple[int, int],
        excluded_regions,
        max_distance: float,
        min_distance: float,
    ):
        height, width = frame.shape[:2]
        px, py = player_xy

        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_edges = cv2.Canny(frame_gray, 45, 120)
        raw = []

        for entry in self.templates:
            for scale in self.scales:
                if abs(scale - 1.0) < 0.001:
                    color = entry["color"]
                    gray = entry["gray"]
                    edges = entry["edges"]
                else:
                    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                    color = cv2.resize(
                        entry["color"], None, fx=scale, fy=scale, interpolation=interpolation
                    )
                    gray = cv2.resize(
                        entry["gray"], None, fx=scale, fy=scale, interpolation=interpolation
                    )
                    edges = cv2.resize(
                        entry["edges"], None, fx=scale, fy=scale, interpolation=interpolation
                    )

                th, tw = gray.shape[:2]
                if th < 12 or tw < 12:
                    continue

                color_map = self._score_map(frame, color)
                gray_map = self._score_map(frame_gray, gray)
                edge_map = self._score_map(frame_edges, edges)

                if color_map is None or gray_map is None or edge_map is None:
                    continue

                # Use the strongest of three visual signals. This is much more tolerant
                # of water/ground background changes and sprite animation than one RGB match.
                combined = np.maximum(
                    color_map,
                    np.maximum(gray_map * 0.98, edge_map * 0.82),
                )
                ys, xs = np.where(
                    (combined >= self.color_threshold)
                    | (gray_map >= self.grayscale_threshold)
                    | (edge_map >= self.edge_threshold)
                )

                for x, y in zip(xs, ys):
                    cx = int(x + tw / 2)
                    cy = int(y + th / 2)

                    if self._inside_excluded(
                        cx, cy, width, height, excluded_regions
                    ):
                        continue

                    distance = math.hypot(cx - px, cy - py)
                    if distance > max_distance or distance < min_distance:
                        continue

                    score = float(
                        max(
                            color_map[y, x],
                            gray_map[y, x] * 0.98,
                            edge_map[y, x] * 0.82,
                        )
                    )
                    raw.append(
                        (cx, cy, entry["name"], score, distance, tw, th)
                    )

        # Non-max suppression across all templates/scales.
        raw.sort(key=lambda item: item[3], reverse=True)
        kept = []
        for item in raw:
            x, y, name, score, distance, tw, th = item
            duplicate = False
            for existing in kept:
                ex, ey = existing[0], existing[1]
                if math.hypot(x - ex, y - ey) < max(18, min(tw, th) * 0.55):
                    duplicate = True
                    break
            if not duplicate:
                kept.append(item)

        # Prefer nearby monsters, with score as a tie breaker.
        kept.sort(key=lambda item: (item[4], -item[3]))
        return kept

    @staticmethod
    def hp_percent(frame: np.ndarray, roi_cfg: dict) -> float | None:
        h, w = frame.shape[:2]
        x = int(roi_cfg["x_ratio"] * w)
        y = int(roi_cfg["y_ratio"] * h)
        rw = max(1, int(roi_cfg["width_ratio"] * w))
        rh = max(1, int(roi_cfg["height_ratio"] * h))

        roi = frame[y : y + rh, x : x + rw]
        if roi.size == 0:
            return None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array([35, 60, 40], dtype=np.uint8),
            np.array([100, 255, 255], dtype=np.uint8),
        )
        col_has_hp = np.count_nonzero(mask, axis=0) > max(1, int(rh * 0.20))
        return float(np.count_nonzero(col_has_hp) / rw * 100.0)

    @staticmethod
    def save_debug(frame, detections, player_xy, path: str) -> None:
        debug = frame.copy()
        px, py = player_xy
        cv2.circle(debug, (px, py), 8, (255, 255, 255), 2)

        for detection in detections[:20]:
            x, y, name, score, distance, tw, th = detection
            cv2.rectangle(
                debug,
                (int(x - tw / 2), int(y - th / 2)),
                (int(x + tw / 2), int(y + th / 2)),
                (255, 255, 255),
                1,
            )
            cv2.putText(
                debug,
                f"{name} {score:.2f}",
                (x - 30, y - int(th / 2) - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )
        cv2.imwrite(path, debug)
