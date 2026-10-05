from __future__ import annotations

from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ITEMS_PATH = ROOT / "data" / "openkore_items.txt"


@lru_cache(maxsize=1)
def item_names() -> dict[int, str]:
    names: dict[int, str] = {}
    if not ITEMS_PATH.exists():
        return names

    for raw in ITEMS_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or "#" not in line:
            continue
        parts = line.split("#")
        if len(parts) < 2:
            continue
        try:
            item_id = int(parts[0].strip())
        except ValueError:
            continue
        name = parts[1].strip().replace("_", " ")
        if name:
            names[item_id] = name
    return names


def item_name(name_id: int) -> str:
    item_id = int(name_id)
    return item_names().get(item_id, f"Item #{item_id}")


def item_id_for_name(name: str) -> int | None:
    needle = str(name or "").strip().casefold()
    if not needle:
        return None
    for item_id, item_name_value in item_names().items():
        if item_name_value.strip().casefold() == needle:
            return int(item_id)
    return None
