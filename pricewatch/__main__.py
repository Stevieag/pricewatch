"""CLI:  python -m pricewatch [serve | check URL | check-all [--no-fair] | doctor |
                             find-better [ID] | config | gen-token]"""
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
    sub.add_parser("doctor", help="check the AI search setup (Ollama, SearXNG, browser)")
    fb = sub.add_parser("find-better", help="run the AI price search on your products")
    fb.add_argument("id", nargs="?", type=int, help="one product id (default: all products)")
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
    if args.cmd == "doctor":
        return doctor(s)
    if args.cmd == "find-better":
        return find_better(s, args.id)

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


def doctor(s) -> int:
    """Check every moving part of the AI price search, with a fix for each failure."""
    import json as _json
    import urllib.request

    from .agent import LLM, AgentError, searxng_search
    from .fetcher import find_browser

    results = []

    def step(name, ok, detail="", fix=""):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")
        if not ok and fix:
            print(f"        fix: {fix}")

    print("PriceWatch doctor\n")
    step("Config file found", s.env_file is not None, str(s.env_file or ""),
         "cp .env.example ~/.config/pricewatch/pricewatch.env && chmod 600 ~/.config/pricewatch/pricewatch.env")
    step("Search switched on", s.search_provider == "searxng", f"PW_SEARCH_PROVIDER={s.search_provider}",
         "set PW_SEARCH_PROVIDER=searxng in ~/.config/pricewatch/pricewatch.env")
    step("AI matching switched on", s.llm_provider != "none", f"PW_LLM_PROVIDER={s.llm_provider}",
         "set PW_LLM_PROVIDER=ollama (optional: without it a strict heuristic is used)")

    if s.llm_provider == "ollama":
        try:
            with urllib.request.urlopen(f"{s.llm_base_url}/api/tags", timeout=5) as r:
                names = [m.get("name", "") for m in _json.loads(r.read().decode()).get("models", [])]
            step("Ollama running", True, s.llm_base_url)
            have = any(n == s.llm_model or n.split(":")[0] == s.llm_model or n == s.llm_model + ":latest"
                       for n in names)
            step(f"Model {s.llm_model} downloaded", have, ", ".join(names[:5]) or "no models",
                 f"ollama pull {s.llm_model}")
            if have:
                import time
                t = time.time()
                try:
                    out = LLM(s).json('Reply with exactly this JSON: {"ok": true}')
                    step("Model answers in JSON", out.get("ok") is True, f"{time.time() - t:.1f}s")
                except AgentError as e:
                    step("Model answers in JSON", False, str(e))
        except Exception as e:  # noqa: BLE001
            step("Ollama running", False, f"{type(e).__name__} at {s.llm_base_url}",
                 "open the Ollama app (or run: ollama serve)")

    if s.search_provider == "searxng":
        try:
            urls = searxng_search(s, "apple ipad mini", 5)
            step("SearXNG search works", bool(urls), f"{len(urls)} results from {s.searxng_url}",
                 "check the SearXNG engines; try again in a minute")
        except AgentError as e:
            step("SearXNG search works", False, str(e),
                 "open Docker Desktop, then: docker compose --profile search up -d searxng")

    b = find_browser(s.browser_path)
    step("Browser for blocked shops", bool(b), b or "none", "install Firefox or Chrome")
    ok = all(results)
    print(f"\n{'All good - try: python3 -m pricewatch find-better' if ok else 'Fix the FAIL lines above, then run doctor again.'}")
    return 0 if ok else 1


def find_better(s, item_id=None) -> int:
    """Run the v2 agent now, in the foreground, and print what it found."""
    import time

    from .agent import AgentError, PriceAgent
    from .util import domain_of, fmt_price

    if not s.db_path.exists():
        print("No database yet. Add products with: python3 -m pricewatch serve")
        return 1
    db = DB(s.db_path)
    agent = PriceAgent(s, db)
    if not agent.available:
        print(agent.status_text() + "\nRun: python3 -m pricewatch doctor")
        return 1
    items = ([db.get_item(item_id)] if item_id else
             db.q("SELECT * FROM items WHERE parent_id IS NULL AND active=1 ORDER BY id"))
    items = [i for i in items if i]
    if not items:
        print("No matching products.")
        return 1
    print(f"{agent.status_text()}\n")
    failed = 0
    for it in items:
        name = (it["title"] or domain_of(it["url"]))[:60]
        print(f"[{it['id']}] {name} - {fmt_price(it['current_price'], it['currency'])} at {domain_of(it['url'])}")
        t = time.time()
        try:
            summary = agent.run(it["id"])
            for line in summary.splitlines():
                print("    " + line)
        except AgentError as e:
            failed += 1
            print(f"    FAILED: {e}")
        alts = db.alternatives(it["id"])
        for a in alts:
            if a["review"] in ("suspect", "wrong"):
                print(f"    ⚠ please check {domain_of(a['url'])} at {fmt_price(a['current_price'], a['currency'])}: "
                      f"{a['review_note'] or 'flagged'}\n      {a['url']}\n"
                      f"      (confirm or correct it under 'Feedback for the AI' on the product page)")
        if alts:
            best = min((a for a in alts if a["current_price"] and a["review"] in ("ok", "confirmed")),
                       key=lambda a: float(a["current_price"]), default=None)
            if best:
                cheaper = it["current_price"] and float(best["current_price"]) < float(it["current_price"])
                print(f"    best elsewhere: {fmt_price(best['current_price'], best['currency'])} at "
                      f"{domain_of(best['url'])}{'  <- CHEAPER' if cheaper else ''}")
                print(f"    link: {best['url']}")
        print(f"    ({time.time() - t:.0f}s)\n")
    return 0 if not failed else 1


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
