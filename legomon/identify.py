"""Stage 2: set identification.

1. Regex candidate set numbers out of title/description.
2. Validate each against Rebrickable (free API) — a hit also gives us
   name/theme/year/pieces/RRP, cached in the sets table.
3. If nothing validates and photos exist, ask Claude (vision) to identify the
   set(s) from box art / built models — including bundles/job lots.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

import requests

from .config import Config
from .db import Store
from .util import with_retries

log = logging.getLogger("legomon.identify")

# 4-7 digit tokens; exclude piece counts like "2,000 pieces" via context later
SET_NUM_RE = re.compile(r"\b(\d{4,7})\b")
REBRICKABLE_SET_URL = "https://rebrickable.com/api/v3/lego/sets/{num}-1/"

VISION_PROMPT = """You are identifying LEGO sets in a resale listing.

Listing title: {title}
Listing description: {description}

Look at the attached photo(s) of the listing. Identify every LEGO set visible
(from box art, instruction booklets, or recognisable built models). For
bundles / job lots, list EVERY identifiable set. If loose bricks are visible,
estimate their total weight in kg from the photo.

Respond with ONLY a JSON object, no other text:
{{
  "sets": [
    {{"set_number": "75192", "name": "best guess name", "confidence": 0.0-1.0,
      "quantity": 1}}
  ],
  "loose_brick_kg": null or number,
  "is_bundle": true/false,
  "notes": "anything relevant to identification"
}}
If you cannot identify any set, return {{"sets": [], ...}} with notes."""


class Identifier:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    # ---- Rebrickable validation ------------------------------------------
    def lookup_set(self, num: str) -> dict | None:
        """Validate a candidate number against Rebrickable; cache in DB.
        Returns set metadata dict or None if not a real set."""
        canonical = f"{num}-1"
        cached = self.store.get_set(canonical)
        if cached is not None:
            return dict(cached) if cached["name"] else None  # negative-cached
        key = self.config.env("REBRICKABLE_API_KEY")
        if not key:
            if not getattr(self, "_warned_no_key", False):
                log.warning("REBRICKABLE_API_KEY not set — cannot validate set numbers")
                self._warned_no_key = True
            return None

        def _get():
            return requests.get(REBRICKABLE_SET_URL.format(num=num),
                                headers={"Authorization": f"key {key}"}, timeout=30)

        resp = with_retries(_get, attempts=3, label=f"rebrickable {num}")
        if resp.status_code == 404:
            # negative-cache so we don't re-query junk numbers
            self.store.save_set(canonical, "", "", None, None, None)
            return None
        resp.raise_for_status()
        data = resp.json()
        self.store.save_set(canonical, data.get("name", ""), "",
                            data.get("year"), data.get("num_parts"),
                            None, data.get("set_img_url", ""))
        return dict(self.store.get_set(canonical))

    # ---- regex extraction ---------------------------------------------------
    def candidates_from_text(self, title: str, description: str) -> list[str]:
        text = f"{title}\n{description}"
        nums = []
        for m in SET_NUM_RE.finditer(text):
            n = m.group(1)
            # skip obvious years and piece counts ("1500 pieces")
            tail = text[m.end():m.end() + 12].lower()
            if 1949 < int(n) < 2035 and ("piece" not in tail):
                # could be a year OR a real set number (e.g. 2010 isn't a set);
                # keep it — Rebrickable validation is the arbiter, but push
                # year-looking numbers to the back of the queue
                nums.append((1, n))
            elif "piece" in tail or "pcs" in tail or " pc" in tail:
                continue
            else:
                nums.append((0, n))
        seen, ordered = set(), []
        for _, n in sorted(nums, key=lambda t: t[0]):
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        return ordered[:10]

    # ---- Claude vision ------------------------------------------------------
    def identify_from_photos(self, title: str, description: str,
                             photo_paths: list[str]) -> dict | None:
        api_key = self.config.env("ANTHROPIC_API_KEY")
        if not api_key or not photo_paths:
            return None
        try:
            import anthropic
        except ImportError:
            log.error("anthropic package not installed")
            return None
        client = anthropic.Anthropic(api_key=api_key)
        max_photos = int(self.config.get("identification.max_photos_per_listing", 4))
        content = []
        for p in photo_paths[:max_photos]:
            path = Path(p)
            if not path.exists():
                continue
            media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type,
                           "data": base64.b64encode(path.read_bytes()).decode()},
            })
        if not content:
            return None
        content.append({"type": "text", "text": VISION_PROMPT.format(
            title=title[:300], description=description[:2000])})

        def _call():
            return client.messages.create(
                model=self.config.get("identification.claude_model", "claude-sonnet-5"),
                max_tokens=1000,
                messages=[{"role": "user", "content": content}])

        try:
            resp = with_retries(_call, attempts=3, label="claude vision identify")
        except Exception as e:  # noqa: BLE001
            log.error("claude identification failed: %s", e)
            return None
        return _parse_json_reply(resp.content[0].text)

    # ---- orchestration ----------------------------------------------------------
    def identify(self, listing_id: int) -> list[dict]:
        """Run full identification for a listing row; persists results.
        Returns list of {set_number, confidence, quantity} that validated."""
        row = self.store.get_listing(listing_id)
        if row is None:
            return []
        results = []
        for num in self.candidates_from_text(row["title"], row["description"]):
            meta = self.lookup_set(num)
            if meta:
                self.store.save_identification(listing_id, meta["set_number"],
                                               0.95, "regex")
                results.append({"set_number": meta["set_number"],
                                "confidence": 0.95, "quantity": 1})
        if results:
            return results

        photos = json.loads(row["photo_paths"] or "[]")
        vision = self.identify_from_photos(row["title"], row["description"], photos)
        if not vision:
            return []
        min_conf = float(self.config.get("identification.min_confidence", 0.5))
        for s in vision.get("sets", []):
            num = re.sub(r"\D", "", str(s.get("set_number", "")))
            if not num:
                continue
            conf = float(s.get("confidence", 0))
            meta = self.lookup_set(num)
            if meta is None:
                continue
            self.store.save_identification(
                listing_id, meta["set_number"], conf, "vision",
                quantity=int(s.get("quantity", 1)),
                notes=str(vision.get("notes", ""))[:500])
            if conf >= min_conf:
                results.append({"set_number": meta["set_number"],
                                "confidence": conf,
                                "quantity": int(s.get("quantity", 1))})
        if vision.get("loose_brick_kg"):
            self.store.save_identification(
                listing_id, "LOOSE_BULK", 0.5, "vision",
                quantity=max(1, round(float(vision["loose_brick_kg"]))),
                notes=f"~{vision['loose_brick_kg']}kg loose bricks")
        return results


def _parse_json_reply(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        log.error("unparseable JSON from Claude: %s", text[:300])
        return None
