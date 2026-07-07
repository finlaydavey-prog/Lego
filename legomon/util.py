"""Shared helpers: URL unwrapping, sanitisation, retries, polite HTTP."""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import requests

log = logging.getLogger("legomon")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_FB_ITEM_RE = re.compile(r"facebook\.com/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"£\s*([\d,]+(?:\.\d{1,2})?)")


def unwrap_facebook_url(url: str) -> str:
    """Unwrap l.facebook.com / lm.facebook.com tracking redirects back to the
    real target URL, and canonicalise Marketplace item URLs."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in ("l.facebook.com", "lm.facebook.com") and parsed.path.startswith("/l.php"):
        target = parse_qs(parsed.query).get("u", [None])[0]
        if target:
            url = unquote(target)
    m = _FB_ITEM_RE.search(url)
    if m:
        return f"https://www.facebook.com/marketplace/item/{m.group(1)}/"
    # strip tracking query params from facebook URLs generally
    p = urlparse(url)
    if p.netloc.lower().endswith("facebook.com") and p.query:
        return f"{p.scheme}://{p.netloc}{p.path}"
    return url


def facebook_item_id(url: str) -> str | None:
    m = _FB_ITEM_RE.search(url)
    return m.group(1) if m else None


def domain_allowed(url: str, allowed: list[str]) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in allowed)


def parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = _PRICE_RE.search(text)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"\b([\d,]+(?:\.\d{1,2})?)\b", text)
    return float(m.group(1).replace(",", "")) if m else None


def sanitize_text(text: str | None, max_len: int = 5000) -> str:
    """Email/scrape content is untrusted: strip control characters and cap
    length before it touches the DB or a prompt."""
    if not text:
        return ""
    cleaned = "".join(
        ch for ch in text
        if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
    )
    return cleaned.strip()[:max_len]


def parse_relative_date(text: str | None) -> datetime | None:
    """'57 days ago', '3 hours ago', 'Just now' -> datetime (UTC)."""
    if not text:
        return None
    text = text.strip().lower()
    now = datetime.now(timezone.utc)
    if "just now" in text or "less than" in text:
        return now
    m = re.match(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n),
             "day": timedelta(days=n), "week": timedelta(weeks=n),
             "month": timedelta(days=30 * n)}[unit]
    return now - delta


def with_retries(fn, attempts: int = 3, base_delay: float = 2.0, label: str = ""):
    """Call fn() with exponential backoff. Raises the last error."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - deliberate catch-all for retry
            if i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            log.warning("%s failed (%s), retrying in %.0fs", label or "call", e, delay)
            time.sleep(delay)


def polite_get(url: str, *, timeout: int = 30, headers: dict | None = None,
               session: requests.Session | None = None) -> requests.Response:
    h = {"User-Agent": USER_AGENT,
         "Accept": "text/html,application/xhtml+xml",
         "Accept-Language": "en-GB,en;q=0.9"}
    if headers:
        h.update(headers)
    getter = session.get if session else requests.get
    return getter(url, headers=h, timeout=timeout)


class RobotsChecker:
    """Minimal robots.txt check using urllib's parser, cached per host."""

    def __init__(self):
        self._parsers: dict[str, object] = {}

    def allowed(self, url: str) -> bool:
        from urllib.robotparser import RobotFileParser
        host = urlparse(url).netloc
        if host not in self._parsers:
            rp = RobotFileParser(f"https://{host}/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None
            self._parsers[host] = rp
        rp = self._parsers[host]
        if rp is None:
            return True
        return rp.can_fetch(USER_AGENT, url)
