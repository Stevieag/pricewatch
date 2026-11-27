# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Shopify: the page data shows the lowest variant's price, but the link's ?variant= (a bigger
size) costs more. Products and prices are made up."""
import json
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pricewatch.config import load_settings
from pricewatch.monitor import fetch_price

PAGE = """<html><head><title>Acme Gazebo G300</title>
<meta property="og:title" content="Acme Gazebo G300">
<meta property="og:price:amount" content="289.00"><meta property="og:price:currency" content="GBP">
<link rel="preconnect" href="https://cdn.shopify.com"></head>
<body><div class="price">£579.00</div></body></html>"""
PRODUCT_JS = {"title": "Acme Gazebo G300", "variants": [
    {"id": 111, "title": "5 / Steel 28mm", "price": 28900, "available": False},
    {"id": 222, "title": "6 / Steel 28mm", "price": 35900, "available": True},
    {"id": 1001, "title": "3 / Steel 28mm", "price": 57900, "available": True}]}

# what Shopify themes embed (ShopifyAnalytics.meta): prices in pence, variant name in public_title
OTHER = ('<script type="application/json">{"product":{"variants":[{"id":7,"price":99900},'
         '{"id":8,"price":109900},{"id":9,"price":119900},{"id":10,"price":129900}]}}</script>')
IN_PAGE = ('<script>window.ShopifyAnalytics = window.ShopifyAnalytics || {}; var meta = {"product":'
           '{"id":9,"vendor":"Acme","variants":[{"id":111,"price":28900,"name":"G300 - 5",'
           '"public_title":"5 / Steel 28mm","sku":""},{"id":1001,"price":57900,'
           '"name":"G300 - 3","public_title":"3 / Steel 28mm","sku":""}]},"page":{}};</script>')


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    js_hits = 0

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith(".js"):
            Shop.js_hits += 1
        body, ctype = {
            "/products/acme-gazebo-g300": (PAGE, "text/html"),
            "/products/acme-gazebo-g300.js": (json.dumps(PRODUCT_JS), "application/json"),
            "/products/in-page": (PAGE.replace("</body>", IN_PAGE + OTHER + "</body>"), "text/html"),
            "/products/not-shopify": (PAGE.replace("cdn.shopify.com", "cdn.example.com"), "text/html"),
        }.get(path, (None, None))
        if body is None:
            self.send_response(404); self.end_headers(); return
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class ShopifyVariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/products/"
        cls.s = load_settings({"PW_ALLOW_PRIVATE_HOSTS": "true", "PW_BROWSER_FETCH": "never"})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path):
        return fetch_price(self.s, self.base + path)[0]

    def test_linked_variant_price(self):
        ex = self.get("acme-gazebo-g300?variant=1001")
        self.assertEqual((ex.price, ex.currency, ex.method), (Decimal("579"), "GBP", "shopify"))
        self.assertEqual(str(ex.price), "579.00")
        self.assertTrue(ex.reliable)
        self.assertIn("3 / Steel 28mm", ex.title)

    def test_no_variant_uses_first_available(self):
        ex = self.get("acme-gazebo-g300")
        self.assertEqual(ex.price, Decimal("359"))       # 5 is sold out; 6 is what the page shows

    def test_variants_read_from_the_page_without_a_second_request(self):
        Shop.js_hits = 0
        ex = self.get("in-page?variant=1001")
        self.assertEqual((ex.price, ex.method), (Decimal("579"), "shopify"))
        self.assertIn("3 / Steel 28mm", ex.title)
        self.assertEqual(Shop.js_hits, 0)
        self.assertEqual(self.get("in-page").price, Decimal("289"))   # its own list, not a longer one

    def test_unknown_variant_and_non_shopify_unchanged(self):
        self.assertEqual(self.get("acme-gazebo-g300?variant=999").price, Decimal("289.00"))
        self.assertEqual(self.get("not-shopify?variant=1001").method, "meta")


if __name__ == "__main__":
    unittest.main()


class FairPriceUsesTheLinkedVariant(unittest.TestCase):
    """Fair Price must compare the same variant: otherwise it reports the shop's lowest
    variant as everyone else's price."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.s = load_settings({"PW_ALLOW_PRIVATE_HOSTS": "true", "PW_BROWSER_FETCH": "never",
                               "PW_DATA_DIR": cls.tmp.name})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.tmp.cleanup()

    def test_personas_and_baseline(self):
        from unittest import mock
        from pricewatch import fair
        from pricewatch.db import DB, now
        db = DB(self.s.db_path)
        url = (f"http://127.0.0.1:{self.srv.server_address[1]}/products/in-page?variant=1001")
        iid = db.add_item(url=url, title="Gazebo", price="579.00", currency="GBP", interval_minutes=1440)
        with db.tx() as c:   # what the old code recorded: the lowest variant
            c.execute("INSERT INTO fair_checks(item_id,persona,price,currency,checked_at,method) "
                      "VALUES(?,?,?,?,?,?)", (iid, "win-chrome", "289.00", "GBP", now() - 3600, "meta"))
        with mock.patch.object(fair.time, "sleep"):
            self.assertGreater(fair.run_personas(self.s, db, iid), 0)
        rows = db.q("SELECT price, method FROM fair_checks WHERE item_id=? AND method='shopify'", (iid,))
        self.assertTrue(rows)
        self.assertTrue(all(r["price"] == "579.00" for r in rows))
        rep = fair.report(self.s, db, iid)
        self.assertEqual((rep.baseline, rep.fair_price), (Decimal("579.00"), Decimal("579.00")))
