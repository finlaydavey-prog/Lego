"""Facebook Marketplace — best-effort tiers. Tier 1 (native FB alerts read via
email) lives in email_intake.py and is the recommended path.

Tier 3 (probe_listing): logged-out fetch of an individual listing page to
enrich email-intake listings. Some pages render for logged-out visitors; when
a login redirect appears we give up on that URL — we never attempt to bypass
or automate the login or anti-bot checks.

Tier 2 (FacebookLoggedInSource): Playwright with a persistent profile the user
logged into manually once via `python monitor.py fb-login`. Disabled by
default. WARNING: automated access breaches Facebook's ToS; a flagged or
locked account is possible. Poll gently.
"""
from __future__ import annotations

import logging
import re

from ..models import Listing
from ..util import parse_price, polite_get, sanitize_text
from . import Source

log = logging.getLogger("legomon.facebook")


def probe_listing(config, url: str) -> dict | None:
    """Tier 3: try to read og: metadata from a listing page without login.
    Returns {title, description, image} or None if the login wall appears."""
    sel = config.selectors["facebook"]
    try:
        resp = polite_get(url, timeout=20)
    except Exception as e:  # noqa: BLE001
        log.debug("logged-out probe failed for %s: %s", url, e)
        return None
    final_url = resp.url or ""
    body_head = resp.text[:5000]
    if any(m in final_url or m in body_head for m in sel["login_markers"]):
        log.debug("login wall for %s — giving up (tier 3 never bypasses it)", url)
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")

    def meta(css):
        el = soup.select_one(css)
        return el.get("content", "") if el else ""

    title = sanitize_text(meta(sel["item_title"]), 300)
    if not title:
        return None
    return {
        "title": title,
        "description": sanitize_text(meta(sel["item_description"]), 2000),
        "image": meta(sel["item_image"]),
    }


class FacebookLoggedInSource(Source):
    """Tier 2. Requires `pip install playwright`, `playwright install chromium`,
    then a one-off manual login: `python monitor.py fb-login`."""

    name = "facebook"

    SEARCH_URL = ("https://www.facebook.com/marketplace/search"
                  "?query={query}&exact=false")
    _ITEM_RE = re.compile(r"/marketplace/item/(\d+)")

    def profile_dir(self):
        return self.config.path("sources.facebook.loggedin.profile_dir", "fb_profile")

    def poll_minutes(self) -> int:
        return int(self.config.get("sources.facebook.loggedin.poll_minutes", 180))

    def jitter_minutes(self) -> int:
        return int(self.config.get("sources.facebook.loggedin.jitter_minutes", 45))

    def fetch_new_listings(self) -> list[Listing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("facebook tier 2 needs playwright: pip install playwright "
                      "&& playwright install chromium")
            return []
        profile = self.profile_dir()
        if not profile.exists():
            log.error("no Facebook profile at %s — run `python monitor.py fb-login` "
                      "once to log in manually", profile)
            return []
        listings: dict[str, Listing] = {}
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(str(profile), headless=True)
            try:
                page = ctx.new_page()
                for term in self.config.get("search_terms", ["lego"]):
                    url = self.SEARCH_URL.format(query=term.replace(" ", "%20"))
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(4000)
                    if "login" in page.url:
                        log.error("facebook session expired — re-run fb-login")
                        break
                    for l in self._parse_results(page):
                        listings[l.source_id] = l
                    page.wait_for_timeout(3000)
            finally:
                ctx.close()
        log.info("facebook tier-2 poll: %d listings", len(listings))
        return list(listings.values())

    def _parse_results(self, page) -> list[Listing]:
        out = []
        for a in page.query_selector_all('a[href*="/marketplace/item/"]'):
            href = a.get_attribute("href") or ""
            m = self._ITEM_RE.search(href)
            if not m:
                continue
            text = sanitize_text(a.inner_text(), 400)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            price = next((parse_price(l) for l in lines if "£" in l), None)
            title = next((l for l in lines if "£" not in l), "")
            img = a.query_selector("img")
            out.append(Listing(
                source=self.name,
                source_id=m.group(1),
                url=f"https://www.facebook.com/marketplace/item/{m.group(1)}/",
                title=title or f"facebook listing {m.group(1)}",
                price=price,
                location=next((l for l in lines[2:] if "£" not in l and l != title), ""),
                image_urls=[img.get_attribute("src")] if img and img.get_attribute("src") else [],
            ))
        return out


def interactive_login(config):
    """Open a visible browser on facebook.com so the user can log in once;
    the session persists in the profile dir for tier 2 polling."""
    from playwright.sync_api import sync_playwright
    profile = config.path("sources.facebook.loggedin.profile_dir", "fb_profile")
    profile.mkdir(parents=True, exist_ok=True)
    print(f"Opening browser (profile: {profile}). Log in to Facebook, solve any "
          "checkpoint, then close the window.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=False)
        page = ctx.new_page()
        page.goto("https://www.facebook.com")
        page.wait_for_event("close", timeout=0)
        ctx.close()
    print("Profile saved. Enable sources.facebook.loggedin in config.yaml to use it.")
