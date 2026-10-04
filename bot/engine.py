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
        self.debug_requested = False

        target_cfg = self.config["targeting"]
        self.vision = Vision(
            float(target_cfg["template_threshold"]),
            float(target_cfg.get("grayscale_threshold", target_cfg["template_threshold"])),
            float(target_cfg.get("edge_threshold", 0.42)),
            [float(value) for value in target_cfg.get("scales", [1.0])],
        )

        keyboard.add_hotkey(self.config["bot"]["stop_hotkey"], self.stop)
        keyboard.add_hotkey(self.config["bot"]["pause_hotkey"], self.toggle_pause)
        keyboard.add_hotkey(self.config["bot"].get("debug_hotkey", "f10"), self.request_debug)

        self.last_heal = 0.0
        self.last_target_seen = time.monotonic()
        self.last_target_click = 0.0
        self.last_target_xy = None

    def stop(self):
        print("\n[BOT] Stop requested.")
        self.running = False

    def toggle_pause(self):
        self.paused = not self.paused
        print(f"\n[BOT] {'Paused' if self.paused else 'Resumed'}.")

    def request_debug(self):
        self.debug_requested = True

    def _handle_hp(self, frame) -> bool:
        hp_cfg = self.config["hp"]
        hp = self.vision.hp_percent(frame, hp_cfg["roi"])

        if hp is not None:
            print(f"\r[BOT] HP estimate: {hp:5.1f}%", end="", flush=True)

        if not hp_cfg.get("enabled", False) or hp is None:
            return False

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

    @staticmethod
    def _same_target(a, b, radius=55):
        if a is None or b is None:
            return False
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 <= radius

    def run(self):
        window_cfg = self.config["window"]
        hwnd, rect, title = find_game_window(
            window_cfg.get("process_names", []),
            window_cfg.get("title_contains", []),
        )
        focus_window(hwnd)

        width = rect[2] - rect[0]
        height = rect[3] - rect[1]

        player_cfg = self.config["player"]
        player_xy = (
            int(float(player_cfg["center_x_ratio"]) * width),
            int(float(player_cfg["center_y_ratio"]) * height),
        )

        print(f"[BOT] Game window found: {title}")
        print(f"[BOT] Client area: {width}x{height}")
        print(f"[BOT] Loaded {len(self.vision.templates)} monster template(s).")
        print(
            f"[BOT] {self.config['bot']['stop_hotkey'].upper()} stop | "
            f"{self.config['bot']['pause_hotkey'].upper()} pause | "
            f"{self.config['bot'].get('debug_hotkey', 'f10').upper()} debug screenshot"
        )

        if not self.vision.templates:
            raise RuntimeError("No PNG templates found in the templates folder.")

        bot_cfg = self.config["bot"]
        targeting = self.config["targeting"]
        movement = self.config["movement"]
        debug_cfg = self.config.get("debug", {})

        attack_wait = float(bot_cfg["attack_wait_seconds"])
        loop_delay = float(bot_cfg["loop_delay_ms"]) / 1000.0
        move_after = float(bot_cfg["no_target_move_after_seconds"])
        reclick_cooldown = float(bot_cfg.get("target_reclick_cooldown_seconds", 2.0))

        while self.running:
            if self.paused:
                time.sleep(0.15)
                continue

            try:
                hwnd, rect, _title = find_game_window(
                    window_cfg.get("process_names", []),
                    window_cfg.get("title_contains", []),
                )
                current_width = rect[2] - rect[0]
                current_height = rect[3] - rect[1]
                player_xy = (
                    int(float(player_cfg["center_x_ratio"]) * current_width),
                    int(float(player_cfg["center_y_ratio"]) * current_height),
                )

                frame = self.vision.capture(rect)

                if self._handle_hp(frame):
                    time.sleep(loop_delay)
                    continue

                detections = self.vision.find_targets(
                    frame,
                    player_xy,
                    targeting["excluded_regions"],
                    float(targeting["max_target_distance_px"]),
                    float(targeting.get("min_target_distance_px", 35)),
                )

                if self.debug_requested:
                    path = debug_cfg.get("output_path", "debug_last.jpg")
                    self.vision.save_debug(frame, detections, player_xy, path)
                    print(f"\n[BOT] Debug screenshot saved: {path}")
                    self.debug_requested = False

                if detections:
                    x, y, name, score, distance, _tw, _th = detections[0]
                    y += int(targeting.get("click_y_offset", 0))
                    now = time.monotonic()

                    target_xy = (x, y)
                    if (
                        not self._same_target(target_xy, self.last_target_xy)
                        or now - self.last_target_click >= reclick_cooldown
                    ):
                        print(
                            f"\n[BOT] Target {name} at ({x},{y}) "
                            f"score={score:.2f} distance={distance:.0f}px"
                        )
                        click_relative(rect, x, y)
                        self.last_target_click = now
                        self.last_target_xy = target_xy

                    self.last_target_seen = now
                    time.sleep(attack_wait)
                    continue

                self.last_target_xy = None

                if (
                    movement.get("enabled", True)
                    and time.monotonic() - self.last_target_seen >= move_after
                ):
                    mx, my = move_randomly(
                        rect,
                        player_xy,
                        targeting["excluded_regions"],
                        int(movement["min_radius_px"]),
                        int(movement["max_radius_px"]),
                        float(movement.get("click_delay_seconds", 0.20)),
                    )
                    print(f"\n[BOT] No target -> searching at ({mx},{my}).")
                    self.last_target_seen = time.monotonic()

                time.sleep(loop_delay)

            except Exception as exc:
                print(f"\n[BOT] Error: {exc}")
                time.sleep(0.5)

        print("\n[BOT] Stopped.")
