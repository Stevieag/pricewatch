#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Regenerate the README screenshots from DEMO data (never your own watchlist).

    python3 docs/screenshots/make_screenshots.py

Needs Google Chrome or Chromium. Uses a throw-away database in a temp folder and a random
port; nothing is fetched from the internet. Apart from GTA VI (real prices seen at Amazon
and ShopTo), every product and shop here is made up and uses `.example` domains.
"""
from __future__ import annotations

import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pricewatch import fair  # noqa: E402
from pricewatch.agent import PriceAgent  # noqa: E402
from pricewatch.config import load_settings  # noqa: E402
from pricewatch.db import DB, now  # noqa: E402
from pricewatch.fetcher import find_browser  # noqa: E402
from pricewatch.web import serve  # noqa: E402

OUT = Path(__file__).resolve().parent
DAY = 86400

# (title, url, current, first, alert, history days, [alternatives], wobble)
# alternative: (title, url, price, review, note)
PRODUCTS = [
    ("Grand Theft Auto VI Standard Edition - Xbox Series X|S",
     "https://www.amazon.co.uk/dp/B0HRCCYQKL", "69.99", "74.99", "50", 45,
     [("Grand Theft Auto VI (GTA 6) - Xbox Series X/S", "https://www.shopto.net/en/xbxs-gta6/", "55.85", "confirmed", None),
      ("Grand Theft Auto VI - Xbox Series X", "https://www.game.co.uk/en/grand-theft-auto-vi-xbox", "64.99", "ok", None)], True),
    ("Cordless Drill Driver 18V with 2 Batteries", "https://www.toolhouse.example/p/drill-18v",
     "89.00", "89.00", "80", 30,
     [("18V Cordless Drill Driver, 2 x 2.0Ah", "https://www.diy-direct.example/drill-18v-2ah", "79.00", "ok", None)], False),
    ("Noise-Cancelling Wireless Headphones", "https://www.soundstore.example/anc-headphones",
     "249.00", "249.00", "200", 30, [], False),
    ("5.5L Digital Air Fryer", "https://www.kitchenhub.example/air-fryer-5-5l",
     "119.99", "109.99", "90", 30,
     [("Digital Air Fryer 5.5 Litre", "https://www.homedeals.example/air-fryer",
       "24.99", "suspect", "much lower than 119.99 elsewhere - could be a monthly, deposit, trade-in or used price")], False),
    ("Building Blocks Set, 500 Pieces", "https://www.toyland.example/blocks-500",
     "29.99", "34.99", "30", 20, [], False),
    ("Merino Wool Crew Neck Jumper", "https://www.knitwear.example/merino-crew",
     "65.00", "80.00", "50", 20, [], False),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed(db: DB) -> int:
    random.seed(7)
    t = now()
    gta_id = None
    with db.tx() as c:
        for order, (title, url, cur, first, alert, days, alts, wobble) in enumerate(PRODUCTS):
            iid = c.execute(
                """INSERT INTO items(url,title,currency,current_price,target_price,interval_minutes,
                   next_check_at,last_checked_at,extract_method,source,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (url, title, "GBP", cur, alert, 1440, t + 9 * 3600, t - 15 * 3600, "jsonld", "user",
                 t - order * 60)).lastrowid  # watchlist is newest-first: GTA VI on top
            gta_id = gta_id or iid
            price = Decimal(first)
            for d in range(days, -1, -1):
                if wobble and d in (31, 18, 9):
                    price = Decimal(random.choice(["72.99", "69.99", "74.99"]))
                if d == 0 or (d < days // 3 and price != Decimal(cur)):
                    price = Decimal(cur)
                c.execute("INSERT INTO prices(item_id,price,currency,checked_at) VALUES(?,?,?,?)",
                          (iid, str(price), "GBP", t - d * DAY - 15 * 3600))
            for a_title, a_url, a_price, review, note in alts:
                aid = c.execute(
                    """INSERT INTO items(parent_id,url,title,currency,current_price,interval_minutes,
                       next_check_at,last_checked_at,extract_method,source,created_at,review,review_note)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (iid, a_url, a_title, "GBP", a_price, 1440, t + 9 * 3600, t - 15 * 3600, "jsonld",
                     "agent", t - days * DAY, review, note)).lastrowid
                ap = Decimal(a_price) + Decimal("6")
                for d in range(days, -1, -1):
                    if d in (25, 12, 4):
                        ap = max(Decimal(a_price), ap - Decimal("2"))
                    c.execute("INSERT INTO prices(item_id,price,currency,checked_at) VALUES(?,?,?,?)",
                              (aid, str(ap if d else Decimal(a_price)), "GBP", t - d * DAY - 15 * 3600))
        # Fair Price for GTA VI: anonymous shoppers + the price "you" saw
        for persona in ("desktop-win-en", "mac-safari-en", "iphone-safari", "android-chrome", "linux-firefox-de"):
            c.execute("INSERT INTO fair_checks(item_id,persona,price,currency,checked_at,method,context) "
                      "VALUES(?,?,?,?,?,?,?)", (gta_id, persona, "69.99", "GBP", t - 3600, "jsonld",
                                                 "the shop's structured product data (schema.org offer)"))
        c.execute("INSERT INTO fair_checks(item_id,persona,price,currency,checked_at) VALUES(?,?,?,?,?)",
                  (gta_id, "you", "69.99", "GBP", t - 7200))
    return gta_id


def shoot(browser: str, url: str, out: Path, height: int, dark: bool) -> None:
    scheme = "0" if dark else "1"  # blink: 0 = dark, 1 = light
    cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           f"--window-size=1100,{height}", "--force-device-scale-factor=2",
           f"--blink-settings=preferredColorScheme={scheme}", f"--screenshot={out}", url]
    if dark:
        cmd.insert(1, "--force-dark-mode")
    if sys.platform.startswith("linux"):
        cmd.insert(1, "--no-sandbox")
    subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    print("wrote", out.relative_to(ROOT))


def main() -> int:
    browser = find_browser()
    if not browser or "firefox" in browser.lower():
        print("Needs Google Chrome or Chromium installed.")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        # search "on" so the page shows the normal search hint (nothing is actually searched)
        s = load_settings({"PW_DATA_DIR": tmp, "PW_PORT": str(free_port()),
                           "PW_SEARCH_PROVIDER": "searxng", "PW_SEARXNG_URL": "http://127.0.0.1:9",
                           "PW_LLM_PROVIDER": "ollama", "PW_LLM_MODEL": "gemma4:e2b"})
        db = DB(s.db_path)
        gta = seed(db)
        httpd = serve(s, db, PriceAgent(s, db), fair.FairRunner(s, db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.5)
        base = f"http://127.0.0.1:{s.port}"
        for dark in (False, True):
            mode = "dark" if dark else "light"
            shoot(browser, f"{base}/", OUT / f"watchlist-{mode}.png", 1105, dark)
            shoot(browser, f"{base}/items/{gta}?range=90d", OUT / f"product-{mode}.png", 1840, dark)
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
