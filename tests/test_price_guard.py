# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""The Pixel 11 Pro XL case: Amazon showed '1-year Accidental Damage insurance £115.99'
next to the £1,139.99 phone, and PriceWatch recorded £115.99."""
import tempfile
import threading
import unittest
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

from pricewatch.agent import PriceAgent
from pricewatch.config import load_settings
from pricewatch.db import DB
from pricewatch.extract import extract
from pricewatch.monitor import check_item
from pricewatch.web import serve

AMAZON_LIKE = """<title>Google Pixel 11 Pro XL 1TB : Amazon.co.uk</title>
<div id="attach-accessory"><span class="a-price">£115.99</span></div>
<p>Add a protection plan: 1-year Accidental Damage insurance £115.99 Please read the IPID</p>
<div id="corePrice_feature_div"><span class="a-price priceToPay"><span>£1,139.99</span></span></div>
<span>or £115.99/mo for 10 months</span>"""


class Extraction(unittest.TestCase):
    def test_main_price_beats_insurance_and_instalments(self):
        ex = extract(AMAZON_LIKE)
        self.assertEqual(ex.price, Decimal("1139.99"))

    def test_text_only_addons_are_skipped(self):
        for html, want in [
            ("<p>Add 1-year Accidental Damage insurance £115.99 Please read</p><h2>£1,139.99</h2>", "1139.99"),
            ("<p>Protection plan £49.00</p><p>Price £399.00</p>", "399.00"),
            ("<p>AppleCare+ for iPhone £199</p><p>Our price £999</p>", "999"),
            ("<p>or £115.99/mo for 10 months</p><p>£1,139.99</p>", "1139.99"),
            ("<p>Price match guarantee £549.00</p>", "549.00"),   # "guarantee" alone isn't an add-on
        ]:
            self.assertEqual(extract(html).price, Decimal(want), html)


PAGES = {"/text-only": "<title>Phone</title><p>Today only £115.99</p>",
         "/structured": '<script type="application/ld+json">{"@type":"Product","name":"Phone",'
                        '"offers":{"price":"115.99","priceCurrency":"GBP"}}</script>'}


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = PAGES.get(self.path.split("?")[0], "").encode()
        self.send_response(200 if b else 404)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class HoldDoubtfulPrice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shop = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.shop.serve_forever, daemon=True).start()
        cls.p = cls.shop.server_address[1]
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

    def add(self, path):
        return self.db.add_item(url=f"http://127.0.0.1:{self.p}{path}?n={path}", title="Phone",
                                price="1139.99", currency="GBP", interval_minutes=1440)

    def post(self, path):
        data = urlencode({"csrf": self.httpd.app.csrf_token()}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data), timeout=10) as r:
            return r.read().decode()

    def test_page_text_drop_is_held_then_kept_or_accepted(self):
        iid = self.add("/text-only")
        check_item(self.s, self.db, self.db.get_item(iid))
        it = self.db.get_item(iid)
        self.assertEqual((it["current_price"], it["pending_price"], it["review"]), ("1139.99", "115.99", "suspect"))
        self.assertIn("Today only £115.99", it["review_note"])
        self.assertEqual(len(self.db.history(iid)), 1)              # the doubtful reading wasn't recorded
        with urllib.request.urlopen(f"{self.base}/items/{iid}", timeout=10) as r:
            page = r.read().decode()
        self.assertIn("Check this price", page)
        self.assertIn("Use £115.99", page)
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as r:
            self.assertIn("⚠ check price", r.read().decode())

        self.post(f"/items/{iid}/keep-price")
        it = self.db.get_item(iid)
        self.assertEqual((it["current_price"], it["pending_price"], it["review"]), ("1139.99", None, "ok"))

        check_item(self.s, self.db, self.db.get_item(iid))           # held again ...
        self.post(f"/items/{iid}/accept-price")                       # ... and this time accepted
        it = self.db.get_item(iid)
        self.assertEqual((it["current_price"], it["pending_price"]), ("115.99", None))

    def test_shop_structured_data_is_trusted(self):
        iid = self.add("/structured")                                  # a real 90% drop in shop data
        check_item(self.s, self.db, self.db.get_item(iid))
        it = self.db.get_item(iid)
        self.assertEqual((it["current_price"], it["pending_price"]), ("115.99", None))


if __name__ == "__main__":
    unittest.main()
