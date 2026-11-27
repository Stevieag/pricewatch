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
GRANTHAM = ('<title>Isabella - Ventura Pacific Full Awning</title>'
            '<script type="application/ld+json">{"@type":"Product","name":"Ventura Pacific","offers":'
            '{"lowPrice":"1645.00","price":"1645.00","priceCurrency":"GBP"}}</script>'
            '<form class="variations_form cart" data-product_id="77" data-product_variations="false">'
            '<select name="attribute_size"><option value="G19/1015 (1001-1025cm)">G19</option></select>'
            '<select name="attribute_depth"><option value="250cm">250</option><option value="300cm">300</option></select>'
            '</form>')
CAMPING_ONLINE = ('<title>Dorema Monza XL300 Touring Seasonal Awning 28mm Steel Frame</title>'
                  '<script type="application/ld+json">{"@type":"Product","name":"Dorema Monza XL300 Touring '
                  'Seasonal Awning 28mm Steel Frame","offers":{"price":"1479.00","priceCurrency":"GBP"}}</script>'
                  '<form class="cart"><select name="wapf[field_1]"><option value="">Choose an option</option>'
                  '<option value="a" data-wapf-label="size 5-750-775" data-wapf-pricetype="fixed">size 5-750-775</option>'
                  '<option value="b" data-wapf-label="size 14-975-1000" data-wapf-pricetype="fixed" data-wapf-price="410">'
                  'size 14-975-1000 <span class="wapf-pricing-hint">(+&pound;410.00)</span></option>'
                  '<option value="c" data-wapf-label="size 15-1000-1025" data-wapf-pricetype="fixed" data-wapf-price="500">'
                  'size 15-1000-1025 <span class="wapf-pricing-hint">(+&pound;500.00)</span></option></select>'
                  '<select name="wapf[field_2]"><option data-wapf-label="No">No</option>'
                  '<option data-wapf-label="Yes" data-wapf-price="589">Yes (+£589.00)</option></select></form>')
STANDART = ('<title>Dorema Monza XL300 | FREE 28mm Frame Upgrade | 300cm Depth</title>'
            '<script type="application/ld+json">{"@type":"Product","name":"Dorema Monza XL300","offers":'
            '{"price":"1699.00","priceCurrency":"GBP"}}</script>'
            '<form class="variations_form cart" data-product_id="88" data-product_variations="false">'
            '<select name="attribute_pa_monza-xl300"><option value="">Choose</option>'
            '<option value="size-5-750-775-cm">5</option><option value="size-15-1000-1025-cm">15</option></select>'
            '<select name="attribute_pa_frame_options"><option value=""></option>'
            '<option value="25mm-easygrip-steel">25</option><option value="28mm-easygrip-steel">28</option>'
            '<option value="fibretech-plus">fibre</option></select></form>')
STANDART_FREE = STANDART.replace('</form>', '</form><div class="woobt-product woobt-product-together" data-id="9" '
                                 'data-name="FREE 28mm Steel Frame Upgrade with Dorema Monza Awning Range" '
                                 'data-new-price="100%" data-price="0" data-regular-price="0"></div>')
AWNING_ACE = ('<title>2026 Camptech Savanna DL</title><script type="application/ld+json">'
              '{"@context":"https://schema.org","@type":"ProductGroup","name":"Camptech Savanna DL",'
              '"variesBy":["https://schema.org/size"],"hasVariant":['
              '{"@type":"Product","url":"https://x/SL-06.html","sku":"SL-06","size":"(Size 6): 775-800cm",'
              '"offers":{"@type":"Offer","price":"837.00","priceCurrency":"GBP","availability":"https://schema.org/InStock"}},'
              '{"@type":"Product","url":"https://x/SL-15.html","sku":"SL-15","size":"(Size 15): 1000-1025cm",'
              '"offers":{"@type":"Offer","price":"1035.00","priceCurrency":"GBP","availability":"https://schema.org/InStock"}},'
              '{"@type":"Product","url":"https://x/SL-19.html","sku":"SL-19","size":"(Size 19): 1100-1125cm",'
              '"offers":{"@type":"Offer","price":"1108.00","priceCurrency":"GBP","availability":"https://schema.org/InStock"}}]}'
              '</script><p>Was £758.00</p><select name="N3_"><option value="V2062_">(Size 6): 775-800cm: £837.00</option></select>')
NO_SIZE = SHOPIFY.replace('"Size 15 (1000-1025) / Steel 28mm"', '"Size 14 (975-1000) / Steel 28mm"')


class Shop(BaseHTTPRequestHandler):
    lookups = []

    def log_message(self, *a):
        pass

    def do_POST(self):   # WooCommerce's ?wc-ajax=get_variation
        from urllib.parse import parse_qs
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        Shop.lookups.append(form)
        prices = {("77", "G19/1015 (1001-1025cm)", "300cm"): 2239, ("77", "G19/1015 (1001-1025cm)", "250cm"): 1999,
                  ("88", "size-15-1000-1025-cm", "28mm-easygrip-steel"): 2069,
                  ("88", "size-15-1000-1025-cm", "25mm-easygrip-steel"): 1939}
        price = prices.get((form.get("product_id"),
                            form.get("attribute_size") or form.get("attribute_pa_monza-xl300"),
                            form.get("attribute_depth") or form.get("attribute_pa_frame_options")))
        b = json.dumps({"display_price": price, "is_in_stock": False} if price else False).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        body = {"/products/monza": SHOPIFY, "/product/monza-xl300/": WOO, "/products/small": NO_SIZE, "/product/standart-monza/": STANDART, "/product/standart-free/": STANDART_FREE, "/product/co-monza/": CAMPING_ONLINE, "/product/ventura-pacific/": GRANTHAM, "/product/ajax/": WOO_AJAX, "/dometic-stuga-rest-full-awning": MAGENTO, "/savanna.html": AWNING_ACE,
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

    def test_megaframe_is_not_plain_steel(self):   # picked "Steel" £3,224 for "Steel Megaframe" £3,678
        from pricewatch.monitor import _option_score
        mega = "1015 (1001-1025cm) / Steel Megaframe"
        self.assertIsNone(_option_score(mega, "1015 (1001-1025cm) / Steel"))
        self.assertEqual(_option_score(mega, "1015 (1001-1025cm) / Steel Megaframe"), 2)
        self.assertIsNone(_option_score(HINT, "15 (1000-1025) / Steel EasyGrip"))
        self.assertEqual(_option_score(HINT, "Size 15 / 28mm Frame"), 2)

    def test_woocommerce_link_with_your_options(self):   # Grantham read £1,645 (cheapest), not £2,239
        from urllib.parse import urlencode
        url = (self.base + "/product/ventura-pacific/?" +
               urlencode({"attribute_size": "G19/1015 (1001-1025cm)", "attribute_depth": "300cm"}))
        ex, final = fetch_price(self.s, url)
        self.assertEqual((ex.price, ex.method), (Decimal("2239.00"), "woocommerce"))
        self.assertEqual(Shop.lookups[-1]["product_id"], "77")
        self.assertIn("G19/1015 (1001-1025cm) / 300cm", ex.title)
        self.assertEqual(final, url)
        gone = url.replace("300cm", "400cm")
        self.assertIsNone(fetch_price(self.s, gone)[0].price)   # option no longer sold: no guess

    def test_size_sold_as_a_paid_extra(self):   # Camping Online showed £1,479 (size 5) as "£500 cheaper"
        ex, _ = fetch_price(self.s, self.base + "/product/co-monza/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("1979.00"), "jsonld"))
        self.assertIn("size 15-1000-1025", ex.title)
        self.assertIn("1479.00 + extras 500.00", ex.context)
        ex, _ = fetch_price(self.s, self.base + "/product/co-monza/", "19 (1100-1125) / Steel 28 mm")
        self.assertIsNone(ex.price)                          # size not offered: no guess

    def test_woocommerce_options_picked_for_you(self):   # Standart: £1,699 default, £2,069 for yours
        ex, url = fetch_price(self.s, self.base + "/product/standart-monza/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("2069.00"), "woocommerce"))
        self.assertIn("attribute_pa_frame_options=28mm-easygrip-steel", url)   # re-checks ask the same
        self.assertIn("size-15-1000-1025-cm", url)
        self.assertEqual(fetch_price(self.s, url)[0].price, Decimal("2069.00"))

    def test_free_frame_upgrade_prices_the_base_frame(self):   # Standart: 28 mm free → £1,939
        ex, url = fetch_price(self.s, self.base + "/product/standart-free/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("1939.00"), "woocommerce"))
        self.assertIn("attribute_pa_frame_options=25mm-easygrip-steel", url)
        self.assertIn("FREE 28mm Steel Frame Upgrade", ex.context)
        ex, _ = fetch_price(self.s, self.base + "/product/standart-free/", "15 (1000-1025) / Mega")
        self.assertIsNone(ex.price)                     # a free 28 mm steel upgrade is not Megaframe

    def test_product_group_variants_by_size(self):   # Awning Ace: size on the variant, price on its offer
        ex, url = fetch_price(self.s, self.base + "/savanna.html", "19 (1100-1125) / Steel")
        self.assertEqual((ex.price, ex.method), (Decimal("1108.00"), "jsonld"))
        self.assertTrue(url.endswith("/savanna.html"))
        ex, _ = fetch_price(self.s, self.base + "/savanna.html", "15 (1000-1025) / Steel")
        self.assertEqual(ex.price, Decimal("1035.00"))

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
