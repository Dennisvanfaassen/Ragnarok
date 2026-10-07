from __future__ import annotations

import csv
import ctypes
import json
import shutil
import threading
import time
import zipfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

import mss
import mss.tools

from diagnostics.authenticated_client import authenticated_client_monitor
from diagnostics.native_action_bridge import native_action_bridge


user32 = ctypes.windll.user32
VK_LBUTTON = 0x01


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class HuntingDiagnosticRecorder:
    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.started_at: float | None = None
        self.session_dir: Path | None = None
        self.zip_path: Path | None = None
        self.event_count = 0
        self.sample_count = 0
        self.screenshot_count = 0
        self._last_position: tuple[int, int] | None = None
        self._last_position_change = 0.0
        self._last_stuck_shot = 0.0
        self._last_event_shot: dict[str, float] = {}

    @property
    def root(self) -> Path:
        path = Path(__file__).resolve().parents[1] / "diagnostics_output"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _classic_geometry(self) -> dict[str, int] | None:
        pid = authenticated_client_monitor.snapshot().get("classic_pid")
        if not pid:
            return None

        found = {"hwnd": None}
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
        )

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if int(process_id.value) != int(pid):
                return True
            rect = RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                return True
            if rect.right - rect.left < 300 or rect.bottom - rect.top < 200:
                return True
            origin = POINT(0, 0)
            if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
                return True
            found["hwnd"] = int(hwnd)
            found["left"] = int(origin.x)
            found["top"] = int(origin.y)
            found["width"] = int(rect.right - rect.left)
            found["height"] = int(rect.bottom - rect.top)
            return False

        user32.EnumWindows(enum_proc(callback), 0)
        if not found.get("hwnd"):
            return None
        return {
            "left": found["left"],
            "top": found["top"],
            "width": found["width"],
            "height": found["height"],
        }

    @staticmethod
    def _cursor() -> dict[str, Any]:
        p = POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return {
            "x": int(p.x),
            "y": int(p.y),
            "left_down": bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000),
        }

    def _write_jsonl(self, filename: str, payload: dict[str, Any]):
        if not self.session_dir:
            return
        path = self.session_dir / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _capture(self, label: str, *, cooldown: float = 0.0) -> str | None:
        if not self.running or not self.session_dir:
            return None
        now = time.time()
        if cooldown:
            previous = self._last_event_shot.get(label, 0.0)
            if now - previous < cooldown:
                return None
            self._last_event_shot[label] = now

        geometry = self._classic_geometry()
        if not geometry:
            return None

        safe = "".join(
            ch if ch.isalnum() or ch in "-_" else "_"
            for ch in label
        )[:48]
        self.screenshot_count += 1
        name = f"{self.screenshot_count:04d}_{int(now * 1000)}_{safe}.png"
        target = self.session_dir / "screenshots" / name
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            with mss.mss() as sct:
                raw = sct.grab(geometry)
                mss.tools.to_png(raw.rgb, raw.size, output=str(target))
            return f"screenshots/{name}"
        except Exception:
            return None

    def event(
        self,
        source: str,
        action: str,
        details: dict[str, Any] | None = None,
        *,
        screenshot: bool = False,
        screenshot_cooldown: float = 0.0,
    ):
        if not self.running:
            return

        now = time.time()
        shot = None
        if screenshot:
            shot = self._capture(
                f"{source}_{action}",
                cooldown=screenshot_cooldown,
            )

        payload = {
            "time": now,
            "elapsed": (
                round(now - self.started_at, 4)
                if self.started_at is not None
                else None
            ),
            "source": source,
            "action": action,
            "cursor": self._cursor(),
            "screenshot": shot,
            "details": details or {},
        }
        with self._lock:
            self.event_count += 1
            self._write_jsonl("events.jsonl", payload)

    def _sample(self):
        now = time.time()
        snapshot = authenticated_client_monitor.snapshot()
        live = snapshot.get("live_state") or {}
        world = live.get("world") or {}
        actors = live.get("actors") or []

        try:
            from core.hunting_ai import hunting_ai
            hunt = hunting_ai.snapshot()
            navigation = hunting_ai.diagnostic_navigation()
        except Exception:
            hunt = {}
            navigation = {}

        cursor = self._cursor()
        target = hunt.get("target") or {}
        route = (hunt.get("settings") or {}).get("saved_hunt_route") or {}
        try:
            native = native_action_bridge.snapshot()
            native_agent = native.get("agent") or {}
            native_recent_calls = list(native_agent.get("recent_calls") or [])
        except Exception:
            native = {}
            native_recent_calls = []

        record = {
            "time": now,
            "elapsed": (
                round(now - self.started_at, 4)
                if self.started_at is not None
                else None
            ),
            "map": world.get("map"),
            "player": {
                "x": world.get("x"),
                "y": world.get("y"),
            },
            "hunt": {
                "running": hunt.get("running"),
                "state": hunt.get("state"),
                "message": hunt.get("message"),
                "target": target,
                "saved_route": route,
                "navigation": navigation,
            },
            "cursor": cursor,
            "last_client_action": world.get("last_client_action"),
            "last_combat": world.get("last_combat"),
            "native_bridge": {
                "status": native.get("status"),
                "attached": native.get("attached"),
                "socket_learned": (native.get("agent") or {}).get("socket_learned"),
                "socket": (native.get("agent") or {}).get("socket"),
                "recent_calls": native_recent_calls,
            },
            "actors": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "kind": a.get("kind"),
                    "x": a.get("x"),
                    "y": a.get("y"),
                    "aggressive_to_me": a.get("aggressive_to_me"),
                }
                for a in actors
            ],
        }

        with self._lock:
            self.sample_count += 1
            self._write_jsonl("samples.jsonl", record)

        px, py = world.get("x"), world.get("y")
        if px is not None and py is not None:
            pos = (int(px), int(py))
            if self._last_position != pos:
                self._last_position = pos
                self._last_position_change = now
            elif (
                hunt.get("running")
                and hunt.get("state") == "WANDERING"
                and cursor.get("left_down")
                and now - self._last_position_change >= 0.8
                and now - self._last_stuck_shot >= 1.5
            ):
                self._last_stuck_shot = now
                shot = self._capture("movement_no_progress")
                self.event(
                    "recorder",
                    "movement_no_progress",
                    {
                        "position": {"x": pos[0], "y": pos[1]},
                        "state": hunt.get("state"),
                        "message": hunt.get("message"),
                        "captured": shot,
                    },
                )

    def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    self._sample()
                except Exception as exc:
                    self.event(
                        "recorder",
                        "sample_error",
                        {"error": str(exc)},
                    )
                self._stop.wait(0.10)
        finally:
            self.running = False

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return self.snapshot()

            stamp = time.strftime("%Y%m%d_%H%M%S")
            session = self.root / f"hunt_diag_{stamp}"
            suffix = 1
            while session.exists():
                session = self.root / f"hunt_diag_{stamp}_{suffix}"
                suffix += 1
            session.mkdir(parents=True)
            (session / "screenshots").mkdir()

            self.session_dir = session
            self.zip_path = None
            self.started_at = time.time()
            self.event_count = 0
            self.sample_count = 0
            self.screenshot_count = 0
            self._last_position = None
            self._last_position_change = self.started_at
            self._last_stuck_shot = 0.0
            self._last_event_shot.clear()
            self._stop.clear()
            self.running = True

            try:
                from core.mouse_adapter import mouse_game_adapter
                calibration = mouse_game_adapter.calibration_snapshot()
            except Exception:
                calibration = None

            metadata = {
                "started_at": self.started_at,
                "format_version": 2,
                "sample_interval_ms": 100,
                "classic_geometry": self._classic_geometry(),
                "calibration": calibration,
                "notes": (
                    "Passive game-state diagnostics plus normal Windows input telemetry. "
                    "Includes a rolling trace of recent outbound socket calls (length and first "
                    "32 packet bytes) from the native bridge; credentials are not recorded."
                ),
            }
            (session / "metadata.json").write_text(
                json.dumps(metadata, indent=2),
                encoding="utf-8",
            )

            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="hunt-diagnostic-recorder",
            )
            self._thread.start()

        self.event("recorder", "recording_started", screenshot=True)
        return self.snapshot()

    def _build_csvs(self):
        if not self.session_dir:
            return

        events: list[dict[str, Any]] = []
        events_path = self.session_dir / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        if events:
            rows = []
            for item in events:
                details = item.get("details") or {}
                rows.append({
                    "time": item.get("time"),
                    "elapsed": item.get("elapsed"),
                    "source": item.get("source"),
                    "action": item.get("action"),
                    "cursor_x": (item.get("cursor") or {}).get("x"),
                    "cursor_y": (item.get("cursor") or {}).get("y"),
                    "left_down": (item.get("cursor") or {}).get("left_down"),
                    "screenshot": item.get("screenshot"),
                    "details_json": json.dumps(details, ensure_ascii=False),
                })
            with (self.session_dir / "events.csv").open(
                "w", newline="", encoding="utf-8-sig"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

            attack_actions = {
                "attack_click",
                "instant_attack",
                "attack_attempt",
                "attack_precision_retry",
                "client_attack_registered",
                "combat_confirmed",
                "target_finished",
            }
            attack_rows = []
            for item in events:
                if item.get("action") not in attack_actions:
                    continue
                details = item.get("details") or {}
                attack_rows.append({
                    "time": item.get("time"),
                    "elapsed": item.get("elapsed"),
                    "source": item.get("source"),
                    "action": item.get("action"),
                    "cursor_x": (item.get("cursor") or {}).get("x"),
                    "cursor_y": (item.get("cursor") or {}).get("y"),
                    "left_down": (item.get("cursor") or {}).get("left_down"),
                    "screenshot": item.get("screenshot"),
                    "details_json": json.dumps(details, ensure_ascii=False),
                })
            if attack_rows:
                with (self.session_dir / "attacks.csv").open(
                    "w", newline="", encoding="utf-8-sig"
                ) as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=list(attack_rows[0].keys())
                    )
                    writer.writeheader()
                    writer.writerows(attack_rows)

        samples_path = self.session_dir / "samples.jsonl"
        movement_rows = []
        if samples_path.exists():
            for line in samples_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                player = item.get("player") or {}
                hunt = item.get("hunt") or {}
                nav = hunt.get("navigation") or {}
                cursor = item.get("cursor") or {}
                target = hunt.get("target") or {}
                goal = nav.get("wander_goal") or {}
                movement_rows.append({
                    "time": item.get("time"),
                    "elapsed": item.get("elapsed"),
                    "map": item.get("map"),
                    "player_x": player.get("x"),
                    "player_y": player.get("y"),
                    "hunt_state": hunt.get("state"),
                    "message": hunt.get("message"),
                    "cursor_x": cursor.get("x"),
                    "cursor_y": cursor.get("y"),
                    "left_down": cursor.get("left_down"),
                    "target_id": target.get("id"),
                    "target_x": target.get("x"),
                    "target_y": target.get("y"),
                    "goal_x": goal.get("x"),
                    "goal_y": goal.get("y"),
                    "straight_segment_index": nav.get("straight_segment_index"),
                    "wander_progress_index": nav.get("wander_progress_index"),
                    "astar_path_json": json.dumps(
                        nav.get("astar_path") or [],
                        ensure_ascii=False,
                    ),
                    "straight_segments_json": json.dumps(
                        nav.get("straight_segments") or [],
                        ensure_ascii=False,
                    ),
                })
        if movement_rows:
            with (self.session_dir / "movement.csv").open(
                "w", newline="", encoding="utf-8-sig"
            ) as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=list(movement_rows[0].keys())
                )
                writer.writeheader()
                writer.writerows(movement_rows)


    def stop(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

        if self.session_dir:
            self._build_csvs()
            ended = time.time()
            summary = {
                "started_at": self.started_at,
                "ended_at": ended,
                "duration_seconds": (
                    round(ended - self.started_at, 2)
                    if self.started_at is not None else None
                ),
                "events": self.event_count,
                "samples": self.sample_count,
                "screenshots": self.screenshot_count,
            }
            (self.session_dir / "summary.json").write_text(
                json.dumps(summary, indent=2),
                encoding="utf-8",
            )

            zip_path = self.session_dir.with_suffix(".zip")
            if zip_path.exists():
                zip_path.unlink()
            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for path in self.session_dir.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(self.session_dir))
            self.zip_path = zip_path

        self.running = False
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "event_count": self.event_count,
            "sample_count": self.sample_count,
            "screenshot_count": self.screenshot_count,
            "session_name": self.session_dir.name if self.session_dir else None,
            "zip_ready": bool(self.zip_path and self.zip_path.exists()),
            "zip_name": self.zip_path.name if self.zip_path else None,
        }

    def download_path(self) -> Path | None:
        if self.zip_path and self.zip_path.exists():
            return self.zip_path
        return None


hunting_diagnostic_recorder = HuntingDiagnosticRecorder()
