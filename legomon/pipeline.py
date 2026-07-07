"""Pipeline orchestration: capture -> identify -> price -> assess -> score -> alert."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from .alerts import send_deal_alert
from .condition import ConditionAssessor
from .config import Config
from .db import Store
from .identify import Identifier
from .pricing import Pricer
from .scoring import Scorer
from .util import USER_AGENT

log = logging.getLogger("legomon.pipeline")


class Pipeline:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.identifier = Identifier(config, store)
        self.pricer = Pricer(config, store)
        self.assessor = ConditionAssessor(config, store)
        self.scorer = Scorer(config)

    # ---- stage 1: capture --------------------------------------------------
    def ingest(self, listings) -> list[int]:
        """Upsert listings; returns ids needing (re)processing."""
        to_process = []
        for l in listings:
            self._maybe_probe_facebook(l)
            lid, is_new, price_changed = self.store.upsert_listing(l)
            if is_new:
                log.info("new listing [%s/%s] %s — £%s",
                         l.source, l.source_id, l.title[:60], l.price)
                self._download_photos(lid, l.image_urls)
                to_process.append(lid)
            elif price_changed:
                log.info("price change on [%s/%s] %s", l.source, l.source_id,
                         l.title[:60])
                to_process.append(lid)
        return to_process

    def _maybe_probe_facebook(self, l):
        """Tier 3: enrich sparse email-intake Facebook listings with a
        logged-out page fetch, when enabled. Gives up at the login wall."""
        if (l.source != "facebook" or l.description
                or not self.config.get("sources.facebook.loggedout_probe.enabled",
                                       False)):
            return
        from .sources.facebook import probe_listing
        meta = probe_listing(self.config, l.url)
        if meta:
            l.title = meta["title"] or l.title
            l.description = meta["description"]
            if meta["image"] and meta["image"] not in l.image_urls:
                l.image_urls.append(meta["image"])

    def _download_photos(self, listing_id: int, urls: list[str]):
        if not self.config.get("storage.download_photos", True) or not urls:
            return
        photo_dir = self.config.path("storage.photo_dir", "data/photos") / str(listing_id)
        photo_dir.mkdir(parents=True, exist_ok=True)
        max_photos = int(self.config.get("storage.max_photos_per_listing", 6))
        paths = []
        for i, url in enumerate(urls[:max_photos]):
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
                resp.raise_for_status()
                suffix = ".png" if "png" in resp.headers.get("Content-Type", "") else ".jpg"
                p = photo_dir / f"{i}{suffix}"
                p.write_bytes(resp.content)
                paths.append(str(p))
            except requests.RequestException as e:
                log.debug("photo download failed %s: %s", url, e)
        if paths:
            self.store.set_photo_paths(listing_id, paths)

    # ---- stages 2-6 ---------------------------------------------------------
    def process_listing(self, listing_id: int, dry_run: bool = False) -> dict | None:
        """Run identification, pricing, condition, scoring, alerting for one
        listing. Returns the score dict or None."""
        row = self.store.get_listing(listing_id)
        if row is None:
            return None
        listing = dict(row)

        sets = self.identifier.identify(listing_id)
        cond = self.assessor.assess(listing_id)
        column = self.scorer.price_column(cond["condition"])

        market_value, detail = 0.0, []
        for s in sets:
            if s["set_number"] == "LOOSE_BULK":
                continue
            pricing = self.pricer.market_value(s["set_number"], column)
            if pricing["value"]:
                subtotal = pricing["value"] * s.get("quantity", 1)
                market_value += subtotal
                detail.append({**s, **pricing, "subtotal": round(subtotal, 2)})
        # loose bulk component (from vision kg estimate)
        for ident in self.store.identifications_for(listing_id):
            if ident["set_number"] == "LOOSE_BULK":
                bulk = self.pricer.loose_bulk_value(ident["quantity"])
                market_value += bulk
                detail.append({"set_number": "LOOSE_BULK",
                               "kg": ident["quantity"], "subtotal": bulk})

        if market_value <= 0:
            self.store.set_status(listing_id, "identified" if sets else "error")
            log.info("listing %d (%s): no market value — skipping score",
                     listing_id, listing["title"][:50])
            return None

        score = self.scorer.score(listing["price"], market_value,
                                  cond["condition"], cond["flags"])
        if score is None:
            self.store.set_status(listing_id, "priced")
            return None

        self.store.save_score(listing_id, score["market_value"],
                              score["condition_multiplier"],
                              score["estimated_net"], score["margin"],
                              score["margin_pct"], {"sets": detail})
        self.store.set_status(listing_id, "scored")
        log.info("scored %d: ask £%.2f, est net £%.2f, margin %.0f%% %s",
                 listing_id, listing["price"], score["estimated_net"],
                 score["margin_pct"] * 100, "DEAL!" if score["is_deal"] else "")

        if score["is_deal"]:
            self._alert(listing, score, cond, detail, dry_run)
        return score

    def _alert(self, listing: dict, score: dict, cond: dict, sets: list[dict],
               dry_run: bool):
        if dry_run:
            log.info("[dry-run] would alert: %s (£%s, margin %.0f%%)",
                     listing["title"][:60], listing["price"],
                     score["margin_pct"] * 100)
            return
        already = all(self.store.was_alerted(listing["id"], ch)
                      for ch in self.config.get("alerts.channels", ["ntfy"]))
        if already:
            return
        with_sets = []
        for s in sets:
            if s.get("set_number") and s["set_number"] != "LOOSE_BULK":
                meta = self.store.get_set(s["set_number"])
                with_sets.append({"set_number": s["set_number"],
                                  "name": meta["name"] if meta else ""})
        for channel in send_deal_alert(self.config, listing, score, cond, with_sets):
            self.store.mark_alerted(listing["id"], channel)

    # ---- run --------------------------------------------------------------
    def run_sources(self, sources, dry_run: bool = False) -> int:
        """One poll of the given sources through the full pipeline.
        Returns number of listings processed."""
        processed = 0
        for source in sources:
            try:
                listings = source.fetch_new_listings()
            except Exception:  # noqa: BLE001 - one source down must not kill the run
                log.exception("source %s failed", source.name)
                continue
            for lid in self.ingest(listings):
                try:
                    self.process_listing(lid, dry_run=dry_run)
                    processed += 1
                except Exception:  # noqa: BLE001
                    log.exception("processing listing %d failed", lid)
                    self.store.set_status(lid, "error")
        return processed
