# LEGO Marketplace Arbitrage Monitor

Watches Gumtree (and, via its own notification emails, Facebook Marketplace)
for LEGO listings near **NE6 5SD**, identifies the sets, prices them against
6-month sold data, assesses condition from photos with the Claude API, and
pushes an alert when a listing is priced at **≤ 60% of realistic net resale
value** (configurable).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in the keys you have (see below)
python monitor.py run --dry-run          # one poll, no alerts
python monitor.py report                 # last scored listings by margin
python monitor.py run --loop             # continuous, with per-source jitter
```

Cron alternative to `--loop`:

```cron
*/45 * * * * cd /path/to/Lego && python3 monitor.py run >> logs/cron.log 2>&1
```

Everything tunable lives in `config.yaml` (thresholds, fees, condition
multipliers, poll intervals, email rules). All scraping selectors live in
`selectors.yaml` — when Gumtree changes markup, fix it there, not in code.
Secrets live only in `.env`.

## Sources

| Source | Status | Needs |
|---|---|---|
| **Gumtree** | always-on, fully automated | nothing |
| **Facebook via email intake** (tier 1) | recommended, on when IMAP creds set | one-off FB app setup + IMAP app password |
| Facebook logged-out probe (tier 3) | off by default | nothing |
| Facebook logged-in Playwright (tier 2) | off by default, **ToS risk** | `playwright`, one-off manual login |
| eBay local (Browse API) | on when keys set | free eBay developer keys |

### Gumtree

Polls the public search results pages for each search term plus a
`baby-kids-stuff` category sweep (that's where LEGO actually sits — verified
live), sorted newest-first, ~45 min apart with jitter and 20 s between pages.
Parsing reads the `window.clientData` JSON embedded in the page (includes
titles, prices, epoch timestamps, descriptions, seller-declared condition, and
"nearby" results just outside the radius, which get tagged `nearby`); if that
ever disappears there is an automatic CSS-selector fallback and a logged
warning.

**robots.txt note:** gumtree.com disallows `/search` for crawlers. This tool
makes a handful of requests per poll on your behalf rather than crawling, but
if you want strict compliance set `sources.gumtree.enforce_robots: true`
(which currently disables the Gumtree source, since the robots-allowed
`/for-sale/...` paths are bot-blocked at the CDN). If plain requests get
blocked (HTTP 471), the source falls back to Playwright automatically when
it's installed.

### Facebook Marketplace — email intake (tier 1, recommended)

Facebook does the monitoring on its own infrastructure; we just read the mail.
One-off setup:

1. In the Facebook app, search Marketplace for "lego", set radius, tap
   **"Notify me about new listings like these"**. Repeat per search term.
2. Settings → Notifications → Marketplace → enable **Email**.
3. (Gmail) Create a filter labelling these emails `LEGO-Alerts`. Point your
   Gumtree/eBay saved-search emails at the same label — one watcher covers all.
4. Create an [App Password](https://myaccount.google.com/apppasswords) and put
   it in `.env` (`IMAP_*` variables). Any IMAP host works, so a dedicated
   address like `lego.alerts@…` is ideal.

The watcher polls the label every 5 minutes for unread messages matching the
sender/subject rules in `config.yaml` (add new alert formats there — no code
changes). It unwraps Facebook's `l.facebook.com/l.php?u=…` tracking redirects
back to canonical `facebook.com/marketplace/item/<id>` URLs, extracts
title/price/thumbnail from the email body, de-duplicates by item id, marks
messages read, and keeps raw HTML briefly (14 days) only when parsing fails —
in which case you also get an ops alert instead of silent data loss. Email
content is treated as untrusted: text is sanitised and only links on
facebook.com / gumtree.com / ebay.co.uk are ever followed.

### Facebook tiers 2 & 3 (optional, off by default)

- **Tier 3 (logged-out probe)**: opportunistically fetches individual listing
  pages without logging in to enrich email-intake data; the moment a login
  redirect appears it gives up. It never attempts to bypass or automate login
  or anti-bot checks. Enable: `sources.facebook.loggedout_probe.enabled: true`.
- **Tier 2 (logged-in Playwright)**: `pip install playwright && playwright
  install chromium`, run `python monitor.py fb-login` once to log in manually,
  then enable `sources.facebook.loggedin`. Polls every ~3 h with jitter.
  **Warning: this breaches Facebook's Terms of Service and can get the account
  flagged or locked.** Use a burner account if you use it at all.

### eBay local (optional)

Official Browse API, no scraping. Register at developer.ebay.com, put
`EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET` in `.env` and it joins the rotation:
LEGO within 5 miles, newest first.

## Pipeline

1. **Capture** → SQLite (`data/monitor.sqlite3`), keyed on (source, listing id)
   so nothing is processed or alerted twice. Price changes on relisted items
   are tracked in `price_history` and re-queue the listing for scoring.
2. **Identify** → regex for 4–7-digit set numbers (piece counts filtered out,
   year-like numbers deprioritised), validated against **Rebrickable**; if no
   number validates, photos go to the **Claude API** which returns candidate
   sets with confidence — for bundles/job lots it lists every visible set and
   estimates loose-brick weight.
3. **Price** → **BrickLink Price Guide** 6-month *sold* averages (OAuth1), New
   vs Used, cached 7 days. Optional BrickEconomy sanity check
   (`pricing.brickeconomy.enabled` — scrape-based and fragile, off by
   default). No BrickLink creds → falls back to an RRP/piece-count heuristic,
   clearly marked `rrp_estimate` in the data.
4. **Condition** → Claude returns structured JSON: condition class
   (sealed/boxed-complete/boxed-incomplete/built-complete/loose-bulk/unknown),
   flags (yellowing, damaged box, missing minifigures, knock-off bricks,
   smoke/pets, stickers), completeness confidence, notes. Sealed prices
   against New, everything else Used, with configurable multipliers and
   per-flag discounts. Without an API key a text-only heuristic runs instead.
5. **Score** → `net = market × multiplier × (1 − 13% fees)`;
   `margin = net − asking`. Deal when `asking ≤ 60% × net` and margin ≥ £10.
6. **Alert** → ntfy.sh (default; set `NTFY_TOPIC` and subscribe in the app)
   and/or Telegram (`alerts.channels` in config). Includes set name/number,
   asking price, estimated net resale, margin %, condition + flags, location,
   and the direct listing URL.

## CLI

```
python monitor.py run [--dry-run] [--loop] [--source gumtree]
python monitor.py report [--limit 20]      # scored listings, best margin first
python monitor.py price 75192              # standalone pricing test
python monitor.py identify-photo pic.jpg   # standalone vision-ID test
python monitor.py assess-photo pic.jpg     # standalone condition test
python monitor.py test-alert               # ping your alert channels
python monitor.py fb-login                 # one-off manual FB login (tier 2)
```

## Tests

```bash
python -m pytest tests/
```

37 tests, no network needed: scoring maths, set-number extraction, Facebook
redirect unwrapping, email parsing against a real-format FB alert fixture,
Gumtree parsing against a live-captured fixture (both JSON and CSS-fallback
paths), dedupe/price-change tracking, and a stubbed end-to-end pipeline run.
