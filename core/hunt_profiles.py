from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from core.models import BotProfile
from core.pathing import normalize_map_name


class HuntProfileStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path = Path(__file__).resolve().parents[1] / "user_data" / "hunt_profiles.json"
        self._profiles: dict[str, dict[str, Any]] = {}
        self._active_id: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                rows = raw.get("profiles")
                if isinstance(rows, list):
                    self._profiles = {
                        str(row["id"]): row
                        for row in rows
                        if isinstance(row, dict) and row.get("id")
                    }
                self._active_id = raw.get("active_id")
        except Exception:
            self._profiles = {}
            self._active_id = None

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_id": self._active_id,
                    "profiles": list(self._profiles.values()),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    @staticmethod
    def _slug(value: str) -> str:
        text = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
        return text or "hunt"

    def list(self) -> dict[str, Any]:
        with self._lock:
            rows = [dict(row) for row in self._profiles.values()]
        rows.sort(key=lambda row: (
            str(row.get("map") or "").lower(),
            str(row.get("name") or "").lower(),
        ))
        return {"active_id": self._active_id, "profiles": rows}

    def get(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._profiles.get(str(profile_id))
            return dict(row) if row else None

    def save(
        self,
        *,
        name: str,
        profile: BotProfile,
        route: dict[str, Any] | None,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        title = str(name or "").strip()
        if not title:
            raise ValueError("Give this hunt profile a name.")

        payload = profile.model_dump(mode="json")
        map_name = normalize_map_name((payload.get("hunt") or {}).get("map"))
        if not map_name:
            raise ValueError("Choose a hunting map before saving a hunt profile.")

        now = time.time()
        with self._lock:
            existing = self._profiles.get(str(profile_id)) if profile_id else None
            pid = str(profile_id) if existing else (
                f"{self._slug(map_name)}-{self._slug(title)}-{uuid.uuid4().hex[:6]}"
            )
            created_at = float(existing.get("created_at") or now) if existing else now
            row = {
                "id": pid,
                "name": title,
                "map": map_name,
                "created_at": created_at,
                "updated_at": now,
                "profile": payload,
                "route": route or {
                    "map": map_name,
                    "mode": "loop",
                    "waypoints": [],
                    "exists": False,
                },
            }
            self._profiles[pid] = row
            self._active_id = pid
            self._save()
            return dict(row)

    def delete(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            existed = self._profiles.pop(str(profile_id), None)
            if self._active_id == str(profile_id):
                self._active_id = None
            self._save()
        return {"deleted": bool(existed), "active_id": self._active_id}

    def mark_active(self, profile_id: str | None) -> None:
        with self._lock:
            self._active_id = str(profile_id) if profile_id else None
            self._save()


hunt_profile_store = HuntProfileStore()
