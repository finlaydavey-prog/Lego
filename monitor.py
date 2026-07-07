#!/usr/bin/env python3
"""LEGO marketplace arbitrage monitor.

  python monitor.py run [--dry-run] [--source gumtree] # one poll of all sources
  python monitor.py run --loop                         # scheduler loop with jitter
  python monitor.py report [--limit 20]                # last scored listings
  python monitor.py price 75192                        # standalone pricing test
  python monitor.py identify-photo photo.jpg           # standalone vision test
  python monitor.py assess-photo photo.jpg             # standalone condition test
  python monitor.py test-alert                         # ping alert channels
  python monitor.py fb-login                           # one-off Facebook login (tier 2)

Cron example (Gumtree+email handled internally by --loop; or run one-shot):
  */45 * * * * cd /path/to/Lego && python3 monitor.py run >> logs/cron.log 2>&1
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

from legomon.config import Config
from legomon.db import Store
from legomon.pipeline import Pipeline
from legomon.sources import load_sources


def setup_logging(root: Path):
    (root / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(root / "logs" / "monitor.log")])


def cmd_run(args, config: Config, store: Store):
    pipeline = Pipeline(config, store)
    sources = load_sources(config, store)
    if args.source:
        sources = [s for s in sources if s.name == args.source]
        if not sources:
            print(f"source {args.source!r} not enabled/known", file=sys.stderr)
            return 1
    if not sources:
        print("no sources enabled — check config.yaml and .env", file=sys.stderr)
        return 1
    names = ", ".join(s.name for s in sources)
    if not args.loop:
        logging.info("single run: %s%s", names, " [dry-run]" if args.dry_run else "")
        n = pipeline.run_sources(sources, dry_run=args.dry_run)
        logging.info("run complete: %d listings processed", n)
        return 0

    # scheduler loop: each source has its own cadence with jitter
    logging.info("loop mode: %s", names)
    next_run = {s.name: time.time() + random.uniform(0, 30) for s in sources}
    tick = int(config.get("scheduler.tick_seconds", 30))
    while True:
        now = time.time()
        for s in sources:
            if now >= next_run[s.name]:
                pipeline.run_sources([s], dry_run=args.dry_run)
                jitter = s.jitter_minutes() * 60
                next_run[s.name] = (time.time() + s.poll_minutes() * 60
                                    + random.uniform(-jitter, jitter))
                logging.info("next %s poll in %.0f min", s.name,
                             (next_run[s.name] - time.time()) / 60)
        time.sleep(tick)


def cmd_report(args, config: Config, store: Store):
    rows = store.top_scored(args.limit)
    if not rows:
        print("no scored listings yet")
        return 0
    rows = sorted(rows, key=lambda r: r["margin_pct"] or 0, reverse=True)
    print(f"{'id':>4}  {'margin':>8}  {'%':>6}  {'ask':>8}  {'est net':>8}  "
          f"{'condition':<16} {'source':<9} title")
    for r in rows:
        print(f"{r['id']:>4}  £{r['margin']:>7.2f}  {r['margin_pct']:>5.0%}  "
              f"£{r['price']:>7.2f}  £{r['estimated_net']:>7.2f}  "
              f"{(r['condition'] or 'unknown'):<16} {r['source']:<9} "
              f"{r['title'][:48]}")
        print(f"{'':>4}  {r['url']}")
    return 0


def cmd_price(args, config: Config, store: Store):
    from legomon.identify import Identifier
    from legomon.pricing import Pricer
    ident = Identifier(config, store)
    meta = ident.lookup_set(args.set_number.split("-")[0])
    if meta:
        print(f"set {meta['set_number']}: {meta['name']} "
              f"({meta['year']}, {meta['pieces']} pieces)")
    else:
        print(f"warning: {args.set_number} not validated by Rebrickable "
              "(missing key or unknown set)")
    pricer = Pricer(config, store)
    for cond in ("new", "used"):
        r = pricer.market_value(args.set_number, cond)
        print(f"  {cond:>4}: £{r['value']}" if r["value"] else f"  {cond:>4}: n/a",
              f"(source: {r['source']}, sales: {r['n_sales']})"
              + (f" | brickeconomy sanity: £{r['sanity_check']}" if r["sanity_check"] else ""))
    return 0


def cmd_identify_photo(args, config: Config, store: Store):
    from legomon.identify import Identifier
    ident = Identifier(config, store)
    result = ident.identify_from_photos(args.title or "test photo", "", [args.photo])
    print(result if result else "no identification (check ANTHROPIC_API_KEY)")
    return 0


def cmd_assess_photo(args, config: Config, store: Store):
    from legomon.condition import ConditionAssessor
    assessor = ConditionAssessor(config, store)
    result = assessor._assess_with_claude(args.title or "test photo", "", "",
                                          [args.photo])
    print(result if result else "no assessment (check ANTHROPIC_API_KEY)")
    return 0


def cmd_test_alert(args, config: Config, store: Store):
    from legomon.alerts import send_ntfy, send_telegram
    ok = []
    if send_ntfy(config, "legomon test", "Alert channel is working.", priority="default"):
        ok.append("ntfy")
    if send_telegram(config, "legomon test", "Alert channel is working."):
        ok.append("telegram")
    print(f"delivered to: {', '.join(ok) if ok else 'nothing — check .env'}")
    return 0 if ok else 1


def cmd_fb_login(args, config: Config, store: Store):
    from legomon.sources.facebook import interactive_login
    interactive_login(config)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="poll sources and process listings")
    p.add_argument("--dry-run", action="store_true",
                   help="score listings but send no alerts")
    p.add_argument("--loop", action="store_true", help="run continuously")
    p.add_argument("--source", help="only this source (gumtree|email_intake|facebook|ebay)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="last scored listings by margin")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("price", help="standalone pricing test")
    p.add_argument("set_number")
    p.set_defaults(fn=cmd_price)

    p = sub.add_parser("identify-photo", help="standalone vision identification test")
    p.add_argument("photo")
    p.add_argument("--title", default="")
    p.set_defaults(fn=cmd_identify_photo)

    p = sub.add_parser("assess-photo", help="standalone condition assessment test")
    p.add_argument("photo")
    p.add_argument("--title", default="")
    p.set_defaults(fn=cmd_assess_photo)

    p = sub.add_parser("test-alert", help="ping alert channels")
    p.set_defaults(fn=cmd_test_alert)

    p = sub.add_parser("fb-login", help="one-off manual Facebook login (tier 2)")
    p.set_defaults(fn=cmd_fb_login)

    args = parser.parse_args()
    config = Config()
    setup_logging(config.root)
    store = Store(config.path("storage.db_path", "data/monitor.sqlite3"))
    sys.exit(args.fn(args, config, store) or 0)


if __name__ == "__main__":
    main()
