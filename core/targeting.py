from __future__ import annotations

import math
import time
from typing import Any


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def build_targeting_state(
    live_snapshot: dict[str, Any],
    wanted_monsters: list[str],
    *,
    stale_after_seconds: float = 12.0,
) -> dict[str, Any]:
    live = live_snapshot.get("live_state") or {}
    world = live.get("world") or {}
    actors = live.get("actors") or []

    player_x = world.get("x")
    player_y = world.get("y")
    now = time.time()

    wanted = {_norm(name) for name in wanted_monsters if _norm(name)}
    candidates: list[dict[str, Any]] = []

    for actor in actors:
        if actor.get("kind") != "monster":
            continue

        name = str(actor.get("name") or "").strip()
        actor_x = actor.get("x")
        actor_y = actor.get("y")
        last_seen = float(actor.get("last_seen") or 0)

        if now - last_seen > stale_after_seconds:
            continue
        # Hunting is whitelist-based. An empty saved monster list means
        # "attack nothing", never "attack everything".
        if not wanted or _norm(name) not in wanted:
            continue
        if actor_x is None or actor_y is None:
            continue

        distance = None
        tile_distance = None
        if player_x is not None and player_y is not None:
            dx = int(actor_x) - int(player_x)
            dy = int(actor_y) - int(player_y)
            distance = round(math.hypot(dx, dy), 2)
            tile_distance = max(abs(dx), abs(dy))

        candidates.append({
            "id": actor.get("id"),
            "name": name or f"Monster #{actor.get('id')}",
            "x": actor_x,
            "y": actor_y,
            "distance": distance,
            "tile_distance": tile_distance,
            "last_seen_age": round(max(0.0, now - last_seen), 2),
            "packet": actor.get("packet"),
        })

    candidates.sort(
        key=lambda item: (
            item["tile_distance"] is None,
            item["tile_distance"] if item["tile_distance"] is not None else 999999,
            item["distance"] if item["distance"] is not None else 999999,
            item["id"] or 0,
        )
    )

    selected = candidates[0] if candidates else None

    return {
        "wanted_monsters": wanted_monsters,
        "target_all_monsters": False,
        "targeting_enabled": bool(wanted),
        "candidate_count": len(candidates),
        "selected": selected,
        "candidates": candidates[:50],
    }
