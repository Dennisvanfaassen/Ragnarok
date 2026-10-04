from __future__ import annotations

import time
from pathlib import Path

import keyboard
import yaml

from bot.controls import click_relative, move_randomly, press_key
from bot.vision import Vision
from bot.window import find_game_window, focus_window


class RagnarokBot:
    def __init__(self, config_path: str):
        self.config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        self.running = True
        self.paused = False

        threshold = float(self.config["targeting"]["template_threshold"])
        self.vision = Vision(threshold)

        keyboard.add_hotkey(self.config["bot"]["stop_hotkey"], self.stop)
        keyboard.add_hotkey(self.config["bot"]["pause_hotkey"], self.toggle_pause)

        self.last_heal = 0.0
        self.last_target_seen = time.monotonic()

    def stop(self):
        print("[BOT] Stop requested.")
        self.running = False

    def toggle_pause(self):
        self.paused = not self.paused
        print(f"[BOT] {'Paused' if self.paused else 'Resumed'}.")

    def _handle_hp(self, frame) -> bool:
        hp_cfg = self.config["hp"]
        hp = self.vision.hp_percent(frame, hp_cfg["roi"])

        if hp is None:
            return False

        print(f"\r[BOT] HP estimate: {hp:5.1f}%", end="", flush=True)

        now = time.monotonic()
        cooldown = float(hp_cfg["cooldown_seconds"])

        if hp <= float(hp_cfg["emergency_below_percent"]):
            if now - self.last_heal >= cooldown:
                print("\n[BOT] Critical HP -> emergency key.")
                press_key(hp_cfg["emergency_key"])
                self.last_heal = now
                time.sleep(0.4)
            return True

        if hp <= float(hp_cfg["heal_below_percent"]):
            if now - self.last_heal >= cooldown:
                print("\n[BOT] HP low -> heal.")
                press_key(hp_cfg["heal_key"])
                self.last_heal = now

        return False

    def run(self):
        title_candidates = self.config["window"]["title_contains"]
        hwnd, rect = find_game_window(title_candidates)
        focus_window(hwnd)

        width = rect[2] - rect[0]
        height = rect[3] - rect[1]

        player_cfg = self.config["player"]
        player_xy = (
            int(float(player_cfg["center_x_ratio"]) * width),
            int(float(player_cfg["center_y_ratio"]) * height),
        )

        print("[BOT] Ragnarok window found.")
        print(f"[BOT] Client area: {width}x{height}")
        print(f"[BOT] Loaded {len(self.vision.templates)} monster template(s).")
        print(
            f"[BOT] {self.config['bot']['stop_hotkey'].upper()} stops | "
            f"{self.config['bot']['pause_hotkey'].upper()} pauses."
        )

        if not self.vision.templates:
            print(
                "[BOT] No templates found. Add cropped monster PNG files to /templates first."
            )

        attack_wait = float(self.config["bot"]["attack_wait_seconds"])
        loop_delay = float(self.config["bot"]["loop_delay_ms"]) / 1000.0
        move_after = float(self.config["bot"]["no_target_move_after_seconds"])

        while self.running:
            if self.paused:
                time.sleep(0.15)
                continue

            try:
                hwnd, rect = find_game_window(title_candidates)
                frame = self.vision.capture(rect)

                if self._handle_hp(frame):
                    time.sleep(loop_delay)
                    continue

                targeting = self.config["targeting"]
                detections = self.vision.find_targets(
                    frame,
                    player_xy,
                    targeting["excluded_regions"],
                    float(targeting["max_target_distance_px"]),
                )

                if detections:
                    x, y, name, score, distance = detections[0]
                    y += int(targeting.get("click_y_offset", 0))
                    print(
                        f"\n[BOT] Target {name} at ({x},{y}) "
                        f"score={score:.2f} distance={distance:.0f}px"
                    )
                    click_relative(rect, x, y)
                    self.last_target_seen = time.monotonic()
                    time.sleep(attack_wait)
                    continue

                if (
                    self.config["movement"]["enabled"]
                    and time.monotonic() - self.last_target_seen >= move_after
                ):
                    move_cfg = self.config["movement"]
                    print("\n[BOT] No target -> searching.")
                    move_randomly(
                        rect,
                        player_xy,
                        int(move_cfg["min_radius_px"]),
                        int(move_cfg["max_radius_px"]),
                    )
                    self.last_target_seen = time.monotonic()

                time.sleep(loop_delay)

            except Exception as exc:
                print(f"\n[BOT] Error: {exc}")
                time.sleep(0.5)

        print("\n[BOT] Stopped.")
