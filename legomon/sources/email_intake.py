"""Source D: IMAP inbox watcher — the Facebook hand-off.

Facebook's own "new listings like these" notification emails (plus Gumtree /
eBay saved-search emails) are the feed. No scraping, no Facebook login:
Facebook is voluntarily sending the mail.

Which emails count, and which link domains may be followed, is all in
config.yaml under sources.email_intake — new alert formats are a config edit.
Email content is untrusted input: everything is sanitised, only allowlisted
domains survive, and tracking redirects are unwrapped before storage.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re

from bs4 import BeautifulSoup

from ..models import Listing
from ..util import (domain_allowed, facebook_item_id, parse_price,
                    sanitize_text, unwrap_facebook_url, with_retries)
from . import Source

log = logging.getLogger("legomon.email")

_GUMTREE_ID_RE = re.compile(r"gumtree\.com/p/[^\s\"']*/(\d+)")
_EBAY_ID_RE = re.compile(r"ebay\.co\.uk/itm/(?:[^\s\"']*?)(\d{9,})")


class EmailIntakeSource(Source):
    name = "email_intake"

    def __init__(self, config, store):
        super().__init__(config, store)
        self.rules = config.get("sources.email_intake.rules", [])
        self.allowed_domains = config.get(
            "sources.email_intake.allowed_link_domains",
            ["facebook.com", "gumtree.com", "ebay.co.uk"])

    # ---- IMAP ----------------------------------------------------------------
    def _connect(self) -> imaplib.IMAP4_SSL:
        host = self.config.env("IMAP_HOST", "imap.gmail.com")
        port = int(self.config.env("IMAP_PORT", "993"))
        user = self.config.env("IMAP_USER")
        password = self.config.env("IMAP_PASSWORD")
        if not user or not password:
            raise RuntimeError("IMAP_USER / IMAP_PASSWORD not set in .env")
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)
        return conn

    def _select_folder(self, conn: imaplib.IMAP4_SSL) -> str:
        folder = self.config.get("sources.email_intake.folder", "LEGO-Alerts")
        status, _ = conn.select(f'"{folder}"')
        if status == "OK":
            return folder
        fallback = self.config.get("sources.email_intake.fallback_folder", "INBOX")
        log.warning("IMAP folder %r not found, using %r", folder, fallback)
        conn.select(fallback)
        return fallback

    # ---- rule matching ---------------------------------------------------------
    def match_rule(self, sender: str, subject: str) -> dict | None:
        sender_l, subject_l = sender.lower(), subject.lower()
        for rule in self.rules:
            if not any(d.lower() in sender_l for d in rule.get("from_domains", [])):
                continue
            pats = rule.get("subject_patterns", [])
            if not pats or any(p.lower() in subject_l for p in pats):
                return rule
        return None

    # ---- parsing ------------------------------------------------------------------
    def extract_listings(self, html: str, rule: dict) -> list[Listing]:
        """Pull listing URLs out of an alert email plus whatever title/price/
        thumbnail data sits near them, so we have something even if the full
        listing page later needs a login to load."""
        soup = BeautifulSoup(html, "html.parser")
        found: dict[str, Listing] = {}
        for a in soup.find_all("a", href=True):
            url = unwrap_facebook_url(a["href"].strip())
            if not domain_allowed(url, self.allowed_domains):
                continue
            sid, source = self._listing_id(url, rule)
            if not sid:
                continue
            title, price, image = self._context_for_anchor(a)
            existing = found.get(sid)
            if existing:
                # several anchors (image + title) point at the same listing;
                # keep the richest data
                existing.title = existing.title or title
                existing.price = existing.price if existing.price is not None else price
                if image and image not in existing.image_urls:
                    existing.image_urls.append(image)
                continue
            found[sid] = Listing(
                source=source,
                source_id=sid,
                url=url,
                title=title,
                price=price,
                image_urls=[image] if image else [],
                attributes={"via": "email", "rule": rule.get("name", "")},
            )
        for l in found.values():
            l.title = l.title or f"{l.source} listing {l.source_id}"
        return list(found.values())

    @staticmethod
    def _listing_id(url: str, rule: dict) -> tuple[str | None, str]:
        fb = facebook_item_id(url)
        if fb:
            return fb, "facebook"
        m = _GUMTREE_ID_RE.search(url)
        if m:
            return m.group(1), "gumtree"
        m = _EBAY_ID_RE.search(url)
        if m:
            return m.group(1), "ebay"
        return None, rule.get("source", "email")

    @staticmethod
    def _context_for_anchor(a) -> tuple[str, float | None, str | None]:
        text = sanitize_text(a.get_text(" ", strip=True), 300)
        # walk up a couple of levels to find nearby price / title text
        node, price = a, None
        for _ in range(3):
            node = node.parent
            if node is None:
                break
            block = node.get_text(" ", strip=True)
            price = parse_price(block)
            if price is not None:
                if not text or re.fullmatch(r"[£\d.,\s]+", text):
                    # anchor text was just the price/image; take a title line
                    lines = [l for l in re.split(r"\s{2,}|\n", block)
                             if l and "£" not in l]
                    if lines:
                        text = sanitize_text(lines[0], 300)
                break
        img = a.find("img")
        image = img.get("src") if img and img.get("src", "").startswith("http") else None
        title = "" if re.fullmatch(r"[£\d.,\s]*", text or "") else text
        return title, price, image

    # ---- Source interface ------------------------------------------------------------
    def fetch_new_listings(self) -> list[Listing]:
        conn = with_retries(self._connect, attempts=3, label="IMAP connect")
        listings: list[Listing] = []
        try:
            self._select_folder(conn)
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return []
            ids = data[0].split()
            log.info("email intake: %d unread messages", len(ids))
            for num in ids:
                try:
                    listings.extend(self._process_message(conn, num))
                except Exception:  # noqa: BLE001 - one bad email must not stop the rest
                    log.exception("failed processing message %s", num)
            self.store.prune_emails(
                int(self.config.get("sources.email_intake.keep_raw_days", 14)))
        finally:
            try:
                conn.close()
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return listings

    def _process_message(self, conn, num) -> list[Listing]:
        status, msg_data = conn.fetch(num, "(RFC822)")
        if status != "OK" or not msg_data or msg_data[0] is None:
            return []
        msg = email.message_from_bytes(msg_data[0][1], policy=email.policy.default)
        message_id = msg.get("Message-ID", "").strip() or f"no-id-{num.decode()}"
        sender = msg.get("From", "")
        subject = msg.get("Subject", "")

        if self.store.email_processed(message_id):
            self._mark_seen(conn, num)
            return []

        rule = self.match_rule(sender, subject)
        if rule is None:
            return []  # not an alert email; leave it unread for the human

        html = self._html_body(msg)
        found = self.extract_listings(html, rule) if html else []
        parse_ok = bool(found)
        # keep raw HTML only when parsing failed, for debugging
        self.store.record_email(message_id, sanitize_text(sender, 200),
                                sanitize_text(subject, 300),
                                "" if parse_ok else html[:200_000],
                                len(found), parse_ok)
        if not parse_ok:
            log.error("alert email matched rule %r but no listings parsed "
                      "(subject=%r) — raw HTML kept in DB emails table",
                      rule.get("name"), subject)
            self._notify_parse_failure(rule, subject)
        if self.config.get("sources.email_intake.mark_processed", True):
            self._mark_seen(conn, num)
        return found

    @staticmethod
    def _html_body(msg) -> str:
        body = msg.get_body(preferencelist=("html", "plain"))
        if body is None:
            return ""
        content = body.get_content()
        if body.get_content_type() == "text/plain":
            # linkify plain text so extract_listings still finds URLs
            content = "".join(f'<a href="{u}">{u}</a>'
                              for u in re.findall(r"https?://\S+", content))
        return content

    @staticmethod
    def _mark_seen(conn, num):
        try:
            conn.store(num, "+FLAGS", "\\Seen")
        except Exception:  # noqa: BLE001
            log.warning("could not mark message %s seen", num)

    def _notify_parse_failure(self, rule: dict, subject: str):
        from ..alerts import send_ops_alert
        send_ops_alert(self.config,
                       f"Email parser broke for rule '{rule.get('name')}' "
                       f"(subject: {subject[:80]}). Raw HTML saved in DB.")
