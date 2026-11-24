"""Headless-browser fallback, against a fake shop that blocks non-browsers (like Argos)."""
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pricewatch.config import load_settings
from pricewatch.fetcher import FetchError, find_browser
from pricewatch.monitor import fetch_price

PAGE = """<html><head><title>Blocked Shop</title></head><body><h1>Tablet 128GB</h1>
<div data-test="product-price"></div>
<script>document.querySelector('[data-test=product-price]').innerHTML='<h2>&pound;699.00</h2>';</script>
</body></html>"""


class BotWall(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # real browsers send Sec-Fetch-* headers; plain HTTP clients don't
        if not self.headers.get("Sec-Fetch-Mode"):
            self.send_response(403); self.end_headers(); self.wfile.write(b"Access Denied"); return
        b = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@unittest.skipUnless(find_browser(), "no Chrome/Edge/Brave/Chromium/Firefox installed")
class BrowserFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), BotWall)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/product/9841129"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def settings(self, mode):
        return load_settings({"PW_ALLOW_PRIVATE_HOSTS": "true", "PW_BROWSER_FETCH": mode})

    def test_never_mode_reports_block(self):
        with self.assertRaises(FetchError) as cm:
            fetch_price(self.settings("never"), self.url)
        self.assertIn("blocked", str(cm.exception))

    def test_auto_falls_back_to_browser(self):
        ex, _ = fetch_price(self.settings("auto"), self.url)
        self.assertEqual(ex.price, Decimal("699.00"))      # also proves JavaScript ran
        self.assertEqual(ex.method, "price-element+browser")

    def test_browser_still_ssrf_guarded(self):
        s = load_settings({"PW_BROWSER_FETCH": "always"})
        with self.assertRaises(FetchError):
            fetch_price(s, self.url)


if __name__ == "__main__":
    unittest.main()


class FirefoxMarionette(unittest.TestCase):
    """Drives tests/fixtures/firefox, a fake Firefox that speaks the Marionette protocol."""

    def test_firefox_fetch(self):
        import os
        from pricewatch.fetcher import browser_get, is_firefox
        fake = os.path.join(os.path.dirname(__file__), "fixtures", "firefox")
        self.assertTrue(is_firefox(fake))
        res = browser_get("http://127.0.0.1:9/product/1", browser=fake, user_agent="x", allow_private=True)
        from pricewatch.extract import extract
        self.assertEqual(extract(res.text).price, Decimal("699.00"))
        self.assertEqual(res.url, "http://127.0.0.1:9/product/1")


@unittest.skipUnless(find_browser(), "no Chrome/Edge/Brave/Chromium/Firefox installed")
class FairPersonasThroughBrowser(unittest.TestCase):
    def test_personas_fall_back_to_browser_when_blocked(self):
        import tempfile
        from pricewatch import fair
        from pricewatch.db import DB
        srv = ThreadingHTTPServer(("127.0.0.1", 0), BotWall)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                s = load_settings({"PW_ALLOW_PRIVATE_HOSTS": "true", "PW_DATA_DIR": d})
                db = DB(s.db_path)
                iid = db.add_item(url=f"http://127.0.0.1:{srv.server_address[1]}/p", title="Tablet",
                                  price="699.00", currency="GBP", interval_minutes=1440)
                fair.BROWSER_SPACING_SECONDS = 0
                self.assertEqual(fair.run_personas(s, db, iid), len(fair.BROWSER_LANGS))
                rep = fair.report(s, db, iid)
                self.assertEqual(str(rep.baseline), "699.00")
                labels = {label: err for label, _, err, *_ in rep.latest}
                self.assertIn("Anonymous browser · German", labels)
                self.assertIn("device imitation", labels["iPhone · Safari"])
        finally:
            srv.shutdown()
