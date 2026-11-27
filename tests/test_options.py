# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Products with options (size, frame, colour), one fake shop per platform type: Shopify
variants, WooCommerce (embedded, loaded separately, paid add-ons, bundled free upgrades),
Magento/schema.org offers and ProductGroup variants. Each must be priced at the option you
chose, never at the shop's default or cheapest option. Products and prices are made up."""
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

MINE = "Acme Gazebo G300 (FREE Peg Kit) (3 (300-325) / Steel 28 mm)"
HINT = "3 (300-325) / Steel 28 mm"

SHOPIFY = ('<title>Acme Gazebo G300</title><link href="https://cdn.shopify.com/x.css">'
           '<meta property="og:price:amount" content="359.00"><meta property="og:price:currency" content="GBP">'
           '<script>var meta = {"product":{"variants":['
           '{"id":1,"price":35900,"public_title":"Size 1 (200-225) / Steel EasyGrip"},'
           '{"id":2,"price":44900,"public_title":"Size 1 (200-225) / Steel 28mm"},'
           '{"id":3,"price":48900,"public_title":"Size 3 (300-325) / Steel EasyGrip"},'
           '{"id":4,"price":57900,"public_title":"Size 3 (300-325) / Steel 28mm"},'
           '{"id":5,"price":61900,"public_title":"Size 3 (300-325) / Fibre"}]}};</script>')
WOO_VARS = [{"attributes": {"attribute_pa_size": "1-200-225cm"}, "display_price": 359, "is_in_stock": True},
            {"attributes": {"attribute_pa_size": "3-300-325cm"}, "display_price": 539, "is_in_stock": True}]
WOO = ('<title>Acme - Gazebo G300</title>'
       '<script type="application/ld+json">{"@type":"Product","name":"Gazebo G300","offers":'
       '{"lowPrice":"359.00","price":"359.00","priceCurrency":"GBP"}}</script>'
       f'<form class="variations_form" data-product_variations="{html.escape(json.dumps(WOO_VARS))}"></form>'
       '<label>28mm Frame (1-2) <span>(+&pound;90.00)</span></label>'
       '<label>28mm Frame (3-7) <span>(+&pound;130.00)</span></label>'
       '<label>FibreTech Frame (3-7) Touring Only (+&pound;290.00)</label>'
       '<label>Starlon Carpet (12-15) (+&pound;144.00)</label>')
MAGENTO = ('<title>Northwind Marquee 2026</title><script type="application/ld+json">'
           + json.dumps({"@type": "Product", "name": "Northwind Marquee 2026",
                         "offers": {"@type": "AggregateOffer", "lowPrice": "699.00", "offers": [
                             {"@type": "Offer", "price": f"{699 + 20 * i}.00", "priceCurrency": "GBP",
                              "availability": "https://schema.org/" + ("InStock" if i in (6, 7) else "OutOfStock"),
                              "sku": f"NWM2026-Size {1 + i} ({200 + 25 * i}-{225 + 25 * i}cm)"}
                             for i in range(11)]}}) + "</script>")
WOO_AJAX = WOO.replace(html.escape(json.dumps(WOO_VARS)), "false")
WOO_TWO_SELECTS = ('<title>Summit - Pavilion P2</title>'
            '<script type="application/ld+json">{"@type":"Product","name":"Pavilion P2","offers":'
            '{"lowPrice":"445.00","price":"445.00","priceCurrency":"GBP"}}</script>'
            '<form class="variations_form cart" data-product_id="77" data-product_variations="false">'
            '<select name="attribute_size"><option value="G19/G3 (301-325cm)">G19</option></select>'
            '<select name="attribute_depth"><option value="250cm">250</option><option value="300cm">300</option></select>'
            '</form>')
WAPF_ADDONS = ('<title>Acme Gazebo G300 28mm Steel Frame</title>'
                  '<script type="application/ld+json">{"@type":"Product","name":"Acme Gazebo G300 '
                  '28mm Steel Frame","offers":{"price":"379.00","priceCurrency":"GBP"}}</script>'
                  '<form class="cart"><select name="wapf[field_1]"><option value="">Choose an option</option>'
                  '<option value="a" data-wapf-label="size 1-200-225" data-wapf-pricetype="fixed">size 1-200-225</option>'
                  '<option value="b" data-wapf-label="size 2-275-300" data-wapf-pricetype="fixed" data-wapf-price="410">'
                  'size 2-275-300 <span class="wapf-pricing-hint">(+&pound;410.00)</span></option>'
                  '<option value="c" data-wapf-label="size 3-300-325" data-wapf-pricetype="fixed" data-wapf-price="200">'
                  'size 3-300-325 <span class="wapf-pricing-hint">(+&pound;200.00)</span></option></select>'
                  '<select name="wapf[field_2]"><option data-wapf-label="No">No</option>'
                  '<option data-wapf-label="Yes" data-wapf-price="589">Yes (+£589.00)</option></select></form>')
WOO_AJAX_FRAMES = ('<title>Acme Gazebo G300 | FREE 28mm Frame Upgrade</title>'
            '<script type="application/ld+json">{"@type":"Product","name":"Acme Gazebo G300","offers":'
            '{"price":"499.00","priceCurrency":"GBP"}}</script>'
            '<form class="variations_form cart" data-product_id="88" data-product_variations="false">'
            '<select name="attribute_pa_gazebo-g300"><option value="">Choose</option>'
            '<option value="size-1-200-225-cm">5</option><option value="size-3-300-325-cm">15</option></select>'
            '<select name="attribute_pa_frame_options"><option value=""></option>'
            '<option value="25mm-easygrip-steel">25</option><option value="28mm-easygrip-steel">28</option>'
            '<option value="fibretech-plus">fibre</option></select></form>')
WOO_FREE_UPGRADE = WOO_AJAX_FRAMES.replace('</form>', '</form><div class="woobt-product woobt-product-together" data-id="9" '
                                 'data-name="FREE 28mm Steel Frame Upgrade with Acme Gazebo Range" '
                                 'data-new-price="100%" data-price="0" data-regular-price="0"></div>')
PRODUCT_GROUP = ('<title>2026 Ridge Shelter DL</title><script type="application/ld+json">'
              '{"@context":"https://schema.org","@type":"ProductGroup","name":"Ridge Shelter DL",'
              '"variesBy":["https://schema.org/size"],"hasVariant":['
              '{"@type":"Product","url":"https://x/SL-1.html","sku":"SL-1","size":"(Size 1): 200-225cm",'
              '"offers":{"@type":"Offer","price":"237.00","priceCurrency":"GBP","availability":"https://schema.org/InStock"}},'
              '{"@type":"Product","url":"https://x/SL-3.html","sku":"SL-3","size":"(Size 3): 300-325cm",'
              '"offers":{"@type":"Offer","price":"435.00","priceCurrency":"GBP","availability":"https://schema.org/InStock"}},'
              '{"@type":"Product","url":"https://x/SL-7.html","sku":"SL-7","size":"(Size 7): 400-425cm",'
              '"offers":{"@type":"Offer","price":"508.00","priceCurrency":"GBP","availability":"https://schema.org/InStock"}}]}'
              '</script><p>Was £158.00</p><select name="N3_"><option value="V2062_">(Size 1): 200-225cm: £237.00</option></select>')
NO_SIZE = SHOPIFY.replace('"Size 3 (300-325) / Steel 28mm"', '"Size 2 (275-300) / Steel 28mm"')


class Shop(BaseHTTPRequestHandler):
    lookups = []

    def log_message(self, *a):
        pass

    def do_POST(self):   # WooCommerce's ?wc-ajax=get_variation
        from urllib.parse import parse_qs
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
        Shop.lookups.append(form)
        prices = {("77", "G19/G3 (301-325cm)", "300cm"): 739, ("77", "G19/G3 (301-325cm)", "250cm"): 699,
                  ("88", "size-3-300-325-cm", "28mm-easygrip-steel"): 669,
                  ("88", "size-3-300-325-cm", "25mm-easygrip-steel"): 539}
        price = prices.get((form.get("product_id"),
                            form.get("attribute_size") or form.get("attribute_pa_gazebo-g300"),
                            form.get("attribute_depth") or form.get("attribute_pa_frame_options")))
        b = json.dumps({"display_price": price, "is_in_stock": False} if price else False).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        body = {"/products/gazebo": SHOPIFY, "/product/gazebo-g300/": WOO, "/products/small": NO_SIZE, "/product/woo-ajax-frames/": WOO_AJAX_FRAMES, "/product/woo-free-upgrade/": WOO_FREE_UPGRADE, "/product/wapf-addons/": WAPF_ADDONS, "/product/pavilion-p2/": WOO_TWO_SELECTS, "/product/ajax/": WOO_AJAX, "/northwind-marquee": MAGENTO, "/ridge-shelter.html": PRODUCT_GROUP,
                "/products/gazebo.js": None}.get(self.path.split("?")[0])
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
        self.assertEqual(strip_option_hint(MINE), "Acme Gazebo G300 (FREE Peg Kit)")
        self.assertIsNone(option_hint("Black and Decker Mower (Corded)"))
        self.assertIsNone(option_hint("Grand Theft Auto VI (Xbox / PS5)"))

    def test_shopify_picks_your_size_and_frame(self):
        ex, url = fetch_price(self.s, self.base + "/products/gazebo", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("579.00"), "shopify"))
        self.assertTrue(url.endswith("?variant=4"))
        self.assertIn("Size 3 (300-325) / Steel 28mm", ex.title)

    def test_woocommerce_size_plus_frame_extra(self):
        ex, url = fetch_price(self.s, self.base + "/product/gazebo-g300/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("669.00"), "woocommerce"))   # base + paid frame extra
        self.assertIn("attribute_pa_size=3-300-325cm", url)
        self.assertIn("28mm Frame", ex.title)

    def test_per_size_offers_in_shop_data(self):   # Magento: AggregateOffer, one offer per size
        ex, _ = fetch_price(self.s, self.base + "/northwind-marquee", "8 (375-400cm)")
        self.assertEqual((ex.price, ex.method, ex.in_stock), (Decimal("839.00"), "jsonld", True))
        self.assertIn("Size 8 (375-400cm)", ex.title)

    def test_woocommerce_options_loaded_separately(self):   # crashed: 'bool' is not iterable
        ex, _ = fetch_price(self.s, self.base + "/product/ajax/", HINT)
        self.assertIsNone(ex.price)
        self.assertIn("loads its options separately", ex.context)

    def test_megaframe_is_not_plain_steel(self):   # a different frame type is a different option
        from pricewatch.monitor import _option_score
        mega = "G3 (301-325cm) / Steel Megaframe"
        self.assertIsNone(_option_score(mega, "G3 (301-325cm) / Steel"))
        self.assertEqual(_option_score(mega, "G3 (301-325cm) / Steel Megaframe"), 2)
        self.assertIsNone(_option_score(HINT, "3 (300-325) / Steel EasyGrip"))
        self.assertEqual(_option_score(HINT, "Size 3 / 28mm Frame"), 2)

    def test_woocommerce_link_with_your_options(self):   # options named in the link, priced via wc-ajax
        from urllib.parse import urlencode
        url = (self.base + "/product/pavilion-p2/?" +
               urlencode({"attribute_size": "G19/G3 (301-325cm)", "attribute_depth": "300cm"}))
        ex, final = fetch_price(self.s, url)
        self.assertEqual((ex.price, ex.method), (Decimal("739.00"), "woocommerce"))
        self.assertEqual(Shop.lookups[-1]["product_id"], "77")
        self.assertIn("G19/G3 (301-325cm) / 300cm", ex.title)
        self.assertEqual(final, url)
        gone = url.replace("300cm", "400cm")
        self.assertIsNone(fetch_price(self.s, gone)[0].price)   # option no longer sold: no guess

    def test_size_sold_as_a_paid_extra(self):   # WAPF add-ons: base + size extra
        ex, _ = fetch_price(self.s, self.base + "/product/wapf-addons/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("579.00"), "jsonld"))
        self.assertIn("size 3-300-325", ex.title)
        self.assertIn("379.00 + extras 200.00", ex.context)
        ex, _ = fetch_price(self.s, self.base + "/product/wapf-addons/", "7 (400-425) / Steel 28 mm")
        self.assertIsNone(ex.price)                          # size not offered: no guess

    def test_woocommerce_options_picked_for_you(self):   # WooCommerce, prices loaded separately
        ex, url = fetch_price(self.s, self.base + "/product/woo-ajax-frames/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("669.00"), "woocommerce"))
        self.assertIn("attribute_pa_frame_options=28mm-easygrip-steel", url)   # re-checks ask the same
        self.assertIn("size-3-300-325-cm", url)
        self.assertEqual(fetch_price(self.s, url)[0].price, Decimal("669.00"))

    def test_free_frame_upgrade_prices_the_base_frame(self):   # WPC bundle: a £0 upgrade item
        ex, url = fetch_price(self.s, self.base + "/product/woo-free-upgrade/", HINT)
        self.assertEqual((ex.price, ex.method), (Decimal("539.00"), "woocommerce"))
        self.assertIn("attribute_pa_frame_options=25mm-easygrip-steel", url)
        self.assertIn("FREE 28mm Steel Frame Upgrade", ex.context)
        ex, _ = fetch_price(self.s, self.base + "/product/woo-free-upgrade/", "3 (300-325) / Mega")
        self.assertIsNone(ex.price)                     # a free 28 mm steel upgrade is not Megaframe

    def test_product_group_variants_by_size(self):   # schema.org: size on the variant, price on its offer
        ex, url = fetch_price(self.s, self.base + "/ridge-shelter.html", "7 (400-425) / Steel")
        self.assertEqual((ex.price, ex.method), (Decimal("508.00"), "jsonld"))
        self.assertTrue(url.endswith("/ridge-shelter.html"))
        ex, _ = fetch_price(self.s, self.base + "/ridge-shelter.html", "3 (300-325) / Steel")
        self.assertEqual(ex.price, Decimal("435.00"))

    def test_shop_without_your_option_reports_no_price(self):
        ex, _ = fetch_price(self.s, self.base + "/products/small", HINT)
        self.assertIsNone(ex.price)
        self.assertIn("no '3 (300-325) / Steel 28 mm' option", ex.context)

    def test_rechecks_of_another_shop_use_your_choice(self):
        db = DB(self.s.db_path)
        top = db.add_item(url="https://shop-a.example/products/x?variant=9", title=MINE,
                          price="579.00", currency="GBP", interval_minutes=1440)
        alt = db.add_item(url=self.base + "/product/gazebo-g300/", title="Gazebo G300 (1 (200-225cm))", price="359.00",
                          currency="GBP", interval_minutes=1440, parent_id=top, source="agent")
        check_item(self.s, db, db.get_item(alt))
        self.assertEqual(db.get_item(alt)["current_price"], "669.00")
        self.assertNotIn("200-225", db.get_item(alt)["title"])
        self.assertIn("28mm Frame", db.get_item(alt)["title"])


if __name__ == "__main__":
    unittest.main()
