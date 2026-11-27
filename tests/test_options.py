# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Products with options (size, frame, colour). The agent added Towsure, Camping Online and
Standart Caravans as 'cheaper' for a size 15 / Steel 28 mm Dorema Monza XL300, but read each
shop's default option (size 5). Grantham sells the 28 mm frame as a paid extra."""
import html
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pricewatch.config import load_settings
from pricewatch.db import DB
from pricewatch.monitor import check_item, fetch_price, option_hint, strip_option_hint

MINE = "Dorema Monza XL300 Full Caravan Awning (FREE SafeLock Kit) (15 (1000-1025) / Steel 28 mm)"
HINT = "15 (1000-1025) / Steel 28 mm"

SHOPIFY = ('<title>Dorema Monza XL300</title><link href="https://cdn.shopify.com/x.css">'
           '<meta property="og:price:amount" content="1459.00"><meta property="og:price:currency" content="GBP">'
           '<script>var meta = {"product":{"variants":['
           '{"id":1,"price":145900,"public_title":"Size 5 (750-775) / Steel EasyGrip"},'
           '{"id":2,"price":154900,"public_title":"Size 5 (750-775) / Steel 28mm"},'
           '{"id":3,"price":184900,"public_title":"Size 15 (1000-1025) / Steel EasyGrip"},'
           '{"id":4,"price":197900,"public_title":"Size 15 (1000-1025) / Steel 28mm"},'
           '{"id":5,"price":211900,"public_title":"Size 15 (1000-1025) / Fibre"}]}};</script>')
WOO_VARS = [{"attributes": {"attribute_pa_size": "5-750-775cm"}, "display_price": 1459, "is_in_stock": True},
            {"attributes": {"attribute_pa_size": "15-1000-1025cm"}, "display_price": 1939, "is_in_stock": True}]
WOO = ('<title>Dorema - Monza XL300 Full Awning</title>'
       '<script type="application/ld+json">{"@type":"Product","name":"Monza XL300","offers":'
       '{"lowPrice":"1459.00","price":"1459.00","priceCurrency":"GBP"}}</script>'
       f'<form class="variations_form" data-product_variations="{html.escape(json.dumps(WOO_VARS))}"></form>'
       '<label>28mm Frame (5-14) <span>(+&pound;90.00)</span></label>'
       '<label>28mm Frame (15-19) <span>(+&pound;130.00)</span></label>'
       '<label>FibreTech Frame (15-19) Touring Only (+&pound;290.00)</label>'
       '<label>Starlon Carpet (12-15) (+&pound;144.00)</label>')
MAGENTO = ('<title>Dometic Stuga Rest (Poled) Full Awning 2026</title><script type="application/ld+json">'
           + json.dumps({"@type": "Product", "name": "Dometic Stuga Rest (Poled) Full Awning 2026",
                         "offers": {"@type": "AggregateOffer", "lowPrice": "1799.00", "offers": [
                             {"@type": "Offer", "price": f"{1799 + 50 * i}.00", "priceCurrency": "GBP",
                              "availability": "https://schema.org/" + ("InStock" if i in (6, 7) else "OutOfStock"),
                              "sku": f"ATTE2023-Size {9 + i} ({840 + 25 * i}-{865 + 25 * i}cm)"}
                             for i in range(11)]}}) + "</script>")
WOO_AJAX = WOO.replace(html.escape(json.dumps(WOO_VARS)), "false")
NO_SIZE = SHOPIFY.replace('"Size 15 (1000-1025) / Steel 28mm"', '"Size 14 (975-1000) / Steel 28mm"')


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = {"/products/monza": SHOPIFY, "/product/monza-xl300/": WOO, "/products/small": NO_SIZE, "/product/ajax/": WOO_AJAX, "/dometic-stuga-rest-full-awning": MAGENTO,
                "/products/monza.js": None}.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404); self.end_headers(); return
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class YourOptionEverywhere(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.s = load_settings({"PW_ALLOW_PRIVATE_HOSTS": "true", "PW_BROWSER_FETCH": "never",
                               "PW_DATA_DIR": cls.tmp.name})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.tmp.cleanup()

    def test_hint_from_title(self):
        self.assertEqual(option_hint(MINE), HINT)
        self.assertEqual(strip_option_hint(MINE), "Dorema Monza XL300 Full Caravan Awning (FREE SafeLock Kit)")
        self.assertIsNone(option_hint("Black and Decker Mower (Corded)"))
        self.assertIsNone(option_hint("Grand Theft Auto VI (Xbox / PS5)"))

    def test_shopify_picks_your_size_and_frame(self):
        ex, url = fetch_price(self.s, self.base + "/products/monza", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("1979.00"), "shopify"))
        self.assertTrue(url.endswith("?variant=4"))
        self.assertIn("Size 15 (1000-1025) / Steel 28mm", ex.title)

    def test_woocommerce_size_plus_frame_extra(self):
        ex, url = fetch_price(self.s, self.base + "/product/monza-xl300/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("2069.00"), "woocommerce"))   # £1,939 + £130
        self.assertIn("attribute_pa_size=15-1000-1025cm", url)
        self.assertIn("28mm Frame", ex.title)

    def test_per_size_offers_in_shop_data(self):   # Attwoolls showed £1,799 (size 9)
        ex, _ = fetch_price(self.s, self.base + "/dometic-stuga-rest-full-awning", "16 (1015-1040cm)")
        self.assertEqual((ex.price, ex.method, ex.in_stock), (Decimal("2149.00"), "jsonld", True))
        self.assertIn("Size 16 (1015-1040cm)", ex.title)

    def test_woocommerce_options_loaded_separately(self):   # crashed: 'bool' is not iterable
        ex, _ = fetch_price(self.s, self.base + "/product/ajax/", HINT)
        self.assertIsNone(ex.price)
        self.assertIn("loads its options separately", ex.context)

    def test_shop_without_your_option_reports_no_price(self):
        ex, _ = fetch_price(self.s, self.base + "/products/small", HINT)
        self.assertIsNone(ex.price)
        self.assertIn("no '15 (1000-1025) / Steel 28 mm' option", ex.context)

    def test_rechecks_of_another_shop_use_your_choice(self):
        db = DB(self.s.db_path)
        top = db.add_item(url="https://www.glossopawnings.com/products/x?variant=9", title=MINE,
                          price="1979.00", currency="GBP", interval_minutes=1440)
        alt = db.add_item(url=self.base + "/product/monza-xl300/", title="Monza XL300 (5 (750-775cm))", price="1459.00",
                          currency="GBP", interval_minutes=1440, parent_id=top, source="agent")
        check_item(self.s, db, db.get_item(alt))
        self.assertEqual(db.get_item(alt)["current_price"], "2069.00")
        self.assertNotIn("750-775", db.get_item(alt)["title"])
        self.assertIn("28mm Frame", db.get_item(alt)["title"])


if __name__ == "__main__":
    unittest.main()
