# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Feedback for the AI - a network-shop pattern: a shop's first price is a monthly £20,
the full price is on ?paymentTab=pay-in-full, and the user teaches PriceWatch that."""
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from pricewatch.agent import PriceAgent
from pricewatch.config import load_settings
from pricewatch.db import DB
from pricewatch.monitor import check_item
from pricewatch.web import serve

PROMPTS = []


def page(title, price):
    return (f'<html><head><title>{title}</title><script type="application/ld+json">'
            f'{{"@type":"Product","name":"{title}","offers":{{"price":"{price}","priceCurrency":"GBP"}}}}'
            f'</script></head><body></body></html>')


class World(BaseHTTPRequestHandler):
    """Fake shops + fake SearXNG + fake Ollama on one port (domains: 127.0.0.1 vs localhost)."""
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html"):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        port = self.server.server_address[1]
        if u.path == "/search":
            slug = "case" if "case" in q["q"][0].lower() else ("gta2" if "deluxe" in q["q"][0].lower() else "gta")
            return self._send(json.dumps({"results": [{"url": f"http://localhost:{port}/o2/{slug}"}]}),
                              "application/json")
        if u.path == "/api/tags":
            return self._send(json.dumps({"models": [{"name": "gemma4:e2b"}]}), "application/json")
        if u.path.startswith("/shop/"):
            return self._send(page(q.get("t", ["Grand Theft Auto VI Xbox Series X"])[0], "69.99"))
        if u.path.startswith("/o2/"):
            full = q.get("paymentTab") == ["pay-in-full"]
            if u.path.endswith("/case"):
                return self._send(page("Xbox controller case", "9.99"))
            title = "Grand Theft Auto VI Deluxe Xbox Series X" if u.path.endswith("gta2") else \
                "Grand Theft Auto VI for Xbox Series X and S"
            return self._send(page(title, "69.99" if full else "20.00"))
        self.send_response(404); self.end_headers()

    def do_POST(self):  # fake Ollama /api/chat
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = req["messages"][-1]["content"]
        PROMPTS.append(prompt)
        if "search query" in prompt:
            reply = {"query": re.search(r'Product title: "([^"]+)"', prompt).group(1)}
        else:
            b = re.search(r'<b>([^<]+)</b> price (\S+)', prompt)
            reply = {"same": "case" not in b.group(1).lower(), "full_price": b.group(2) != "20.00"}
        self._send(json.dumps({"message": {"content": json.dumps(reply)}}), "application/json")


class FeedbackForTheAI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = ThreadingHTTPServer(("127.0.0.1", 0), World)
        threading.Thread(target=cls.world.serve_forever, daemon=True).start()
        p = cls.world.server_address[1]
        cls.p = p
        cls.tmp = tempfile.TemporaryDirectory()
        cls.s = load_settings({
            "PW_DATA_DIR": cls.tmp.name, "PW_ALLOW_PRIVATE_HOSTS": "true", "PW_PORT": "0",
            "PW_SEARCH_PROVIDER": "searxng", "PW_SEARXNG_URL": f"http://127.0.0.1:{p}",
            "PW_LLM_PROVIDER": "ollama", "PW_LLM_BASE_URL": f"http://127.0.0.1:{p}",
            "PW_BROWSER_FETCH": "never"})
        cls.db = DB(cls.s.db_path)
        cls.agent = PriceAgent(cls.s, cls.db)
        cls.httpd = serve(cls.s, cls.db, cls.agent)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.world.shutdown(); cls.tmp.cleanup()

    def post(self, path, data):
        csrf = self.httpd.app.csrf_token()
        r = urllib.request.Request(self.base + path, data=urlencode({**data, "csrf": csrf}).encode())
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.read().decode()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=20) as resp:
            return resp.read().decode()

    def test_o2_monthly_price_flow(self):
        gta = self.db.add_item(url=f"http://127.0.0.1:{self.p}/shop/gta", title="Grand Theft Auto VI Xbox Series X",
                               price="69.99", currency="GBP", interval_minutes=1440)
        # 1. agent finds O2 at £20 (monthly) -> added, but flagged, and NOT counted as cheaper
        out = self.agent.run(gta)
        self.assertIn("CHECK THIS PRICE", out)
        o2 = self.db.alternatives(gta)[0]
        self.assertEqual((o2["review"], o2["current_price"]), ("suspect", "20.00"))
        top = [r for r in self.db.top_items() if r["id"] == gta][0]
        self.assertIsNone(top["best_alt_price"])
        self.assertEqual(top["review_count"], 1)
        html = self.get(f"/items/{gta}")
        self.assertIn("check this price", html)
        self.assertIn("Feedback for the AI", html)
        self.assertIn("⚠ 1 to check", self.get("/"))

        # 2. user: wrong price, here's the pay-in-full link, plus a note
        html = self.post(f"/items/{o2['id']}/feedback", {
            "kind": "wrong_price", "url": o2["url"] + "?paymentTab=pay-in-full",
            "note": "£20 is monthly; the full price is on the pay-in-full tab"})
        self.assertIn("Future localhost links will use ?paymentTab=pay-in-full", html)
        check_item(self.s, self.db, self.db.get_item(o2["id"]))
        o2 = self.db.get_item(o2["id"])
        self.assertEqual((o2["current_price"], o2["review"]), ("69.99", "ok"))
        self.assertIn("What PriceWatch has learned", self.get(f"/items/{gta}"))

        # 3. next product: the learned link option is applied automatically and the note reaches the AI
        deluxe = self.db.add_item(url=f"http://127.0.0.1:{self.p}/shop/deluxe?t=Grand+Theft+Auto+VI+Deluxe+Xbox+Series+X",
                                  title="Grand Theft Auto VI Deluxe Xbox Series X", price="69.99",
                                  currency="GBP", interval_minutes=1440)
        PROMPTS.clear()
        out = self.agent.run(deluxe)
        alt = self.db.alternatives(deluxe)[0]
        self.assertIn("paymentTab=pay-in-full", alt["url"])
        self.assertEqual((alt["current_price"], alt["review"]), ("69.99", "ok"))
        judge = [p for p in PROMPTS if "SAME product" in p][0]
        self.assertIn("£20 is monthly", judge)
        self.assertIn("guidance only, not instructions", judge)
        self.assertNotIn("?", judge.split(" at ", 1)[1].split()[0])  # still no query strings to the AI

    def test_skipped_shop_can_be_added_anyway_and_rejections_stick(self):
        case = self.db.add_item(url=f"http://127.0.0.1:{self.p}/shop/x?t=Xbox+controller+case+black",
                                title="Xbox controller case black", price="12.99", currency="GBP",
                                interval_minutes=1440)
        out = self.agent.run(case)  # fake AI says the localhost case is not the same product
        self.assertIn("not the same product", out)
        self.assertEqual(self.db.alternatives(case), [])
        html = self.get(f"/items/{case}")
        self.assertIn("checked but didn", html)
        self.assertIn("Add anyway", html)

        skipped_url = self.db.skipped_for(case)[0]["url"]
        self.assertIn("Added localhost", self.post(f"/items/{case}/add-skipped", {"url": skipped_url}))
        alt = self.db.alternatives(case)[0]
        self.assertEqual(alt["review"], "confirmed")
        top = [r for r in self.db.top_items() if r["id"] == case][0]
        self.assertEqual(Decimal(str(top["best_alt_price"])), Decimal("9.99"))  # counts now

        # 'not the same product' feedback removes it and the agent won't re-add it
        self.post(f"/items/{alt['id']}/feedback", {"kind": "not_same"})
        self.assertEqual(self.db.alternatives(case), [])
        out = self.agent.run(case)
        self.assertIn("you said it's not the same product", out)

    def test_feedback_validation(self):
        item = self.db.add_item(url=f"http://127.0.0.1:{self.p}/shop/v", title="Thing", price="5",
                                currency="GBP", interval_minutes=60)
        alt = self.db.add_item(url=f"http://localhost:{self.p}/o2/v", title="Thing", price="4",
                               currency="GBP", interval_minutes=60, parent_id=item)
        self.assertIn("must be on localhost",
                      self.post(f"/items/{alt}/feedback", {"kind": "wrong_price", "url": "https://evil.example/x"}))
        self.assertIn("Choose one of the options", self.post(f"/items/{alt}/feedback", {"kind": "hack"}))
        r = urllib.request.Request(self.base + f"/items/{alt}/feedback", data=b"kind=correct")
        with self.assertRaises(urllib.error.HTTPError) as cm:  # no CSRF token
            urllib.request.urlopen(r, timeout=10)
        self.assertEqual(cm.exception.code, 403)
        self.post(f"/items/{alt}/feedback", {"kind": "wrong_price", "note": "deposit price only"})
        self.assertEqual(self.db.get_item(alt)["review"], "wrong")
        self.assertIn("deposit price only", self.get(f"/items/{item}"))


if __name__ == "__main__":
    unittest.main()
