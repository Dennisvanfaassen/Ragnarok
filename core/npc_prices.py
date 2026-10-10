from __future__ import annotations

import json
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "user_data" / "rms_npc_sell_prices.json"

# Seed a few confirmed Pre-Renewal prices so the current hunt works instantly.
# Unknown item IDs are resolved in the background and cached locally.
SEEDED_PRE_RE_SELL_PRICES = {
    517: 25,   # Meat
    950: 132,  # Heart of Mermaid
    962: 35,   # Tentacle
}


class NpcSellPriceResolver:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._prices: dict[int, int | None] = dict(SEEDED_PRE_RE_SELL_PRICES)
        self._resolving: set[int] = set()
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            if not CACHE_PATH.exists():
                return
            raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            for key, value in dict(raw or {}).items():
                try:
                    item_id = int(key)
                except Exception:
                    continue
                if value is None:
                    self._prices[item_id] = None
                else:
                    self._prices[item_id] = max(0, int(value))
        except Exception:
            pass

    def _save_cache(self) -> None:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(
                json.dumps(
                    {str(k): v for k, v in sorted(self._prices.items())},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def _fetch_pre_re_sell_price(item_id: int) -> int | None:
        url = (
            "https://ratemyserver.net/index.php?"
            f"item_id={int(item_id)}&page=item_db"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 RagnarokBot/1.0 "
                    "(NPC sell price cache; RateMyServer item lookup)"
                )
            },
        )
        with urllib.request.urlopen(request, timeout=8.0) as response:
            html = response.read().decode("utf-8", errors="replace")

        # RMS renders values as: Buy 264z Sell 132z
        match = re.search(
            r"\bSell\s*(?:</?[^>]+>\s*)*([0-9][0-9,]*)z\b",
            html,
            flags=re.IGNORECASE,
        )
        if not match:
            # Some pages have no NPC sell value at all.
            if re.search(r"\bSell\s*(?:</?[^>]+>\s*)*n/?a\b", html, re.I):
                return None
            return None
        return int(match.group(1).replace(",", ""))

    def _resolve_worker(self, item_id: int) -> None:
        try:
            price = self._fetch_pre_re_sell_price(item_id)
            with self._lock:
                self._prices[item_id] = price
                self._save_cache()
        except Exception:
            # Leave it unresolved so a later session can retry.
            pass
        finally:
            with self._lock:
                self._resolving.discard(item_id)

    def ensure(self, item_id: int) -> None:
        item_id = int(item_id or 0)
        if item_id <= 0:
            return
        with self._lock:
            if item_id in self._prices or item_id in self._resolving:
                return
            self._resolving.add(item_id)
        threading.Thread(
            target=self._resolve_worker,
            args=(item_id,),
            daemon=True,
            name=f"rms-price-{item_id}",
        ).start()

    def get(self, item_id: int) -> int | None:
        item_id = int(item_id or 0)
        if item_id <= 0:
            return None
        with self._lock:
            value = self._prices.get(item_id)
        if item_id not in self._prices:
            self.ensure(item_id)
        return value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "source": "RateMyServer Pre-Renewal item database",
                "cached": {
                    str(item_id): price
                    for item_id, price in sorted(self._prices.items())
                },
                "resolving": sorted(self._resolving),
            }


npc_sell_prices = NpcSellPriceResolver()
