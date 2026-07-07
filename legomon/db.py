"""SQLite store. Listing (source, source_id) is the dedupe key across the
whole pipeline — we never process or alert on the same listing twice."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    price REAL,
    currency TEXT DEFAULT 'GBP',
    location TEXT DEFAULT '',
    image_urls TEXT DEFAULT '[]',       -- JSON list
    photo_paths TEXT DEFAULT '[]',      -- JSON list of local files
    posted_at TEXT,
    attributes TEXT DEFAULT '{}',       -- JSON dict
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT DEFAULT 'new',          -- new|identified|priced|scored|error
    UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS price_history (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    price REAL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sets (
    set_number TEXT PRIMARY KEY,        -- canonical e.g. "75192-1"
    name TEXT, theme TEXT, year INTEGER, pieces INTEGER,
    rrp_gbp REAL, image_url TEXT,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identifications (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    set_number TEXT NOT NULL,
    confidence REAL NOT NULL,
    method TEXT NOT NULL,               -- regex|vision
    quantity INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    UNIQUE(listing_id, set_number)
);
CREATE TABLE IF NOT EXISTS price_cache (
    set_number TEXT NOT NULL,
    condition TEXT NOT NULL,            -- new|used
    source TEXT NOT NULL,               -- bricklink|brickeconomy|rrp_estimate
    avg_price REAL,
    n_sales INTEGER,
    currency TEXT DEFAULT 'GBP',
    fetched_at TEXT NOT NULL,
    UNIQUE(set_number, condition, source)
);
CREATE TABLE IF NOT EXISTS conditions (
    listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
    condition TEXT NOT NULL,
    flags TEXT DEFAULT '[]',            -- JSON list
    completeness_confidence REAL,
    notes TEXT DEFAULT '',
    assessed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
    market_value REAL,
    condition_multiplier REAL,
    estimated_net REAL,
    margin REAL,
    margin_pct REAL,
    detail TEXT DEFAULT '{}',           -- JSON breakdown per set
    scored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts_sent (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    channel TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE(listing_id, channel)
);
CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT PRIMARY KEY,
    sender TEXT, subject TEXT,
    raw_html TEXT,                      -- kept briefly for debugging
    received_at TEXT, processed_at TEXT,
    n_listings INTEGER DEFAULT 0,
    parse_ok INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_scores_margin ON scores(margin_pct);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- listings -------------------------------------------------------
    def upsert_listing(self, l: Listing) -> tuple[int, bool, bool]:
        """Returns (listing_id, is_new, price_changed)."""
        cur = self.conn.execute(
            "SELECT id, price FROM listings WHERE source=? AND source_id=?",
            (l.source, l.source_id),
        )
        row = cur.fetchone()
        now = _now()
        if row is None:
            cur = self.conn.execute(
                """INSERT INTO listings (source, source_id, url, title, description,
                   price, currency, location, image_urls, posted_at, attributes,
                   first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (l.source, l.source_id, l.url, l.title, l.description, l.price,
                 l.currency, l.location, json.dumps(l.image_urls),
                 l.posted_at.isoformat() if l.posted_at else None,
                 json.dumps(l.attributes), now, now),
            )
            lid = cur.lastrowid
            self.conn.execute(
                "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,?,?)",
                (lid, l.price, now))
            self.conn.commit()
            return lid, True, False

        lid = row["id"]
        price_changed = l.price is not None and row["price"] != l.price
        self.conn.execute("UPDATE listings SET last_seen=? WHERE id=?", (now, lid))
        if price_changed:
            self.conn.execute(
                "UPDATE listings SET price=?, status='new' WHERE id=?", (l.price, lid))
            self.conn.execute(
                "INSERT INTO price_history (listing_id, price, seen_at) VALUES (?,?,?)",
                (lid, l.price, now))
        self.conn.commit()
        return lid, False, price_changed

    def listings_with_status(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM listings WHERE status=? ORDER BY id", (status,)).fetchall()

    def set_status(self, listing_id: int, status: str):
        self.conn.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_id))
        self.conn.commit()

    def set_photo_paths(self, listing_id: int, paths: list[str]):
        self.conn.execute("UPDATE listings SET photo_paths=? WHERE id=?",
                          (json.dumps(paths), listing_id))
        self.conn.commit()

    def get_listing(self, listing_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()

    # ---- sets / identification ------------------------------------------
    def save_set(self, set_number: str, name: str, theme: str, year: int | None,
                 pieces: int | None, rrp_gbp: float | None, image_url: str = ""):
        self.conn.execute(
            """INSERT OR REPLACE INTO sets
               (set_number, name, theme, year, pieces, rrp_gbp, image_url, fetched_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (set_number, name, theme, year, pieces, rrp_gbp, image_url, _now()))
        self.conn.commit()

    def get_set(self, set_number: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sets WHERE set_number=?", (set_number,)).fetchone()

    def save_identification(self, listing_id: int, set_number: str, confidence: float,
                            method: str, quantity: int = 1, notes: str = ""):
        self.conn.execute(
            """INSERT OR REPLACE INTO identifications
               (listing_id, set_number, confidence, method, quantity, notes)
               VALUES (?,?,?,?,?,?)""",
            (listing_id, set_number, confidence, method, quantity, notes))
        self.conn.commit()

    def identifications_for(self, listing_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM identifications WHERE listing_id=?", (listing_id,)).fetchall()

    # ---- price cache -----------------------------------------------------
    def cached_price(self, set_number: str, condition: str, source: str,
                     max_age_days: int) -> sqlite3.Row | None:
        row = self.conn.execute(
            """SELECT * FROM price_cache
               WHERE set_number=? AND condition=? AND source=?""",
            (set_number, condition, source)).fetchone()
        if row is None:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        return row if age <= timedelta(days=max_age_days) else None

    def cache_price(self, set_number: str, condition: str, source: str,
                    avg_price: float | None, n_sales: int | None):
        self.conn.execute(
            """INSERT OR REPLACE INTO price_cache
               (set_number, condition, source, avg_price, n_sales, fetched_at)
               VALUES (?,?,?,?,?,?)""",
            (set_number, condition, source, avg_price, n_sales, _now()))
        self.conn.commit()

    # ---- condition / scores / alerts -------------------------------------
    def save_condition(self, listing_id: int, condition: str, flags: list[str],
                       completeness_confidence: float | None, notes: str):
        self.conn.execute(
            """INSERT OR REPLACE INTO conditions
               (listing_id, condition, flags, completeness_confidence, notes, assessed_at)
               VALUES (?,?,?,?,?,?)""",
            (listing_id, condition, json.dumps(flags), completeness_confidence,
             notes, _now()))
        self.conn.commit()

    def get_condition(self, listing_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM conditions WHERE listing_id=?", (listing_id,)).fetchone()

    def save_score(self, listing_id: int, market_value: float, multiplier: float,
                   estimated_net: float, margin: float, margin_pct: float,
                   detail: dict):
        self.conn.execute(
            """INSERT OR REPLACE INTO scores
               (listing_id, market_value, condition_multiplier, estimated_net,
                margin, margin_pct, detail, scored_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (listing_id, market_value, multiplier, estimated_net, margin,
             margin_pct, json.dumps(detail), _now()))
        self.conn.commit()

    def was_alerted(self, listing_id: int, channel: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM alerts_sent WHERE listing_id=? AND channel=?",
            (listing_id, channel)).fetchone() is not None

    def mark_alerted(self, listing_id: int, channel: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO alerts_sent (listing_id, channel, sent_at) VALUES (?,?,?)",
            (listing_id, channel, _now()))
        self.conn.commit()

    # ---- emails -----------------------------------------------------------
    def email_processed(self, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM emails WHERE message_id=?", (message_id,)).fetchone() is not None

    def record_email(self, message_id: str, sender: str, subject: str,
                     raw_html: str, n_listings: int, parse_ok: bool):
        self.conn.execute(
            """INSERT OR REPLACE INTO emails
               (message_id, sender, subject, raw_html, received_at, processed_at,
                n_listings, parse_ok)
               VALUES (?,?,?,?,?,?,?,?)""",
            (message_id, sender, subject, raw_html, _now(), _now(),
             n_listings, int(parse_ok)))
        self.conn.commit()

    def prune_emails(self, keep_days: int):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        self.conn.execute(
            "UPDATE emails SET raw_html='' WHERE processed_at < ?", (cutoff,))
        self.conn.commit()

    # ---- reporting --------------------------------------------------------
    def top_scored(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT l.id, l.source, l.title, l.price, l.location, l.url,
                      s.market_value, s.estimated_net, s.margin, s.margin_pct,
                      s.scored_at, c.condition
               FROM scores s
               JOIN listings l ON l.id = s.listing_id
               LEFT JOIN conditions c ON c.listing_id = l.id
               ORDER BY s.scored_at DESC, s.margin_pct DESC
               LIMIT ?""", (limit,)).fetchall()
