# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Some shops block automatic checks (HTTP 403 even for the browser fallback).
You can still watch them by typing the price you see."""
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode

from pricewatch.agent import PriceAgent
from pricewatch.config import load_settings
from pricewatch.db import DB
from pricewatch.monitor import check_item
from pricewatch.web import _title_from_url, serve


class Blocked(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        Blocked.hits += 1
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ManualPrice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shop = ThreadingHTTPServer(("127.0.0.1", 0), Blocked)
        threading.Thread(target=cls.shop.serve_forever, daemon=True).start()
        cls.shop_url = (f"http://127.0.0.1:{cls.shop.server_address[1]}"
                        "/products/samsung-galaxy-s26-ultra-1-tb-titanium-10307505.html")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.s = load_settings({"PW_DATA_DIR": cls.tmp.name, "PW_ALLOW_PRIVATE_HOSTS": "true",
                               "PW_BROWSER_FETCH": "never", "PW_PORT": "0"})
        cls.db = DB(cls.s.db_path)
        cls.httpd = serve(cls.s, cls.db, PriceAgent(cls.s, cls.db))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.shop.shutdown(); cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return r.read().decode()

    def post(self, path, **fields):
        fields["csrf"] = self.httpd.app.csrf_token()
        req = urllib.request.Request(self.base + path, data=urlencode(fields).encode())
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode()

    def test_title_from_url(self):
        self.assertEqual(_title_from_url(self.shop_url), "Samsung Galaxy S26 Ultra 1 Tb Titanium")

    def test_blocked_link_offers_typing_then_compares(self):
        phone = self.db.add_item(url="https://shop.example/galaxy", title="Galaxy S26 Ultra",
                                 price="999.99", currency="GBP", interval_minutes=1440)
        page = self.get("/add?text=" + quote(self.shop_url))
        self.assertIn("Couldn&#x27;t read that page", page)
        self.assertIn("Type the price yourself", page)

        page = self.post("/items/manual", url=self.shop_url, title="Phone at the blocked shop",
                         price="249.00", currency="GBP", parent=str(phone))
        self.assertIn("Saved £249.00", page)
        alt = self.db.alternatives(phone)[0]
        self.assertEqual((alt["current_price"], alt["manual"], alt["review"]), ("249.00", 1, "confirmed"))
        self.assertIn("price typed by you", page)
        self.assertIn(">Update<", page)
        self.assertIn("£249.00", self.get("/"))           # counts as best elsewhere

        # never fetched automatically
        hits = Blocked.hits
        self.db.refresh_all()
        self.assertNotIn(alt["id"], [i["id"] for i in self.db.due_items()])
        check_item(self.s, self.db, self.db.get_item(alt["id"]))
        self.assertEqual(Blocked.hits, hits)

        self.post(f"/items/{alt['id']}/set-price", price="999")
        self.assertEqual(self.db.get_item(alt["id"])["current_price"], "999")
        self.assertEqual(len(self.db.history(alt["id"])), 2)

    def test_own_product_and_bad_input(self):
        url = self.shop_url + "?own=1"
        page = self.post("/items/manual", url=url, title="Phone", price="abc", currency="GBP")
        self.assertIn("Enter the price as a number", page)
        page = self.post("/items/manual", url=url, title="Phone", price="1149", currency="GBP", parent="")
        iid = int(re.search(r'action="/items/(\d+)/set-price"', page).group(1))
        self.assertIn("not checked automatically", page)
        self.assertNotIn(">Check now<", page)
        self.assertIn("typed by you", self.get("/"))
        with self.assertRaises(urllib.error.HTTPError):       # CSRF protected
            urllib.request.urlopen(urllib.request.Request(
                f"{self.base}/items/{iid}/set-price", data=b"price=1"), timeout=10)


if __name__ == "__main__":
    unittest.main()
