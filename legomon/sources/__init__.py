"""Pluggable listing sources sharing one interface."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..config import Config
from ..db import Store
from ..models import Listing

log = logging.getLogger("legomon.sources")


class Source(ABC):
    name: str = "base"

    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    @abstractmethod
    def fetch_new_listings(self) -> list[Listing]:
        """Return listings seen this poll. Dedupe happens downstream in the
        store, so returning already-seen listings is fine."""

    def poll_minutes(self) -> int:
        return int(self.config.get(f"sources.{self.name}.poll_minutes", 60))

    def jitter_minutes(self) -> int:
        return int(self.config.get(f"sources.{self.name}.jitter_minutes", 0))


def load_sources(config: Config, store: Store) -> list[Source]:
    from .ebay import EbaySource
    from .email_intake import EmailIntakeSource
    from .facebook import FacebookLoggedInSource
    from .gumtree import GumtreeSource

    sources: list[Source] = []
    if config.get("sources.gumtree.enabled", True):
        sources.append(GumtreeSource(config, store))
    if config.get("sources.email_intake.enabled", True):
        if config.env("IMAP_PASSWORD"):
            sources.append(EmailIntakeSource(config, store))
        else:
            log.warning("email_intake enabled but IMAP_PASSWORD not set — skipping")
    if config.get("sources.facebook.loggedin.enabled", False):
        log.warning("Facebook logged-in polling is enabled. This breaches "
                    "Facebook's ToS and may get the account flagged.")
        sources.append(FacebookLoggedInSource(config, store))
    if config.get("sources.ebay.enabled", False) or config.env("EBAY_CLIENT_ID"):
        if config.env("EBAY_CLIENT_ID") and config.env("EBAY_CLIENT_SECRET"):
            sources.append(EbaySource(config, store))
        elif config.get("sources.ebay.enabled", False):
            log.warning("ebay enabled but EBAY_CLIENT_ID/SECRET not set — skipping")
    return sources
