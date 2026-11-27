# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Glossop Awnings (Shopify): the page data says £1,389.00, the lowest size, but the link's
variant (a bigger size) costs £1,979.00."""
import json
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pricewatch.config import load_settings
from pricewatch.monitor import fetch_price

PAGE = """<html><head><title>Dorema Monza XL300 Full Caravan Awning</title>
<meta property="og:title" content="Dorema Monza XL300 Full Caravan Awning">
<meta property="og:price:amount" content="1,389.00"><meta property="og:price:currency" content="GBP">
<link rel="preconnect" href="https://cdn.shopify.com"></head>
<body><div class="price">£1,979.00</div></body></html>"""
PRODUCT_JS = {"title": "Dorema Monza XL300 Full Caravan Awning", "variants": [
    {"id": 111, "title": "5 / Steel 28mm", "price": 138900, "available": False},
    {"id": 222, "title": "6 / Steel 28mm", "price": 145900, "available": True},
    {"id": 56081286234443, "title": "15 / Steel 28mm", "price": 197900, "available": True}]}

# what Shopify themes embed (ShopifyAnalytics.meta): prices in pence, variant name in public_title
OTHER = ('<script type="application/json">{"product":{"variants":[{"id":7,"price":99900},'
         '{"id":8,"price":109900},{"id":9,"price":119900},{"id":10,"price":129900}]}}</script>')
IN_PAGE = ('<script>window.ShopifyAnalytics = window.ShopifyAnalytics || {}; var meta = {"product":'
           '{"id":9,"vendor":"Dorema","variants":[{"id":111,"price":138900,"name":"Monza - 5",'
           '"public_title":"5 / Steel 28mm","sku":""},{"id":56081286234443,"price":197900,'
           '"name":"Monza - 15","public_title":"15 / Steel 28mm","sku":""}]},"page":{}};</script>')


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    js_hits = 0

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith(".js"):
            Shop.js_hits += 1
        body, ctype = {
            "/products/dorema-monza-xl300-full-caravan-awning": (PAGE, "text/html"),
            "/products/dorema-monza-xl300-full-caravan-awning.js": (json.dumps(PRODUCT_JS), "application/json"),
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
        ex = self.get("dorema-monza-xl300-full-caravan-awning?variant=56081286234443")
        self.assertEqual((ex.price, ex.currency, ex.method), (Decimal("1979"), "GBP", "shopify"))
        self.assertTrue(ex.reliable)
        self.assertIn("15 / Steel 28mm", ex.title)

    def test_no_variant_uses_first_available(self):
        ex = self.get("dorema-monza-xl300-full-caravan-awning")
        self.assertEqual(ex.price, Decimal("1459"))       # 5 is sold out; 6 is what the page shows

    def test_variants_read_from_the_page_without_a_second_request(self):
        Shop.js_hits = 0
        ex = self.get("in-page?variant=56081286234443")
        self.assertEqual((ex.price, ex.method), (Decimal("1979"), "shopify"))
        self.assertIn("15 / Steel 28mm", ex.title)
        self.assertEqual(Shop.js_hits, 0)
        self.assertEqual(self.get("in-page").price, Decimal("1389"))   # its own list, not a longer one

    def test_unknown_variant_and_non_shopify_unchanged(self):
        self.assertEqual(self.get("dorema-monza-xl300-full-caravan-awning?variant=999").price, Decimal("1389.00"))
        self.assertEqual(self.get("not-shopify?variant=56081286234443").method, "meta")


if __name__ == "__main__":
    unittest.main()
