"""Stage 3: market pricing.

Primary: BrickLink Price Guide API (OAuth1) — 6-month sold averages, split
New vs Used. Secondary: BrickEconomy scrape (fragile, off by default).
Fallback: Rebrickable RRP x configurable factor. All lookups cached 7 days.

Standalone test: python monitor.py price 75192
"""
from __future__ import annotations

import logging
import re

import requests

from .config import Config
from .db import Store
from .util import USER_AGENT, with_retries

log = logging.getLogger("legomon.pricing")

BRICKLINK_PRICE_URL = ("https://api.bricklink.com/api/store/v1/items/SET/"
                       "{set_number}/price")
# Rough per-kg value for loose mixed LEGO on the UK secondary market.
LOOSE_GBP_PER_KG = 7.0


class Pricer:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.cache_days = int(config.get("pricing.cache_days", 7))
        self._bl_session = None

    # ---- BrickLink -------------------------------------------------------
    def _bricklink_auth(self):
        if self._bl_session is not None:
            return self._bl_session
        ck = self.config.env("BRICKLINK_CONSUMER_KEY")
        cs = self.config.env("BRICKLINK_CONSUMER_SECRET")
        tk = self.config.env("BRICKLINK_TOKEN")
        ts = self.config.env("BRICKLINK_TOKEN_SECRET")
        if not all([ck, cs, tk, ts]):
            return None
        from requests_oauthlib import OAuth1
        self._bl_session = OAuth1(ck, client_secret=cs,
                                  resource_owner_key=tk,
                                  resource_owner_secret=ts)
        return self._bl_session

    def bricklink_sold_avg(self, set_number: str, new_or_used: str) -> tuple[float, int] | None:
        """6-month sold average from the BrickLink price guide.
        new_or_used: 'N' or 'U'. Returns (avg_price_gbp, n_sales) or None."""
        auth = self._bricklink_auth()
        if auth is None:
            return None

        def _get():
            return requests.get(
                BRICKLINK_PRICE_URL.format(set_number=set_number),
                params={"guide_type": "sold",       # last-6-months sales
                        "new_or_used": new_or_used,
                        "currency_code": "GBP"},
                auth=auth, headers={"User-Agent": USER_AGENT}, timeout=30)

        resp = with_retries(_get, attempts=3, label=f"bricklink {set_number}")
        if resp.status_code != 200:
            log.warning("bricklink HTTP %s for %s", resp.status_code, set_number)
            return None
        body = resp.json()
        if body.get("meta", {}).get("code") != 200:
            log.warning("bricklink API error for %s: %s", set_number, body.get("meta"))
            return None
        data = body.get("data", {})
        avg = data.get("avg_price")
        n = int(data.get("total_quantity") or data.get("unit_quantity") or 0)
        return (float(avg), n) if avg else None

    # ---- BrickEconomy (optional, fragile) --------------------------------
    def brickeconomy_value(self, set_number: str) -> float | None:
        if not self.config.get("pricing.brickeconomy.enabled", False):
            return None
        num = set_number.split("-")[0]
        try:
            resp = requests.get(f"https://www.brickeconomy.com/set/{num}-1/",
                                headers={"User-Agent": USER_AGENT}, timeout=30)
            if resp.status_code != 200:
                return None
            m = re.search(r"Value[^£]{0,200}£\s*([\d,]+(?:\.\d{2})?)", resp.text)
            return float(m.group(1).replace(",", "")) if m else None
        except requests.RequestException:
            return None

    # ---- public API ---------------------------------------------------------
    def market_value(self, set_number: str, condition: str) -> dict:
        """condition: 'new' | 'used'. Returns
        {value, n_sales, source, sanity_check} — value may be None."""
        canonical = set_number if "-" in set_number else f"{set_number}-1"
        cached = self.store.cached_price(canonical, condition, "bricklink",
                                         self.cache_days)
        if cached and cached["avg_price"]:
            return {"value": cached["avg_price"], "n_sales": cached["n_sales"],
                    "source": "bricklink(cached)", "sanity_check": None}

        result = self.bricklink_sold_avg(canonical, "N" if condition == "new" else "U")
        if result:
            value, n = result
            self.store.cache_price(canonical, condition, "bricklink", value, n)
            return {"value": value, "n_sales": n, "source": "bricklink",
                    "sanity_check": self.brickeconomy_value(canonical)}

        # fallback: Rebrickable metadata + RRP heuristic (or piece count)
        est = self._rrp_estimate(canonical, condition)
        if est is not None:
            self.store.cache_price(canonical, condition, "rrp_estimate", est, 0)
            return {"value": est, "n_sales": 0, "source": "rrp_estimate",
                    "sanity_check": self.brickeconomy_value(canonical)}
        return {"value": None, "n_sales": 0, "source": "none", "sanity_check": None}

    def _rrp_estimate(self, canonical: str, condition: str) -> float | None:
        row = self.store.get_set(canonical)
        factor = float(self.config.get(
            f"pricing.rrp_fallback.{'new' if condition == 'new' else 'used'}_factor",
            0.6))
        if row and row["rrp_gbp"]:
            return round(row["rrp_gbp"] * factor, 2)
        if row and row["pieces"]:
            # ~8p/piece is a long-running UK rule of thumb for set value
            return round(row["pieces"] * 0.08 * factor / 0.6, 2)
        return None

    def loose_bulk_value(self, kg: float) -> float:
        return round(kg * LOOSE_GBP_PER_KG, 2)
