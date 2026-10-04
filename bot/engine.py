from __future__ import annotations

import math
import random
import time
from pathlib import Path

import keyboard
import yaml

from bot.controls import (
    begin_held_walk,
    choose_walk_point,
    click_relative,
    end_held_walk,
    loot_sweep,
    press_key,
    steer_held_walk,
)
from bot.vision import Vision
from bot.minimap import MinimapNavigator
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
        self.had_target_last_frame = False

        # Continuous roaming state.
        self.walking = False
        self.walk_heading = random.uniform(0, math.tau)
        self.walk_started = 0.0
        self.last_steer = 0.0
        self.next_major_turn = 0.0

        minimap_cfg = self.config.get("minimap", {})
        self.navigator = (
            MinimapNavigator(minimap_cfg)
            if minimap_cfg.get("enabled", False)
            else None
        )

    def _stop_walking(self):
        if self.walking:
            end_held_walk()
            self.walking = False

    def stop(self):
        self._stop_walking()
        print("\n[BOT] Stop requested.")
        self.running = False

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self._stop_walking()
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
            self._stop_walking()
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

    def _roam(self, hwnd, rect, player_xy, targeting, movement, frame):
        now = time.monotonic()
        radius = int(movement.get("hold_radius_px", 320))

        nav_heading = None
        nav_info = None
        if self.navigator is not None:
            nav_heading, nav_info = self.navigator.plan(
                frame,
                self.walk_heading if self.walking else None,
            )

        if nav_heading is not None:
            self.walk_heading = nav_heading
        elif not self.walking:
            self.walk_heading = (
                self.walk_heading
                + random.uniform(
                    -float(movement.get("new_heading_max_turn_radians", 1.2)),
                    float(movement.get("new_heading_max_turn_radians", 1.2)),
                )
            ) % math.tau

        x, y = choose_walk_point(
            rect,
            player_xy,
            targeting["excluded_regions"],
            self.walk_heading,
            radius,
        )

        if not self.walking:
            begin_held_walk(hwnd, rect, x, y)
            self.walking = True
            self.walk_started = now
            self.last_steer = now
            if nav_info and nav_info.get("status") == "ok":
                print(
                    f"\n[BOT] Minimap route -> ({x},{y}) "
                    f"offset={nav_info['rotation_offset']:.2f}."
                )
            else:
                print(f"\n[BOT] Roaming continuously toward ({x},{y}).")
            return

        steer_interval = float(movement.get("steer_interval_seconds", 0.45))
        if now - self.last_steer >= steer_interval:
            steer_held_walk(hwnd, rect, x, y)
            self.last_steer = now


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
        loot_cfg = self.config.get("loot", {})
        debug_cfg = self.config.get("debug", {})

        attack_wait = float(bot_cfg["attack_wait_seconds"])
        loop_delay = float(bot_cfg["loop_delay_ms"]) / 1000.0
        move_after = float(bot_cfg["no_target_move_after_seconds"])
        reclick_cooldown = float(bot_cfg.get("target_reclick_cooldown_seconds", 2.0))

        try:
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
                        if self.navigator is not None:
                            nav_path = debug_cfg.get(
                                "minimap_output_path",
                                "debug_minimap.jpg",
                            )
                            self.navigator.save_debug(frame, nav_path)
                            print(
                                f"\n[BOT] Debug screenshots saved: "
                                f"{path} + {nav_path}"
                            )
                        else:
                            print(f"\n[BOT] Debug screenshot saved: {path}")
                        self.debug_requested = False

                    if detections:
                        # Release held movement immediately before engaging.
                        self._stop_walking()

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
                                f"score={score:.2f} distance={distance:.0f}px -> CLICK"
                            )
                            click_relative(hwnd, rect, x, y)
                            self.last_target_click = now
                            self.last_target_xy = target_xy

                        self.last_target_seen = now
                        self.had_target_last_frame = True
                        time.sleep(attack_wait)
                        continue

                    if (
                        self.had_target_last_frame
                        and self.last_target_xy is not None
                        and loot_cfg.get("enabled", True)
                        and loot_cfg.get("sweep_after_target_disappears", True)
                    ):
                        self._stop_walking()
                        time.sleep(float(loot_cfg.get("settle_delay_seconds", 0.18)))
                        print("\n[BOT] Target disappeared -> looting drop area.")
                        loot_sweep(
                            hwnd,
                            rect,
                            self.last_target_xy,
                            int(loot_cfg.get("radius_px", 24)),
                            int(loot_cfg.get("rings", 2)),
                            int(loot_cfg.get("points_per_ring", 8)),
                            float(loot_cfg.get("click_delay_seconds", 0.045)),
                        )
                        self.last_target_seen = time.monotonic()

                    self.had_target_last_frame = False
                    self.last_target_xy = None

                    if (
                        movement.get("enabled", True)
                        and time.monotonic() - self.last_target_seen >= move_after
                    ):
                        self._roam(hwnd, rect, player_xy, targeting, movement, frame)

                    time.sleep(loop_delay)

                except Exception as exc:
                    self._stop_walking()
                    print(f"\n[BOT] Error: {exc}")
                    time.sleep(0.5)
        finally:
            self._stop_walking()

        print("\n[BOT] Stopped.")
