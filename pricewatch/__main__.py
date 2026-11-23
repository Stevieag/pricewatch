"""CLI:  python -m pricewatch [serve | check URL | check-all [--no-fair] | config | gen-token]"""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys

from .agent import PriceAgent
from .config import load_settings, validate_settings
from .db import DB
from .fair import FairRunner
from .monitor import Scheduler, fetch_price


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pricewatch")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the web UI + scheduler (default)")
    sub.add_parser("gen-token", help="print a random access token for PW_ACCESS_TOKEN")
    sub.add_parser("config", help="show effective config (secrets are never printed)")
    c = sub.add_parser("check", help="fetch one URL and print the extracted price")
    c.add_argument("url")
    ca = sub.add_parser("check-all", help="re-check every product in your database now")
    ca.add_argument("--no-fair", action="store_true", help="skip the Fair Price check (faster)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "gen-token":
        print(secrets.token_urlsafe(32))
        return 0

    s = load_settings()
    if args.cmd == "config":
        print(json.dumps(s.public_summary(), indent=2))
        return 0
    if args.cmd == "check":
        from .fetcher import FetchError
        try:
            ex, final = fetch_price(s, args.url)
        except FetchError as e:
            print(f"Couldn't read that page: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"url": final, "title": ex.title, "price": str(ex.price) if ex.price is not None else None,
                          "currency": ex.currency, "method": ex.method, "in_stock": ex.in_stock}, indent=2))
        return 0

    if args.cmd == "check-all":
        return check_all(s, fair_check=not args.no_fair)

    problems = validate_settings(s)
    if problems:
        for p in problems:
            print(f"[pricewatch] {p}", file=sys.stderr)
        return 2

    from .web import serve
    db = DB(s.db_path)
    agent = PriceAgent(s, db)
    fair = FairRunner(s, db)
    sched = Scheduler(s, db, agent_runner=agent.run_in_background, fair_runner=fair.run_in_background)
    sched.start()
    httpd = serve(s, db, agent, fair)
    print(f"PriceWatch running at http://{s.host}:{s.port}  (data: {s.data_dir})")
    if s.env_file is None:
        print("No env file found - using defaults. Copy .env.example to ~/.config/pricewatch/pricewatch.env")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()
        httpd.server_close()
    return 0


def check_all(s, fair_check: bool = True) -> int:
    """Re-check every item (and every alternative) in the real database, then Fair Price."""
    import time
    from decimal import Decimal

    from . import fair
    from .monitor import check_item
    from .util import domain_of, fmt_price

    if not s.db_path.exists():
        print(f"No database yet at {s.db_path}. Add products with: python3 -m pricewatch serve")
        return 1
    db = DB(s.db_path)
    items = db.q("SELECT * FROM items ORDER BY IFNULL(parent_id, id), parent_id IS NOT NULL, id")
    if not items:
        print("Your database has no products yet. Add some with: python3 -m pricewatch serve")
        return 1
    tops = [i for i in items if i["parent_id"] is None]
    print(f"Checking {len(items)} listing(s) across {len(tops)} product(s) in {s.db_path}\n")
    ok = failed = 0
    for it in items:
        before = it["current_price"]
        t = time.time()
        check_item(s, db, it)
        now_it = db.get_item(it["id"])
        name = (now_it["title"] or domain_of(now_it["url"]))[:48]
        prefix = "  " if it["parent_id"] is None else "    ↳ "
        if now_it["last_error"]:
            failed += 1
            print(f"{prefix}FAIL  {name}  [{domain_of(now_it['url'])}]  {now_it['last_error']}")
            continue
        ok += 1
        change = ""
        if before and now_it["current_price"] and Decimal(before) != Decimal(now_it["current_price"]):
            d = Decimal(now_it["current_price"]) - Decimal(before)
            change = f"  ({'▲' if d > 0 else '▼'} {fmt_price(abs(d), now_it['currency'])} since last check)"
        print(f"{prefix}OK    {name}  [{domain_of(now_it['url'])}]  "
              f"{fmt_price(now_it['current_price'], now_it['currency'])}  via {now_it['extract_method']}"
              f"  {time.time() - t:.0f}s{change}")

    if fair_check:
        print(f"\nFair Price ({len(tops)} product(s); this is the slow part)")
        for it in tops:
            fair.run_personas(s, db, it["id"])
            rep = fair.report(s, db, it["id"])
            name = (it["title"] or domain_of(it["url"]))[:48]
            print(f"\n  {name}")
            print(f"    fair price {fmt_price(rep.fair_price, it['currency'])} · anonymous now "
                  f"{fmt_price(rep.baseline, it['currency'])}"
                  + (f" · you saw {fmt_price(rep.seen, it['currency'])}" if rep.seen is not None else ""))
            for v in rep.verdicts:
                mark = {"good": "✓", "warn": "⚠", "bad": "✕"}.get(v.level, "ℹ")
                print(f"    {mark} {v.title}: {v.detail}")
            if rep.cheapest_when:
                print(f"    ⏱ {rep.cheapest_when}")

    print(f"\n{ok} listing(s) checked OK, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
