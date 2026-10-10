from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PRICE_PARTS = [
    ROOT / "data" / "rathena_npc_prices.b64.0",
    ROOT / "data" / "rathena_npc_prices.b64.1",
    ROOT / "data" / "rathena_npc_prices.b64.2",
    ROOT / "data" / "rathena_npc_prices.b64.3",
]


class NpcSellPriceResolver:
    """Offline NPC sell-price lookup generated from the supplied rAthena DB.

    rAthena semantics:
    - explicit Sell wins when present;
    - otherwise Sell = floor(Buy / 2);
    - Trade.NoSell items are stored as 0.
    """

    def __init__(self) -> None:
        self._prices: dict[int, int] = self._load_prices()

    @staticmethod
    def _load_prices() -> dict[int, int]:
        try:
            encoded = "".join(
                path.read_text(encoding="ascii").strip()
                for path in PRICE_PARTS
            )
            raw = zlib.decompress(base64.b64decode(encoded))
            data = json.loads(raw.decode("utf-8"))
            return {
                int(item_id): max(0, int(price))
                for item_id, price in dict(data or {}).items()
            }
        except Exception:
            # Keep the currently used hunt items available even if the packed
            # table is ever missing or damaged.
            return {
                517: 25,   # Meat: Buy 50 -> Sell 25
                950: 132,  # Heart of Mermaid: Buy 264 -> Sell 132
                962: 35,   # Tentacle: Buy 70 -> Sell 35
            }

    def ensure(self, item_id: int) -> None:
        # Compatibility with the former online resolver. Everything is local
        # now, so no background lookup is required.
        return None

    def get(self, item_id: int) -> int | None:
        item_id = int(item_id or 0)
        if item_id <= 0:
            return None
        return self._prices.get(item_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": "supplied rAthena item_db equip/etc/usable",
            "price_count": len(self._prices),
            "rule": "explicit Sell, otherwise floor(Buy / 2); NoSell = 0",
        }


npc_sell_prices = NpcSellPriceResolver()
