"""Gumtree source — the always-on automated source (results pages are public).

Primary parse strategy: Gumtree embeds the full result set as URL-encoded JSON
in `window.clientData` (verified live 2026-07). Fallback: data-q CSS selectors.
Both are configured in selectors.yaml, not here.

Politeness: one page per search term per poll, a configurable delay between
requests, a real User-Agent, and an optional robots.txt gate. Note that
gumtree.com's robots.txt disallows /search for crawlers; enforce_robots in
config.yaml decides whether that skips the fetch or just logs a notice.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, unquote

from ..models import Listing
from ..util import RobotsChecker, parse_price, polite_get, sanitize_text
from . import Source

log = logging.getLogger("legomon.gumtree")

BASE = "https://www.gumtree.com"


class BlockedError(RuntimeError):
    """The response was a bot-block page, not results."""


class GumtreeSource(Source):
    name = "gumtree"

    def __init__(self, config, store):
        super().__init__(config, store)
        self.sel = config.selectors["gumtree"]
        self.robots = RobotsChecker()
        self._robots_warned = False

    # ---- URL construction -------------------------------------------------
    def search_urls(self) -> list[str]:
        loc = quote_plus(self.config.get("location.postcode"))
        dist = int(self.config.get("location.radius_miles", 2))
        sort = self.config.get("sources.gumtree.sort", "date")
        urls = []
        for term in self.config.get("search_terms", ["lego"]):
            q = quote_plus(term)
            urls.append(f"{BASE}/search?search_category=all&q={q}"
                        f"&search_location={loc}&distance={dist}&sort={sort}")
        category = self.config.get("sources.gumtree.category_sweep", "")
        if category:
            urls.append(f"{BASE}/search?search_category={quote_plus(category)}"
                        f"&q=lego&search_location={loc}&distance={dist}&sort={sort}")
        return urls

    # ---- fetching ----------------------------------------------------------
    def _fetch_html(self, url: str) -> str:
        if not self.robots.allowed(url):
            if self.config.get("sources.gumtree.enforce_robots", False):
                log.info("robots.txt disallows %s — skipping (enforce_robots on)", url)
                return ""
            if not self._robots_warned:
                log.warning("gumtree robots.txt disallows this path; fetching "
                            "anyway per config (enforce_robots: false)")
                self._robots_warned = True

        fetcher = self.config.get("sources.gumtree.fetcher", "requests")
        if fetcher == "playwright":
            return self._fetch_playwright(url)
        try:
            return self._fetch_requests(url)
        except BlockedError:
            if self.config.get("sources.gumtree.playwright_fallback", True):
                log.info("requests fetch blocked, retrying with playwright")
                return self._fetch_playwright(url)
            raise

    def _fetch_requests(self, url: str) -> str:
        resp = polite_get(url)
        if resp.status_code in self.sel.get("blocked_status_codes", []):
            raise BlockedError(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        html = resp.text
        if any(marker in html[:2000] for marker in self.sel.get("blocked_markers", [])):
            raise BlockedError("block-page markers in response")
        return html

    def _fetch_playwright(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("playwright not installed (pip install playwright); "
                      "cannot fall back for %s", url)
            return ""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=None)
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
                return page.content()
            finally:
                browser.close()

    # ---- parsing -------------------------------------------------------------
    def parse_client_data(self, html: str) -> list[Listing]:
        m = re.search(self.sel["client_data"]["script_regex"], html)
        if not m:
            return []
        try:
            data = json.loads(unquote(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            return []
        ads: list[tuple[dict, bool]] = []
        for i, dotted in enumerate(self.sel["client_data"]["listings_paths"]):
            node = data
            for part in dotted.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, list):
                ads.extend((ad, i > 0) for ad in node)  # i>0 => nearby result
        f = self.sel["client_data"]["fields"]
        listings = []
        for ad, nearby in ads:
            if not isinstance(ad, dict) or f["id"] not in ad:
                continue
            posted = None
            epoch = ad.get(f["posted_epoch_ms"])
            if isinstance(epoch, (int, float)):
                posted = datetime.fromtimestamp(epoch / 1000, tz=timezone.utc)
            images = [ad.get(f["image"])] if ad.get(f["image"]) else []
            images += ad.get(f["extra_images"]) or []
            attrs = {a.get("name"): a.get("value")
                     for a in (ad.get(f["attributes"]) or []) if isinstance(a, dict)}
            if nearby:
                attrs["nearby"] = "true"  # just outside the configured radius
            listings.append(Listing(
                source=self.name,
                source_id=str(ad[f["id"]]),
                url=BASE + ad.get(f["path"], ""),
                title=sanitize_text(ad.get(f["title"], ""), 300),
                price=parse_price(ad.get(f["price"])),
                description=sanitize_text(ad.get(f["description"], ""), 2000),
                location=sanitize_text(ad.get(f["location"], ""), 200),
                image_urls=[u for u in images if u],
                posted_at=posted,
                attributes=attrs,
            ))
        return listings

    def parse_css(self, html: str) -> list[Listing]:
        """Fallback parser if clientData disappears from the page."""
        from bs4 import BeautifulSoup
        css = self.sel["css"]
        soup = BeautifulSoup(html, "html.parser")
        listings = []
        for card in soup.select(css["result"]):
            anchor = card.select_one(css["anchor"])
            if not anchor or not anchor.get("href"):
                continue
            href = anchor["href"]
            m = re.search(r"/(\d+)/?$", href)
            if not m:
                continue
            title_el = card.select_one(css["title"])
            price_el = card.select_one(css["price"])
            loc_el = card.select_one(css["location"])
            desc_el = card.select_one(css["description"])
            img_el = card.select_one(css["image"])
            listings.append(Listing(
                source=self.name,
                source_id=m.group(1),
                url=BASE + href,
                title=sanitize_text(title_el.get_text() if title_el else "", 300),
                price=parse_price(price_el.get_text() if price_el else None),
                description=sanitize_text(desc_el.get_text() if desc_el else "", 2000),
                location=sanitize_text(loc_el.get_text() if loc_el else "", 200),
                image_urls=[img_el["src"]] if img_el and img_el.get("src") else [],
            ))
        return listings

    def parse(self, html: str) -> list[Listing]:
        listings = self.parse_client_data(html)
        if listings:
            return listings
        listings = self.parse_css(html)
        if listings:
            log.warning("gumtree clientData parse failed; CSS fallback found %d "
                        "listings — markup may have changed", len(listings))
        return listings

    # ---- Source interface ------------------------------------------------------
    def fetch_new_listings(self) -> list[Listing]:
        delay = int(self.config.get("sources.gumtree.request_delay_seconds", 20))
        seen: dict[str, Listing] = {}
        urls = self.search_urls()
        for i, url in enumerate(urls):
            try:
                html = self._fetch_html(url)
            except Exception as e:  # noqa: BLE001 - a dead term shouldn't kill the poll
                log.error("gumtree fetch failed for %s: %s", url, e)
                continue
            if html:
                found = self.parse(html)
                if not found:
                    log.warning("gumtree page parsed to 0 listings (%s) — "
                                "check selectors.yaml against live markup", url)
                for l in found:
                    seen[l.source_id] = l
            if i < len(urls) - 1:
                time.sleep(delay)
        log.info("gumtree poll: %d unique listings across %d pages", len(seen), len(urls))
        return list(seen.values())
