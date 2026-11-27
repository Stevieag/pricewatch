# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Phones: the agent kept adding network contract deals and refurbished listings next to a
new, SIM-free Pixel 11 Pro XL."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from pricewatch.agent import PriceAgent
from pricewatch.config import load_settings
from pricewatch.db import DB
from pricewatch.monitor import check_item
from pricewatch.review import is_phone, offer_mismatch

PIXEL = "Google Pixel 11 Pro XL 1TB Obsidian"
AMAZON = "https://www.amazon.co.uk/dp/B0H62VGVN1"


class Rules(unittest.TestCase):
    def test_is_phone(self):
        for t in (PIXEL, "Apple iPhone 18 Pro 256GB", "Samsung Galaxy S26 Ultra", "Nothing Phone (4)"):
            self.assertTrue(is_phone(t), t)
        for t in ("BLACK+DECKER 1200W Hover Mower", "Grand Theft Auto VI Xbox"):
            self.assertFalse(is_phone(t), t)

    def test_offer_mismatch(self):
        bad = [("Google Pixel 11 Pro XL 1TB - Renewed", "https://shop.example/p/1"),
               ("Pixel 11 Pro XL 1TB", "https://www.backmarket.co.uk/p/pixel-11-pro-xl-refurbished"),
               ("Pixel 11 Pro XL 1TB Grade B", "https://shop.example/p/2"),
               ("Pixel 11 Pro XL 1TB 100GB data, 24 months", "https://www.ee.co.uk/p"),
               ("Google Pixel 11 Pro XL", "https://www.vodafone.co.uk/phones/pay-monthly-contracts/pixel"),
               ("Pixel 11 Pro XL with EE", "https://shop.example/p/3")]
        good = [("Google Pixel 11 Pro XL 1TB SIM Free", "https://www.vodafone.co.uk/phones/sim-free/pixel"),
                ("Google Pixel 11 Pro XL 1TB Obsidian Unlocked", "https://www.johnlewis.com/p/123"),
                ("Pixel 11 Pro XL 1TB", "https://www.o2.co.uk/shop/google/pixel?paymentTab=pay-in-full")]
        for t, u in bad:
            self.assertIsNotNone(offer_mismatch(PIXEL, AMAZON, t, u), t)
        for t, u in good:
            self.assertIsNone(offer_mismatch(PIXEL, AMAZON, t, u), t)
        # your own listing is refurbished or on contract: like-for-like is fine
        self.assertIsNone(offer_mismatch("Pixel 11 Pro XL refurbished", "", "Pixel 11 Pro XL renewed", ""))
        self.assertIsNone(offer_mismatch("Pixel 11 Pro XL on contract", "", "Pixel 11 Pro XL 24 month contract", ""))
        # not a phone: "contract" words don't matter, condition still does
        self.assertIsNone(offer_mismatch("Hover mower", "", "Hover mower, 2 year contract cover", ""))
        self.assertIsNotNone(offer_mismatch("Hover mower", "", "Hover mower (used)", ""))


def page(title, price):
    return (f'<title>{title}</title><script type="application/ld+json">{{"@type":"Product",'
            f'"name":"{title}","offers":{{"price":"{price}","priceCurrency":"GBP"}}}}</script>')


class Shops(BaseHTTPRequestHandler):
    queries = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlsplit(self.path)
        loc = f"http://localhost:{self.server.server_address[1]}"
        if u.path == "/search":
            Shops.queries.append(parse_qs(u.query)["q"][0])
            body = json.dumps({"results": [{"url": f"{loc}{p}"} for p in (
                "/phones/pay-monthly/pixel-11-pro-xl",   # contract, by URL: skipped before fetching
                "/p/48151",                               # renewed, by title after fetching
                "/deal/pixel-ee",                         # contract, by title
                "/pixel-11-pro-xl-sim-free")]})           # the one we want
        else:
            body = {"/phones/pay-monthly/pixel-11-pro-xl": page("Google Pixel 11 Pro XL 1TB Obsidian", "30.00"),
                    "/p/48151": page("Google Pixel 11 Pro XL 1TB Obsidian (Renewed)", "799.00"),
                    "/deal/pixel-ee": page("Google Pixel 11 Pro XL 1TB Obsidian 100GB data EE", "49.00"),
                    "/pixel-11-pro-xl-sim-free": page("Google Pixel 11 Pro XL 1TB Obsidian SIM Free", "1099.00"),
                    }.get(u.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if body.startswith("{") else "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class Agent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Shops)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.p = cls.srv.server_address[1]
        cls.tmp = tempfile.TemporaryDirectory()
        cls.s = load_settings({"PW_DATA_DIR": cls.tmp.name, "PW_ALLOW_PRIVATE_HOSTS": "true",
                               "PW_SEARCH_PROVIDER": "searxng", "PW_SEARXNG_URL": f"http://127.0.0.1:{cls.p}",
                               "PW_BROWSER_FETCH": "never"})
        cls.db = DB(cls.s.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.tmp.cleanup()

    def test_only_the_sim_free_phone_is_added(self):
        iid = self.db.add_item(url=f"http://127.0.0.1:{self.p}/orig", title=PIXEL, price="1139.99",
                               currency="GBP", interval_minutes=1440)
        out = PriceAgent(self.s, self.db).run(iid)
        self.assertTrue(all("SIM free" in q for q in Shops.queries), Shops.queries)
        self.assertIn("skipped - looks like a network contract deal (pay monthly)", out)
        self.assertIn("not added - looks renewed, not new", out)
        self.assertIn("not added - looks like a network contract deal (100gb data)", out)
        alts = self.db.alternatives(iid)
        self.assertEqual([(a["current_price"], a["review"]) for a in alts], [("1099.00", "ok")])
        skipped = {k["reason"] for k in self.db.skipped_for(iid)}   # still offered with "Add anyway"
        self.assertIn("looks renewed, not new", skipped)

    def test_existing_refurb_is_flagged_on_next_check(self):
        iid = self.db.add_item(url=f"http://127.0.0.1:{self.p}/orig?2", title=PIXEL, price="1139.99",
                               currency="GBP", interval_minutes=1440)
        alt = self.db.add_item(url=f"http://localhost:{self.p}/p/48151", title=None, price="799.00",
                               currency="GBP", interval_minutes=1440, parent_id=iid, source="agent")
        check_item(self.s, self.db, self.db.get_item(alt))
        a = self.db.get_item(alt)
        self.assertEqual(a["review"], "suspect")
        self.assertIn("renewed", a["review_note"])
        top = [r for r in self.db.top_items() if r["id"] == iid][0]
        self.assertIsNone(top["best_alt_price"])          # no longer shown as cheaper


if __name__ == "__main__":
    unittest.main()
