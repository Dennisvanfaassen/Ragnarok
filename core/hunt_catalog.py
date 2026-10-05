from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from diagnostics.authenticated_client import authenticated_client_monitor


RAW_ROOT = "https://raw.githubusercontent.com/rathena/rathena/master"
MOB_DB_URL = f"{RAW_ROOT}/db/re/mob_db.yml"
MOB_DIRS = (
    "dungeons",
    "fields",
    "cities",
    "guild",
    "instances",
    "other",
    "new_1",
)


def _clean_map(name: str | None) -> str:
    return re.sub(r"\.(gat|rsw)$", "", str(name or "").strip().lower())


def _spawn_stem(map_name: str) -> str:
    name = _clean_map(map_name)
    # Examples:
    # iz_dun02 -> iz_dun
    # prt_fild08 -> prt_fild
    # gef_fild07 -> gef_fild
    # pay_dun00 -> pay_dun
    # prontera -> prontera
    return re.sub(r"\d+$", "", name).rstrip("_")


class HuntCatalog:
    """Reference + live-learned map/monster/drop catalog.

    rAthena is used only as a reference catalog. SoulBound observations are
    merged in and labeled separately because a private server can customize
    spawns and drops.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.mob_db_cache = self.cache_dir / "rathena_mob_db.yml"
        self._mob_db: dict[int, dict[str, Any]] | None = None
        self._map_cache: dict[str, list[dict[str, Any]]] = {}
        self._observed: dict[str, dict[str, dict[str, Any]]] = {}

    def _download_text(self, url: str, cache_path: Path | None = None) -> str:
        if cache_path and cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="replace")
        req = urllib.request.Request(url, headers={"User-Agent": "RO-Control/0.1"})
        with urllib.request.urlopen(req, timeout=8) as response:
            text = response.read().decode("utf-8", errors="replace")
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8")
        return text

    def _load_mob_db(self) -> dict[int, dict[str, Any]]:
        if self._mob_db is not None:
            return self._mob_db

        db: dict[int, dict[str, Any]] = {}
        try:
            text = self._download_text(MOB_DB_URL, self.mob_db_cache)
            parsed = yaml.safe_load(text) or {}
            rows = parsed.get("Body") if isinstance(parsed, dict) else None
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                try:
                    mob_id = int(row.get("Id"))
                except Exception:
                    continue
                drops = []
                for drop in row.get("Drops") or []:
                    if not isinstance(drop, dict):
                        continue
                    item = str(drop.get("Item") or "").strip()
                    if not item:
                        continue
                    drops.append({
                        "name": item.replace("_", " "),
                        "aegis_name": item,
                        "rate": int(drop.get("Rate") or 0),
                        "steal_protected": bool(drop.get("StealProtected", False)),
                    })
                db[mob_id] = {
                    "id": mob_id,
                    "name": str(row.get("Name") or row.get("AegisName") or f"Mob #{mob_id}"),
                    "aegis_name": str(row.get("AegisName") or ""),
                    "level": row.get("Level"),
                    "drops": drops,
                }
        except Exception:
            db = {}

        self._mob_db = db
        return db

    def _parse_spawn_file(self, text: str, target_map: str) -> list[dict[str, Any]]:
        target = _clean_map(target_map)
        merged: dict[int, dict[str, Any]] = {}
        mob_db = self._load_mob_db()

        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("//") or "\tmonster\t" not in line:
                continue
            try:
                left, rest = line.split("\tmonster\t", 1)
                map_name = _clean_map(left.split(",", 1)[0])
                if map_name != target:
                    continue
                display_name, numbers = rest.split("\t", 1)
                fields = [x.strip() for x in numbers.split(",")]
                mob_id = int(fields[0])
                count = int(fields[1]) if len(fields) > 1 and fields[1] else 1
            except Exception:
                continue

            info = mob_db.get(mob_id) or {}
            entry = merged.setdefault(mob_id, {
                "id": mob_id,
                "name": str(info.get("name") or display_name).strip(),
                "spawn_count": 0,
                "level": info.get("level"),
                "drops": list(info.get("drops") or []),
                "sources": ["rathena_reference"],
                "observed_live": False,
            })
            entry["spawn_count"] += max(0, count)

        return sorted(merged.values(), key=lambda row: (-int(row.get("spawn_count") or 0), row["name"]))

    def _reference_monsters(self, map_name: str) -> list[dict[str, Any]]:
        map_name = _clean_map(map_name)
        if not map_name:
            return []
        if map_name in self._map_cache:
            return [dict(row) for row in self._map_cache[map_name]]

        stem = _spawn_stem(map_name)
        candidates = []
        # Some rAthena mob files use the whole map prefix, others a family name.
        for name in dict.fromkeys([stem, map_name]):
            for directory in MOB_DIRS:
                candidates.append(
                    f"{RAW_ROOT}/npc/re/mobs/{directory}/{name}.txt"
                )

        rows: list[dict[str, Any]] = []
        for idx, url in enumerate(candidates):
            cache = self.cache_dir / "spawns" / f"{stem}_{idx}.txt"
            try:
                text = self._download_text(url, cache)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                continue
            except Exception:
                continue
            parsed = self._parse_spawn_file(text, map_name)
            if parsed:
                rows = parsed
                break

        self._map_cache[map_name] = rows
        return [dict(row) for row in rows]

    def observe_live(self):
        snapshot = authenticated_client_monitor.snapshot()
        live = snapshot.get("live_state") or {}
        world = live.get("world") or {}
        map_name = _clean_map(world.get("map"))
        if not map_name:
            return

        bucket = self._observed.setdefault(map_name, {})
        for actor in live.get("actors") or []:
            if actor.get("kind") != "monster":
                continue
            name = str(actor.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            row = bucket.setdefault(key, {
                "id": actor.get("type") or actor.get("class_id"),
                "name": name,
                "spawn_count": None,
                "level": None,
                "drops": [],
                "sources": ["soulbound_observed"],
                "observed_live": True,
            })
            row["observed_live"] = True

    def map_catalog(self, map_name: str) -> dict[str, Any]:
        map_name = _clean_map(map_name)
        self.observe_live()

        reference = self._reference_monsters(map_name)
        merged: dict[str, dict[str, Any]] = {
            str(row["name"]).lower(): dict(row) for row in reference
        }

        for key, observed in self._observed.get(map_name, {}).items():
            if key in merged:
                merged[key]["observed_live"] = True
                sources = set(merged[key].get("sources") or [])
                sources.add("soulbound_observed")
                merged[key]["sources"] = sorted(sources)
            else:
                merged[key] = dict(observed)

        monsters = sorted(
            merged.values(),
            key=lambda row: (
                not bool(row.get("observed_live")),
                -int(row.get("spawn_count") or 0),
                str(row.get("name") or ""),
            ),
        )
        return {
            "map": map_name,
            "monsters": monsters,
            "monster_count": len(monsters),
            "reference": "rAthena renewal spawn/mob database",
            "private_server_note": (
                "Reference spawns/drops may differ on SoulBound. Monsters actually "
                "seen by Classic.exe are merged in and marked observed."
            ),
        }


hunt_catalog = HuntCatalog(
    Path(__file__).resolve().parents[1] / "world_cache" / "hunt_catalog"
)
