# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""End-to-end: a fake shop + fake SearXNG on localhost, PriceWatch talking to both."""
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit

from pricewatch import fair
from pricewatch.agent import PriceAgent
from pricewatch.config import load_settings
from pricewatch.db import DB
from pricewatch.monitor import Scheduler, wait_idle
from pricewatch.web import serve

STATE = {"price": "49.99"}


def product(name, price, cur="GBP"):
    return f"""<html><head><title>{name} | Shop</title><script type="application/ld+json">
{{"@type":"Product","name":"{name}","offers":{{"@type":"Offer","price":"{price}","priceCurrency":"{cur}"}}}}
</script></head><body><h1>{name}</h1></body></html>"""


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        port = self.server.server_address[1]
        if path == "/search":  # fake SearXNG
            body = json.dumps({"results": [
                {"url": f"http://localhost:{port}/other/kettle"},   # same product, cheaper, other "domain"
                {"url": f"http://127.0.0.1:{port}/kettle?utm_source=x"},  # original domain -> skipped
            ]})
            ctype = "application/json"
        elif path == "/kettle":
            # iPhones are quoted more: simulates device-based pricing
            ua = self.headers.get("User-Agent", "")
            body = product("Acme Kettle AK-200 1.7L", "54.99" if "iPhone" in ua else STATE["price"])
            ctype = "text/html"
        elif path == "/other/kettle":
            body = product("Acme Kettle AK-200 1.7L Stainless", "44.00")
            ctype = "text/html"
        else:
            self.send_response(404); self.end_headers(); return
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shop = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
        threading.Thread(target=cls.shop.serve_forever, daemon=True).start()
        sp = cls.shop.server_address[1]
        cls.shop_url = f"http://127.0.0.1:{sp}/kettle"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.token = "T" * 32
        cls.s = load_settings({
            "PW_PORT": "0", "PW_DATA_DIR": cls.tmp.name, "PW_ALLOW_PRIVATE_HOSTS": "true",
            "PW_SEARCH_PROVIDER": "searxng", "PW_SEARXNG_URL": f"http://127.0.0.1:{sp}",
            "PW_SCHEDULER_TICK_SECONDS": "1", "PW_MIN_INTERVAL_MINUTES": "1",
            "PW_ACCESS_TOKEN": cls.token, "PW_FAIR_INTERVAL_HOURS": "0",  # gitleaks:allow (test value)
        })
        cls.db = DB(cls.s.db_path)
        cls.agent = PriceAgent(cls.s, cls.db)
        cls.fair = fair.FairRunner(cls.s, cls.db)
        cls.sched = Scheduler(cls.s, cls.db)
        cls.sched.start()
        cls.httpd = serve(cls.s, cls.db, cls.agent, cls.fair)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.cookie = f"pw_auth={cls.httpd.app.session_value()}"

    @classmethod
    def tearDownClass(cls):
        cls.sched.stop(); cls.httpd.shutdown(); cls.shop.shutdown(); cls.tmp.cleanup()

    # helpers
    def req(self, path, data=None, headers=None, auth=True, follow=True):
        h = {"Cookie": self.cookie} if auth else {}
        h.update(headers or {})
        body = urlencode(data).encode() if isinstance(data, dict) else data
        r = urllib.request.Request(self.base + path, data=body, headers=h)
        opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(r, timeout=20) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def csrf(self):
        _, html, _ = self.req("/login")
        return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)

    # tests (unittest runs them alphabetically)
    def test_01_security_headers_and_auth(self):
        st, _, h = self.req("/", auth=False, follow=False)
        self.assertEqual(st, 303)
        self.assertEqual(h["Location"], "/login")
        st, body, h = self.req("/login", auth=False)
        self.assertIn("script-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Frame-Options"], "DENY")

    def test_02_dns_rebinding_blocked(self):
        st, _, _ = self.req("/", headers={"Host": "evil.example:1234"})
        self.assertEqual(st, 421)

    def test_03_csrf_required(self):
        st, _, _ = self.req("/items", {"url": self.shop_url, "every": "1", "unit": "1440"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("/items", {"url": self.shop_url, "every": "1", "unit": "1440", "csrf": self.csrf()},
                            headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)

    def test_04_add_flow_with_seen_price(self):
        st, html, _ = self.req("/add?" + urlencode({"text": f"look {self.shop_url}?session=abc", "seen": "59.99"}))
        self.assertEqual(st, 200)
        self.assertIn("£49.99", html)
        self.assertIn("personalised pricing", html)
        self.assertNotIn("session=abc", html)
        st, html, _ = self.req("/items", {"url": self.shop_url, "title": "Acme Kettle AK-200 1.7L",
                                          "price": "49.99", "currency": "GBP", "method": "jsonld",
                                          "every": "15", "unit": "1", "seen": "59.99", "csrf": self.csrf()})
        self.assertEqual(st, 200)
        self.assertIn("Watching", html)
        self.assertIn("every 15 minutes", html)

    def test_05_scheduler_rechecks_and_chart(self):
        item = self.db.q("SELECT * FROM items WHERE parent_id IS NULL")[0]
        STATE["price"] = "47.50"
        self.db.force_due(item["id"])
        time.sleep(1.5); wait_idle(self.sched)
        self.assertEqual(self.db.get_item(item["id"])["current_price"], "47.50")
        st, html, _ = self.req(f"/items/{item['id']}?range=all")
        self.assertIn('<svg class="pw-chart"', html)
        self.assertIn("Table view", html)

    def test_06_fair_price(self):
        iid = self.db.q("SELECT id FROM items WHERE parent_id IS NULL")[0]["id"]
        n = fair.run_personas(self.s, self.db, iid)
        self.assertEqual(n, len(fair.PERSONAS))
        r = fair.report(self.s, self.db, iid)
        titles = [v.title for v in r.verdicts]
        self.assertIn("Possible personalised pricing", titles)      # user saw 59.99 vs baseline 47.50
        self.assertIn("Price changes by device or profile", titles)  # iPhone gets 54.99
        _, html, _ = self.req(f"/items/{iid}")
        self.assertIn("Fair price", html)
        self.assertIn("iPhone · Safari", html)

    def test_07_agent_finds_cheaper_shop(self):
        iid = self.db.q("SELECT id FROM items WHERE parent_id IS NULL")[0]["id"]
        summary = self.agent.run(iid)
        self.assertIn("added 1", summary)
        alts = self.db.alternatives(iid)
        self.assertEqual(len(alts), 1)
        self.assertEqual(alts[0]["current_price"], "44.00")
        self.assertEqual(alts[0]["source"], "agent")
        _, html, _ = self.req("/")
        self.assertIn("cheaper", html)
        # best-elsewhere price links straight to the cheaper shop, in a new tab
        self.assertRegex(html, r'<a href="http://localhost:\d+/other/kettle" target="_blank" rel="noopener noreferrer"')

    def test_08_api_share_needs_bearer(self):
        payload = json.dumps({"url": self.shop_url + "?ref=share", "frequency": "6h", "seen": "47.50"}).encode()
        st, _, _ = self.req("/api/share", payload, {"Content-Type": "application/json"}, auth=False)
        self.assertEqual(st, 401)
        st, body, _ = self.req("/api/share", payload, {"Content-Type": "application/json",
                                                       "Authorization": f"Bearer {self.token}"}, auth=False)
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["every"], "6 hours")

    def test_09_ssrf_guard_on_by_default(self):
        s = load_settings({"PW_DATA_DIR": self.tmp.name})
        from pricewatch.monitor import fetch_price
        from pricewatch.fetcher import FetchError
        with self.assertRaises(FetchError):
            fetch_price(s, self.shop_url)

    # Refresh all + alert price on the watchlist page
    def test_10_alert_price_and_refresh_all(self):
        if not self.db.q("SELECT id FROM items WHERE parent_id IS NULL"):
            self.db.add_item(url=self.shop_url, title="Acme Kettle AK-200 1.7L", price="49.99",
                             currency="GBP", interval_minutes=1440)
        iid = self.db.q("SELECT id FROM items WHERE parent_id IS NULL")[0]["id"]
        STATE["price"] = "49.99"

        # alert above the current price -> banner + "hit"
        st, html, _ = self.req(f"/items/{iid}/target", {"target": "50", "csrf": self.csrf()})
        self.assertEqual(st, 200)
        self.assertIn("Alert set", html)
        self.assertIn("at or below your alert", html)
        self.assertIn("✓ hit", html)
        self.assertIn("↗</a> (alert £50.00)", html)  # banner links to the shop with the price

        # alert below the price -> no banner, shows how far to go
        st, html, _ = self.req(f"/items/{iid}/target", {"target": "30", "csrf": self.csrf()})
        self.assertNotIn("at or below your alert", html)
        self.assertIn("to go", html)

        # bad value rejected, empty value removes the alert
        _, html, _ = self.req(f"/items/{iid}/target", {"target": "abc", "csrf": self.csrf()})
        self.assertIn("must be a number", html)
        _, html, _ = self.req(f"/items/{iid}/target", {"target": "", "csrf": self.csrf()})
        self.assertIn("Alert removed", html)
        self.assertIsNone(self.db.get_item(iid)["target_price"])

        # alert form needs CSRF like every other form
        st, _, _ = self.req(f"/items/{iid}/target", {"target": "1"})
        self.assertEqual(st, 403)

        # Refresh all: everything due now, page shows progress + auto-reload
        STATE["price"] = "45.00"
        st, html, _ = self.req("/refresh-all", {"csrf": self.csrf()})
        self.assertEqual(st, 200)
        self.assertIn("Refreshing", html)
        time.sleep(2.5); wait_idle(self.sched)
        self.assertEqual(self.db.get_item(iid)["current_price"], "45.00")
        _, html, _ = self.req("/")
        self.assertIn("Refresh all prices", html)
        self.assertNotIn('http-equiv="refresh"', html)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


if __name__ == "__main__":
    unittest.main()


class BookmarkletNewTab(unittest.TestCase):
    def test_opens_a_new_tab_and_keeps_the_shop_page(self):
        from pricewatch.web import BOOKMARKLET_JS
        self.assertIn("window.open(u,'_blank')", BOOKMARKLET_JS)
        self.assertIn("location.href=u", BOOKMARKLET_JS)       # fallback if pop-ups are blocked
        self.assertNotIn("noopener", BOOKMARKLET_JS)            # would make window.open return null


class BookmarkletShopifyVariant(unittest.TestCase):
    def test_reads_the_selected_shopify_variant_first(self):
        from pricewatch.web import BOOKMARKLET_JS
        self.assertLess(BOOKMARKLET_JS.index("ShopifyAnalytics"), BOOKMARKLET_JS.index("ld+json"))
        self.assertIn("get('variant')", BOOKMARKLET_JS)
