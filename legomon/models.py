"""Shared data model: every source produces Listing objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Listing:
    source: str                      # gumtree | facebook | ebay | email:<rule>
    source_id: str                   # stable per-source listing id
    url: str
    title: str
    price: float | None = None       # GBP; None if not stated in the capture
    currency: str = "GBP"
    description: str = ""
    location: str = ""
    image_urls: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    attributes: dict = field(default_factory=dict)  # source-specific extras

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_id)
