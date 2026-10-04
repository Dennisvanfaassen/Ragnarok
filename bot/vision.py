from __future__ import annotations

import math
from pathlib import Path

import cv2
import mss
import numpy as np


class Vision:
    def __init__(self, template_threshold: float = 0.80):
        self.template_threshold = template_threshold
        self.sct = mss.mss()
        self.templates = self._load_templates()

    def _load_templates(self):
        templates = []
        template_dir = Path("templates")
        template_dir.mkdir(exist_ok=True)
        for path in sorted(template_dir.glob("*.png")):
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is not None:
                templates.append((path.name, image))
        return templates

    def reload_templates(self) -> None:
        self.templates = self._load_templates()

    def capture(self, rect: tuple[int, int, int, int]) -> np.ndarray:
        x1, y1, x2, y2 = rect
        shot = self.sct.grab({
            "left": x1,
            "top": y1,
            "width": x2 - x1,
            "height": y2 - y1,
        })
        return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)

    @staticmethod
    def _inside_excluded(x, y, width, height, excluded_regions) -> bool:
        for x1r, y1r, x2r, y2r in excluded_regions:
            if x1r * width <= x <= x2r * width and y1r * height <= y <= y2r * height:
                return True
        return False

    def find_targets(
        self,
        frame: np.ndarray,
        player_xy: tuple[int, int],
        excluded_regions,
        max_distance: float,
    ):
        height, width = frame.shape[:2]
        px, py = player_xy
        detections = []

        for template_name, template in self.templates:
            th, tw = template.shape[:2]
            if th >= height or tw >= width:
                continue

            result = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= self.template_threshold)

            candidates = []
            for x, y in zip(xs, ys):
                cx = int(x + tw / 2)
                cy = int(y + th / 2)
                score = float(result[y, x])

                if self._inside_excluded(cx, cy, width, height, excluded_regions):
                    continue

                distance = math.hypot(cx - px, cy - py)
                if distance > max_distance:
                    continue

                # Basic non-max suppression: don't keep nearly identical hits.
                if any(math.hypot(cx - ox, cy - oy) < max(tw, th) * 0.5 for ox, oy, *_ in candidates):
                    continue

                candidates.append((cx, cy, template_name, score, distance))

            detections.extend(candidates)

        detections.sort(key=lambda d: (d[4], -d[3]))
        return detections

    @staticmethod
    def hp_percent(frame: np.ndarray, roi_cfg: dict) -> float | None:
        h, w = frame.shape[:2]
        x = int(roi_cfg["x_ratio"] * w)
        y = int(roi_cfg["y_ratio"] * h)
        rw = max(1, int(roi_cfg["width_ratio"] * w))
        rh = max(1, int(roi_cfg["height_ratio"] * h))

        roi = frame[y:y + rh, x:x + rw]
        if roi.size == 0:
            return None

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        # Ragnarok's player HP bar is green in the supplied client screenshot.
        mask = cv2.inRange(
            hsv,
            np.array([35, 65, 45], dtype=np.uint8),
            np.array([95, 255, 255], dtype=np.uint8),
        )

        col_has_hp = np.count_nonzero(mask, axis=0) > max(1, int(rh * 0.25))
        return float(np.count_nonzero(col_has_hp) / rw * 100.0)
