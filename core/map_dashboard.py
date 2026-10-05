from __future__ import annotations

import base64
from typing import Any

from core.pathing import nav_repository, normalize_map_name
from diagnostics.authenticated_client import authenticated_client_monitor


_grid_cache: dict[str, dict[str, Any]] = {}


def current_map_name() -> str | None:
    snapshot = authenticated_client_monitor.snapshot()
    world = (snapshot.get("live_state") or {}).get("world") or {}
    name = normalize_map_name(world.get("map"))
    return name or None


def map_grid_payload(map_name: str | None = None) -> dict[str, Any]:
    name = normalize_map_name(map_name or current_map_name())
    if not name:
        return {
            "status": "waiting",
            "message": "Current map is not known yet.",
            "map": None,
        }

    cached = _grid_cache.get(name)
    if cached is not None:
        return cached

    try:
        grid, source = nav_repository.load(name)
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "map": name,
        }

    payload = {
        "status": "ready",
        "map": name,
        "width": grid.width,
        "height": grid.height,
        # One byte per RO cell. Browser decodes this once and keeps it locally.
        "walkability_b64": base64.b64encode(grid.cells).decode("ascii"),
        "source": source,
    }
    _grid_cache[name] = payload
    return payload


def map_live_overlay() -> dict[str, Any]:
    snapshot = authenticated_client_monitor.snapshot()
    live = snapshot.get("live_state") or {}
    world = live.get("world") or {}
    actors = live.get("actors") or []

    map_name = normalize_map_name(world.get("map"))
    player = None
    if world.get("x") is not None and world.get("y") is not None:
        player = {
            "x": int(world["x"]),
            "y": int(world["y"]),
        }

    monsters = []
    players = []
    other = []

    for actor in actors:
        x, y = actor.get("x"), actor.get("y")
        if x is None or y is None:
            continue

        entry = {
            "id": actor.get("id"),
            "name": actor.get("name") or "",
            "x": int(x),
            "y": int(y),
            "aggressive_to_me": bool(actor.get("aggressive_to_me")),
        }

        kind = actor.get("kind")
        if kind == "monster":
            monsters.append(entry)
        elif kind == "player":
            players.append(entry)
        else:
            other.append(entry)

    return {
        "status": "ready" if map_name else "waiting",
        "map": map_name or None,
        "player": player,
        "monsters": monsters,
        "players": players,
        "other": other,
        "counts": {
            "monsters": len(monsters),
            "players": len(players),
            "other": len(other),
        },
        "note": (
            "Actor markers are limited to actors currently known by the client "
            "(normally the loaded/visible area around your character)."
        ),
    }
