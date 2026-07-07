"""Stage 4: condition assessment via the Claude API (photos + description).

Standalone test: python monitor.py assess-photo path/to/photo.jpg
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from .config import Config
from .db import Store
from .util import with_retries

log = logging.getLogger("legomon.condition")

CONDITIONS = ["sealed", "boxed-complete", "boxed-incomplete",
              "built-complete", "loose-bulk", "unknown"]
FLAGS = ["yellowing", "sun_damage", "damaged_box", "missing_minifigures",
         "knockoff_bricks", "smoke_pet", "stickers_applied"]

PROMPT = """You are assessing the condition of a second-hand LEGO listing for
resale valuation.

Listing title: {title}
Listing description: {description}
Seller-stated condition (if any): {stated}

Examine the attached photo(s) and the text. Respond with ONLY a JSON object:
{{
  "condition": one of {conditions},
  "flags": subset of {flags},
  "completeness_confidence": 0.0-1.0,
  "notes": "anything affecting resale value (max 2 sentences)"
}}

Guidance: "sealed" only if the box is clearly unopened. Mentions of smoke or
pets in the description => flag smoke_pet. Visible discoloured white/grey
bricks => yellowing. Non-LEGO bricks mixed in => knockoff_bricks."""


class ConditionAssessor:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    def assess(self, listing_id: int) -> dict:
        """Returns {condition, flags, completeness_confidence, notes}; persists.
        Falls back to text heuristics when no API key / photos."""
        row = self.store.get_listing(listing_id)
        if row is None:
            return self._default()
        photos = json.loads(row["photo_paths"] or "[]")
        attrs = json.loads(row["attributes"] or "{}")
        stated = attrs.get("Condition") or attrs.get("condition") or ""

        result = self._assess_with_claude(row["title"], row["description"],
                                          stated, photos)
        if result is None:
            result = self._assess_from_text(row["title"], row["description"], stated)
        self.store.save_condition(listing_id, result["condition"], result["flags"],
                                  result["completeness_confidence"], result["notes"])
        return result

    # ---- Claude vision ------------------------------------------------------
    def _assess_with_claude(self, title, description, stated, photos) -> dict | None:
        api_key = self.config.env("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        try:
            import anthropic
        except ImportError:
            return None
        client = anthropic.Anthropic(api_key=api_key)
        content = []
        for p in photos[:int(self.config.get("identification.max_photos_per_listing", 4))]:
            path = Path(p)
            if not path.exists():
                continue
            media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": media_type,
                                       "data": base64.b64encode(path.read_bytes()).decode()}})
        content.append({"type": "text", "text": PROMPT.format(
            title=title[:300], description=description[:2000],
            stated=stated or "not stated",
            conditions=CONDITIONS, flags=FLAGS)})

        def _call():
            return client.messages.create(
                model=self.config.get("identification.claude_model", "claude-sonnet-5"),
                max_tokens=500,
                messages=[{"role": "user", "content": content}])

        try:
            resp = with_retries(_call, attempts=3, label="claude condition")
        except Exception as e:  # noqa: BLE001
            log.error("claude condition assessment failed: %s", e)
            return None
        m = re.search(r"\{.*\}", resp.content[0].text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        return self._normalise(data)

    # ---- text-only heuristic fallback ----------------------------------------
    def _assess_from_text(self, title: str, description: str, stated: str) -> dict:
        text = f"{title} {description} {stated}".lower()
        flags = []
        if re.search(r"\bsmok|pet[s ]|dog|cat\b", text):
            flags.append("smoke_pet")
        if "yellow" in text:
            flags.append("yellowing")
        if re.search(r"box (is )?(damaged|squashed|crushed|worn|tatty)", text):
            flags.append("damaged_box")
        if re.search(r"missing|incomplete|not complete|some pieces", text):
            flags.append("missing_minifigures" if "figure" in text or "minifig" in text
                         else "damaged_box")
        if re.search(r"\b(new|sealed|unopened|bnib|nisb)\b", text):
            condition = "sealed"
        elif re.search(r"job ?lot|bundle|loose|bulk|\bkg\b|mixed", text):
            condition = "loose-bulk"
        elif re.search(r"complete", text) and "box" in text:
            condition = "boxed-complete"
        elif re.search(r"built|assembled|displayed", text):
            condition = "built-complete"
        elif re.search(r"missing|incomplete", text):
            condition = "boxed-incomplete"
        else:
            condition = "unknown"
        return {"condition": condition, "flags": flags,
                "completeness_confidence": 0.3,
                "notes": "text-heuristic assessment (no photos/API)"}

    @staticmethod
    def _normalise(data: dict) -> dict:
        cond = str(data.get("condition", "unknown")).lower().strip()
        if cond not in CONDITIONS:
            cond = "unknown"
        flags = [f for f in (data.get("flags") or []) if f in FLAGS]
        try:
            conf = max(0.0, min(1.0, float(data.get("completeness_confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        return {"condition": cond, "flags": flags,
                "completeness_confidence": conf,
                "notes": str(data.get("notes", ""))[:500]}

    @staticmethod
    def _default() -> dict:
        return {"condition": "unknown", "flags": [],
                "completeness_confidence": 0.0, "notes": "listing not found"}
