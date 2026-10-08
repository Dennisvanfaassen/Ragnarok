from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from core.pathing import normalize_map_name


class MonsterDensityTracker:
    """Persistent monster-presence heatmap.

    Counts visible monster presence at a low fixed cadence. Recent data is kept
    in 5-minute buckets for time-range filtering; a compact all-time aggregate
    is retained indefinitely.
    """

    def __init__(self, *, cell_size: int = 4, sample_interval: float = 2.0) -> None:
        self.cell_size = max(2, int(cell_size))
        self.sample_interval = max(0.5, float(sample_interval))
        self.bucket_seconds = 300
        self.recent_retention_seconds = 8 * 24 * 3600
        self._lock = threading.RLock()
        self._path = Path(__file__).resolve().parents[1] / "user_data" / "monster_density.json"
        self._session_started_at = time.time()
        self._session: dict[str, dict[str, dict[str, Any]]] = {}
        self._all_time: dict[str, dict[str, dict[str, Any]]] = {}
        self._buckets: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        self._last_sample_at: dict[str, float] = {}
        self._last_persist_at = 0.0
        self._observer_thread: threading.Thread | None = None
        self._observer_stop = threading.Event()
        self._load()

    @staticmethod
    def _blank_cell() -> dict[str, Any]:
        return {"total": 0, "monsters": {}}

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._all_time = raw.get("all_time") or {}
                self._buckets = raw.get("buckets") or {}
        except Exception:
            self._all_time = {}
            self._buckets = {}
        self._prune_recent()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "cell_size": self.cell_size,
                    "bucket_seconds": self.bucket_seconds,
                    "saved_at": time.time(),
                    "all_time": self._all_time,
                    "buckets": self._buckets,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)
        self._last_persist_at = time.time()

    def _prune_recent(self) -> None:
        cutoff = time.time() - self.recent_retention_seconds
        for map_name in list(self._buckets):
            rows = self._buckets.get(map_name) or {}
            for bucket_key in list(rows):
                try:
                    bucket_time = float(bucket_key)
                except Exception:
                    rows.pop(bucket_key, None)
                    continue
                if bucket_time < cutoff:
                    rows.pop(bucket_key, None)
            if not rows:
                self._buckets.pop(map_name, None)

    def _add(
        self,
        target: dict[str, dict[str, dict[str, Any]]],
        map_name: str,
        cell_key: str,
        monster_name: str,
    ) -> None:
        map_rows = target.setdefault(map_name, {})
        cell = map_rows.setdefault(cell_key, self._blank_cell())
        cell["total"] = int(cell.get("total") or 0) + 1
        monsters = cell.setdefault("monsters", {})
        monsters[monster_name] = int(monsters.get(monster_name) or 0) + 1

    def observe_snapshot(self, snapshot: dict[str, Any]) -> bool:
        live = snapshot.get("live_state") or {}
        world = live.get("world") or {}
        map_name = normalize_map_name(world.get("map"))
        if not map_name:
            return False

        now = time.time()
        with self._lock:
            last = float(self._last_sample_at.get(map_name) or 0.0)
            if now - last < self.sample_interval:
                return False
            self._last_sample_at[map_name] = now

            monsters: list[tuple[str, int, int]] = []
            for actor in live.get("actors") or []:
                if actor.get("kind") != "monster":
                    continue
                x, y = actor.get("x"), actor.get("y")
                if x is None or y is None:
                    continue
                name = str(actor.get("name") or "").strip() or "Unknown monster"
                monsters.append((name, int(x), int(y)))

            bucket_start = int(now // self.bucket_seconds) * self.bucket_seconds
            bucket_key = str(bucket_start)
            bucket_map = self._buckets.setdefault(map_name, {}).setdefault(bucket_key, {})

            for name, x, y in monsters:
                cx, cy = x // self.cell_size, y // self.cell_size
                cell_key = f"{cx},{cy}"
                self._add(self._session, map_name, cell_key, name)
                self._add(self._all_time, map_name, cell_key, name)

                cell = bucket_map.setdefault(cell_key, self._blank_cell())
                cell["total"] = int(cell.get("total") or 0) + 1
                names = cell.setdefault("monsters", {})
                names[name] = int(names.get(name) or 0) + 1

            if now - self._last_persist_at >= 15.0:
                self._prune_recent()
                self._save()
        return True

    def start_background_observer(self) -> None:
        with self._lock:
            if self._observer_thread and self._observer_thread.is_alive():
                return
            self._observer_stop.clear()
            self._observer_thread = threading.Thread(
                target=self._observer_loop,
                daemon=True,
                name="monster-density-observer",
            )
            self._observer_thread.start()

    def stop_background_observer(self) -> None:
        self._observer_stop.set()

    def _observer_loop(self) -> None:
        # Import lazily to avoid an import cycle during app startup.
        from diagnostics.authenticated_client import authenticated_client_monitor

        while not self._observer_stop.is_set():
            try:
                snapshot = authenticated_client_monitor.snapshot()
                if snapshot.get("classic_pid"):
                    self.observe_snapshot(snapshot)
            except Exception:
                pass
            self._observer_stop.wait(0.50)

    @staticmethod
    def _merge_cell(target: dict[str, Any], source: dict[str, Any]) -> None:
        target["total"] = int(target.get("total") or 0) + int(source.get("total") or 0)
        names = target.setdefault("monsters", {})
        for name, count in (source.get("monsters") or {}).items():
            names[str(name)] = int(names.get(str(name)) or 0) + int(count or 0)

    def _period_start(self, period: str, now: float) -> float | None:
        if period == "30m":
            return now - 30 * 60
        if period == "2h":
            return now - 2 * 3600
        if period == "today":
            lt = time.localtime(now)
            return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        if period == "7d":
            return now - 7 * 24 * 3600
        return None

    def _aggregate(
        self,
        map_name: str,
        period: str,
    ) -> dict[str, dict[str, Any]]:
        now = time.time()
        if period == "session":
            return {
                k: {"total": int(v.get("total") or 0), "monsters": dict(v.get("monsters") or {})}
                for k, v in (self._session.get(map_name) or {}).items()
            }
        if period == "all":
            return {
                k: {"total": int(v.get("total") or 0), "monsters": dict(v.get("monsters") or {})}
                for k, v in (self._all_time.get(map_name) or {}).items()
            }

        start = self._period_start(period, now)
        if start is None:
            start = now - 30 * 60

        out: dict[str, dict[str, Any]] = {}
        for bucket_key, cells in (self._buckets.get(map_name) or {}).items():
            try:
                bucket_time = float(bucket_key)
            except Exception:
                continue
            if bucket_time + self.bucket_seconds < start:
                continue
            for cell_key, row in (cells or {}).items():
                self._merge_cell(out.setdefault(cell_key, self._blank_cell()), row)
        return out

    def snapshot(
        self,
        map_name: str | None,
        *,
        period: str = "session",
        monster: str | None = None,
    ) -> dict[str, Any]:
        name = normalize_map_name(map_name)
        valid_periods = {"session", "30m", "2h", "today", "7d", "all"}
        period = period if period in valid_periods else "session"
        monster_filter = str(monster or "").strip()
        if monster_filter.lower() in {"", "all", "*"}:
            monster_filter = ""

        if not name:
            return {
                "status": "waiting",
                "map": None,
                "period": period,
                "monster": monster_filter or "all",
                "cell_size": self.cell_size,
                "cells": [],
                "monsters": [],
                "summary": {"samples": 0, "active_cells": 0, "max_density": 0},
            }

        with self._lock:
            aggregate = self._aggregate(name, period)
            known_names: dict[str, int] = defaultdict(int)
            for row in aggregate.values():
                for monster_name, count in (row.get("monsters") or {}).items():
                    known_names[str(monster_name)] += int(count or 0)

            cells: list[dict[str, Any]] = []
            max_density = 0
            total_samples = 0
            for key, row in aggregate.items():
                try:
                    cx, cy = [int(v) for v in key.split(",", 1)]
                except Exception:
                    continue
                monsters = row.get("monsters") or {}
                if monster_filter:
                    density = int(monsters.get(monster_filter) or 0)
                else:
                    density = int(row.get("total") or 0)
                if density <= 0:
                    continue
                max_density = max(max_density, density)
                total_samples += density
                top = sorted(
                    (
                        {"name": str(monster_name), "count": int(count or 0)}
                        for monster_name, count in monsters.items()
                        if int(count or 0) > 0
                    ),
                    key=lambda entry: (-entry["count"], entry["name"].lower()),
                )[:5]
                cells.append(
                    {
                        "cx": cx,
                        "cy": cy,
                        "x": cx * self.cell_size,
                        "y": cy * self.cell_size,
                        "width": self.cell_size,
                        "height": self.cell_size,
                        "density": density,
                        "top_monsters": top,
                    }
                )

            denom = max(1, max_density)
            for cell in cells:
                # sqrt normalization keeps medium-density regions visible while
                # still making the strongest hotspots clearly red.
                cell["intensity"] = round(math.sqrt(cell["density"] / denom), 4)

            options = [
                {"name": monster_name, "samples": count}
                for monster_name, count in sorted(
                    known_names.items(),
                    key=lambda row: (-row[1], row[0].lower()),
                )
            ]
            return {
                "status": "ready",
                "map": name,
                "period": period,
                "monster": monster_filter or "all",
                "cell_size": self.cell_size,
                "session_started_at": self._session_started_at,
                "cells": cells,
                "monsters": options,
                "summary": {
                    "samples": total_samples,
                    "active_cells": len(cells),
                    "max_density": max_density,
                },
            }


monster_density_tracker = MonsterDensityTracker()
