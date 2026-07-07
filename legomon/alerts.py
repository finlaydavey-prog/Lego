"""Stage 6: alerts via ntfy.sh and/or Telegram. Speed matters — good local
LEGO deals go in hours."""
from __future__ import annotations

import logging

import requests

from .config import Config
from .util import with_retries

log = logging.getLogger("legomon.alerts")


def format_deal(listing: dict, score: dict, condition: dict,
                sets: list[dict]) -> tuple[str, str]:
    """Returns (title, body) for a deal alert."""
    set_bits = ", ".join(f"{s['set_number'].split('-')[0]} {s.get('name', '')}".strip()
                         for s in sets) or "unidentified"
    title = f"LEGO deal: £{listing['price']:.0f} → est £{score['estimated_net']:.0f} ({score['margin_pct']:+.0%})"
    flags = ", ".join(condition.get("flags", [])) or "none"
    body = (
        f"{listing['title']}\n"
        f"Sets: {set_bits}\n"
        f"Asking: £{listing['price']:.2f} | Est. resale (net): £{score['estimated_net']:.2f} "
        f"| Margin: £{score['margin']:.2f} ({score['margin_pct']:.0%})\n"
        f"Condition: {condition.get('condition', 'unknown')} | Flags: {flags}\n"
        f"Location: {listing.get('location') or 'unknown'} | Source: {listing['source']}\n"
        f"{listing['url']}"
    )
    return title, body


def send_ntfy(config: Config, title: str, body: str, url: str | None = None,
              priority: str = "high") -> bool:
    topic = config.env("NTFY_TOPIC")
    if not topic:
        return False
    server = config.env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("latin-1", "replace").decode("latin-1"),
               "Priority": priority, "Tags": "bricks,moneybag"}
    if url:
        headers["Click"] = url

    def _post():
        r = requests.post(f"{server}/{topic}", data=body.encode(),
                          headers=headers, timeout=15)
        r.raise_for_status()
        return r

    try:
        with_retries(_post, attempts=3, label="ntfy")
        return True
    except Exception as e:  # noqa: BLE001
        log.error("ntfy alert failed: %s", e)
        return False


def send_telegram(config: Config, title: str, body: str) -> bool:
    token = config.env("TELEGRAM_BOT_TOKEN")
    chat_id = config.env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    def _post():
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{title}\n\n{body}",
                  "disable_web_page_preview": False},
            timeout=15)
        r.raise_for_status()
        return r

    try:
        with_retries(_post, attempts=3, label="telegram")
        return True
    except Exception as e:  # noqa: BLE001
        log.error("telegram alert failed: %s", e)
        return False


def send_deal_alert(config: Config, listing: dict, score: dict,
                    condition: dict, sets: list[dict]) -> list[str]:
    """Send to all configured channels; returns channels that succeeded."""
    title, body = format_deal(listing, score, condition, sets)
    sent = []
    channels = config.get("alerts.channels", ["ntfy"])
    if "ntfy" in channels and send_ntfy(config, title, body, listing.get("url")):
        sent.append("ntfy")
    if "telegram" in channels and send_telegram(config, title, body):
        sent.append("telegram")
    if not sent:
        log.warning("deal found but no alert channel delivered: %s", title)
    return sent


def send_ops_alert(config: Config, message: str):
    """Low-priority operational alert (parser breakage etc.)."""
    channels = config.get("alerts.channels", ["ntfy"])
    if "ntfy" in channels:
        send_ntfy(config, "legomon: attention needed", message, priority="default")
    if "telegram" in channels:
        send_telegram(config, "legomon: attention needed", message)
