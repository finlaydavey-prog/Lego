"""Stage 5: deal scoring.

estimated_net = market_value x condition_multiplier x (1 - selling_fees)
margin        = estimated_net - asking_price
margin_pct    = margin / asking_price

Alert rule: asking_price <= alert_price_ratio x estimated_net
            AND margin >= min_margin_gbp
"""
from __future__ import annotations

import logging

from .config import Config

log = logging.getLogger("legomon.scoring")

# condition -> which BrickLink price column to use
PRICE_COLUMN = {
    "sealed": "new",
    "boxed-complete": "used",
    "boxed-incomplete": "used",
    "built-complete": "used",
    "loose-bulk": "used",
    "unknown": "used",
}


class Scorer:
    def __init__(self, config: Config):
        self.config = config
        self.fees = float(config.get("deal.selling_fees", 0.13))
        self.ratio = float(config.get("deal.alert_price_ratio", 0.60))
        self.min_margin = float(config.get("deal.min_margin_gbp", 10))
        self.multipliers = config.get("condition.multipliers", {})
        self.flag_discounts = config.get("condition.flag_discounts", {})
        self.min_multiplier = float(config.get("condition.min_multiplier", 0.25))

    def price_column(self, condition: str) -> str:
        return PRICE_COLUMN.get(condition, "used")

    def condition_multiplier(self, condition: str, flags: list[str]) -> float:
        mult = float(self.multipliers.get(condition,
                                          self.multipliers.get("unknown", 0.6)))
        for flag in flags:
            mult -= float(self.flag_discounts.get(flag, 0))
        return max(self.min_multiplier, mult)

    def score(self, asking_price: float | None, market_value: float | None,
              condition: str, flags: list[str]) -> dict | None:
        """Returns score dict, or None when unpriceable."""
        if not asking_price or asking_price <= 0 or not market_value:
            return None
        mult = self.condition_multiplier(condition, flags)
        estimated_net = market_value * mult * (1 - self.fees)
        margin = estimated_net - asking_price
        margin_pct = margin / asking_price
        is_deal = (asking_price <= self.ratio * estimated_net
                   and margin >= self.min_margin)
        return {
            "market_value": round(market_value, 2),
            "condition_multiplier": round(mult, 3),
            "estimated_net": round(estimated_net, 2),
            "margin": round(margin, 2),
            "margin_pct": round(margin_pct, 3),
            "is_deal": is_deal,
        }
