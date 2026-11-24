"""Rules from the real find-better run: non-shops, list pages, monthly offers, model numbers,
and re-judging prices after every check."""
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from pricewatch.agent import PriceAgent, clean_title_for_search
from pricewatch.config import load_settings
from pricewatch.db import DB
from pricewatch.extract import extract
from pricewatch.monitor import check_item
from pricewatch.review import (looks_like_listing, model_codes, not_a_shop, price_doubt,
                               shares_model_code)


class Rules(unittest.TestCase):
    def test_not_shops(self):
        for u in ["https://www.facebook.com/surreylivenews/posts/1", "https://www.youtube.com/watch?v=x",
                  "https://www.radiotimes.com/technology/new-ipad-mini-preorder-uk/",
                  "https://pricespy.co.uk/s/gta-vi/", "https://support.apple.com/en-mk/149482",
                  "https://uk.pcmag.com/x", "https://www.lyst.co.uk/clothing/x"]:
            self.assertTrue(not_a_shop(u), u)
        for u in ["https://www.argos.co.uk/product/9933075", "https://www.o2.co.uk/shop/microsoft/gta",
                  "https://www.uktoolstore.co.uk/blackdecker-hover-mower/"]:
            self.assertIsNone(not_a_shop(u), u)

    def test_listing_pages(self):
        for u in ["https://www.johnlewis.com/browse/men/mens-shirts/tommy-hilfiger/stripe/_/N-x",
                  "https://www.amazon.co.uk/crew-neck-cardigan/s?k=crew+neck+cardigan",
                  "https://www.ebay.co.uk/b/bn_3588311", "https://uk.tommy.com/mens-shirts/c/red",
                  "https://chelseapeers.com/collections/satin-pyjama-sets"]:
            self.assertTrue(looks_like_listing(u), u)
        for u in ["https://www.o2.co.uk/shop/microsoft/gta?paymentTab=pay-in-full",
                  "https://www.argos.co.uk/product/9841129", "https://www.next.co.uk/style/su983272/y40513"]:
            self.assertFalse(looks_like_listing(u), u)

    def test_price_doubt(self):
        self.assertIn("much lower", price_doubt(20, "69.99", "jsonld+browser"))
        self.assertIn("much higher", price_doubt(539, "69.99", "jsonld"))
        self.assertIn("page text", price_doubt(37.99, "62", "price-element+browser"))
        self.assertIsNone(price_doubt("54.99", "62", "jsonld"))

    def test_model_codes(self):
        a = model_codes("BLACK+DECKER 1200W 30cm Electric Hover Mower, BEMWH551-GB")
        self.assertEqual(a, {"bemwh551gb"})
        self.assertEqual(shares_model_code(a, model_codes("Black & Decker BEMWH551 Corded")), "BEMWH551")
        self.assertEqual(model_codes("Apple iPad mini A20 Pro 2026 8.5 Inch 128GB 1200W"), set())

    def test_monthly_offer_ignored_and_list_pages_flagged(self):
        o2 = ('<script type="application/ld+json">{"@type":"Product","name":"GTA VI","offers":['
              '{"price":"20.00","priceCurrency":"GBP","priceSpecification":{"price":"20.00","billingDuration":"P1M"}},'
              '{"price":"69.99","priceCurrency":"GBP"}]}</script>')
        self.assertEqual(extract(o2).price, Decimal("69.99"))
        lst = ('<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"@type":"ListItem",'
               '"item":{"@type":"Product","name":"Shirt","offers":{"price":"40"}}}]}</script><b class="price">£40</b>')
        ex = extract(lst)
        self.assertTrue(ex.listing)
        self.assertIsNone(ex.price)

    def test_shop_name_out_of_search(self):
        self.assertEqual(
            clean_title_for_search("Grand Theft Auto VI Standard Edition | Xbox Series X|S Digital Code | "
                                   "Amazon.co.uk: PC", "amazon.co.uk"),
            "Grand Theft Auto VI Standard Edition Xbox Series X S Digital Code")


def page(title, price, method="jsonld"):
    if method == "jsonld":
        return (f'<title>{title}</title><script type="application/ld+json">{{"@type":"Product","name":"{title}",'
                f'"offers":{{"price":"{price}","priceCurrency":"GBP"}}}}</script>')
    return f'<title>{title}</title><h1>{title}</h1><span class="price">£{price}</span>'


class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlsplit(self.path)
        port = self.server.server_address[1]
        loc = f"http://localhost:{port}"
        routes = {
            "/search": json.dumps({"results": [{"url": u2} for u2 in [
                "https://www.facebook.com/x/posts/1",          # not a shop: skipped before fetching
                f"{loc}/browse/mowers",                         # list page by URL
                f"{loc}/mower-bemwh551gb"]]}),                  # same model number
            "/mower-bemwh551gb": page("Black and Decker BEMWH551GB corded hover lawn mower", "49.99"),
            "/orig": page("BLACK+DECKER 1200W Hover Mower BEMWH551-GB", "62.00"),
            "/o2": page("GTA VI", "20.00"),
            "/api/tags": json.dumps({"models": [{"name": "gemma4:e2b"}]}),
        }
        body = routes.get(u.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if body.startswith("{") else "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):  # a small model that wrongly says "not the same" every time
        self.rfile.read(int(self.headers["Content-Length"]))
        b = json.dumps({"message": {"content": json.dumps({"same": False, "full_price": True, "query": "mower"})}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


class AgentOnRealProblems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Web)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.p = cls.srv.server_address[1]
        cls.tmp = tempfile.TemporaryDirectory()
        cls.s = load_settings({"PW_DATA_DIR": cls.tmp.name, "PW_ALLOW_PRIVATE_HOSTS": "true",
                               "PW_SEARCH_PROVIDER": "searxng", "PW_SEARXNG_URL": f"http://127.0.0.1:{cls.p}",
                               "PW_LLM_PROVIDER": "ollama", "PW_LLM_BASE_URL": f"http://127.0.0.1:{cls.p}",
                               "PW_BROWSER_FETCH": "never"})
        cls.db = DB(cls.s.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.tmp.cleanup()

    def test_filters_and_model_number_override(self):
        iid = self.db.add_item(url=f"http://127.0.0.1:{self.p}/orig", title="BLACK+DECKER 1200W Hover Mower BEMWH551-GB",
                               price="62.00", currency="GBP", interval_minutes=1440)
        out = PriceAgent(self.s, self.db).run(iid)
        self.assertIn("facebook.com: skipped - not a shop", out)
        self.assertIn("a search/category page", out)
        self.assertIn("ADDED at 49.99 (cheaper!) (same model BEMWH551GB)", out)  # AI said no; model no. says yes

    def test_recheck_flags_o2_style_price(self):
        iid = self.db.add_item(url=f"http://127.0.0.1:{self.p}/orig?x=2", title="GTA VI", price="69.99",
                               currency="GBP", interval_minutes=1440)
        alt = self.db.add_item(url=f"http://localhost:{self.p}/o2", title="GTA VI", price="69.99",
                               currency="GBP", interval_minutes=1440, parent_id=iid, review="ok")
        check_item(self.s, self.db, self.db.get_item(alt))
        a = self.db.get_item(alt)
        self.assertEqual((a["current_price"], a["review"]), ("20.00", "suspect"))
        top = [r for r in self.db.top_items() if r["id"] == iid][0]
        self.assertIsNone(top["best_alt_price"])  # no longer shown as the cheapest
        # once you confirm it, re-checks leave it alone
        self.db.set_review(alt, "confirmed", "Confirmed by you")
        check_item(self.s, self.db, self.db.get_item(alt))
        self.assertEqual(self.db.get_item(alt)["review"], "confirmed")


class InAppSearch(AgentOnRealProblems):
    """The watchlist box: words search via SearXNG, links look up the price."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from pricewatch.web import serve
        cls.httpd = serve(cls.s, cls.db, PriceAgent(cls.s, cls.db))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        super().tearDownClass()

    # the inherited agent tests run once in the parent class only
    test_filters_and_model_number_override = None
    test_recheck_flags_o2_style_price = None

    def get(self, path):
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=20) as r:
            return r.geturl(), r.read().decode()

    def test_words_search_and_filter(self):
        url, html = self.get("/add?text=black+decker+hover+mower")
        self.assertIn("/search?q=black+decker+hover+mower", url)          # redirected to search
        self.assertIn("/mower-bemwh551gb", html)                            # the shop product page
        self.assertNotIn("facebook.com", html.split("Hid")[0])              # non-shop hidden
        self.assertNotIn("/browse/mowers", html)                            # list page hidden
        self.assertIn("Hid 2 result(s)", html)
        self.assertIn('href="/add?text=http', html)                         # Watch this -> price lookup

    def test_link_still_looks_up_price(self):
        url, html = self.get(f"/add?text=http://localhost:{self.p}/mower-bemwh551gb")
        self.assertIn("/add?", url)
        self.assertIn("£49.99", html)
        self.assertIn("Monitor this price", html)

    def test_search_page_escapes_query(self):
        _, html = self.get("/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
        self.assertNotIn("<script>alert(1)", html)


if __name__ == "__main__":
    unittest.main()
