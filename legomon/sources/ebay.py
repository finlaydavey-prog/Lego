"""Optional Source C: eBay Browse API (official, free) — local pickup LEGO.

Uses the client-credentials OAuth flow; needs EBAY_CLIENT_ID/EBAY_CLIENT_SECRET
in .env. No scraping involved.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timezone

import requests

from ..models import Listing
from ..util import sanitize_text, with_retries
from . import Source

log = logging.getLogger("legomon.ebay")

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE = "EBAY_GB"


class EbaySource(Source):
    name = "ebay"

    def __init__(self, config, store):
        super().__init__(config, store)
        self._token: str | None = None
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        cid = self.config.env("EBAY_CLIENT_ID")
        secret = self.config.env("EBAY_CLIENT_SECRET")
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        resp = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def fetch_new_listings(self) -> list[Listing]:
        postcode = self.config.get("location.postcode", "").replace(" ", "")
        radius = int(self.config.get("sources.ebay.radius_miles", 5))
        token = with_retries(self._get_token, attempts=3, label="ebay token")
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
        }
        listings: dict[str, Listing] = {}
        for term in self.config.get("search_terms", ["lego"]):
            params = {
                "q": term,
                "filter": (f"buyingOptions:{{FIXED_PRICE|BEST_OFFER}},"
                           f"pickupCountry:GB,pickupPostalCode:{postcode},"
                           f"pickupRadius:{radius},pickupRadiusUnit:mi"),
                "sort": "newlyListed",
                "limit": "50",
            }
            try:
                resp = requests.get(SEARCH_URL, headers=headers, params=params,
                                    timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                log.error("ebay search failed for %r: %s", term, e)
                continue
            for item in resp.json().get("itemSummaries", []):
                l = self._to_listing(item)
                if l:
                    listings[l.source_id] = l
            time.sleep(1)
        log.info("ebay poll: %d listings", len(listings))
        return list(listings.values())

    def _to_listing(self, item: dict) -> Listing | None:
        item_id = item.get("legacyItemId") or item.get("itemId", "")
        if not item_id:
            return None
        price = None
        if "price" in item:
            try:
                price = float(item["price"].get("value"))
            except (TypeError, ValueError):
                price = None
        posted = None
        if item.get("itemCreationDate"):
            try:
                posted = datetime.fromisoformat(
                    item["itemCreationDate"].replace("Z", "+00:00"))
            except ValueError:
                posted = None
        images = [item["image"]["imageUrl"]] if item.get("image") else []
        images += [t["imageUrl"] for t in item.get("thumbnailImages", [])
                   if t.get("imageUrl")]
        loc = item.get("itemLocation", {})
        return Listing(
            source=self.name,
            source_id=str(item_id),
            url=item.get("itemWebUrl", ""),
            title=sanitize_text(item.get("title", ""), 300),
            price=price,
            currency=item.get("price", {}).get("currency", "GBP"),
            location=sanitize_text(
                ", ".join(x for x in (loc.get("city"), loc.get("postalCode")) if x), 200),
            image_urls=images[:6],
            posted_at=posted,
            attributes={"condition": item.get("condition", "")},
        )
