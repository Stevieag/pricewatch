"""doctor + find-better CLI commands, against a fake Ollama and the fake shop/SearXNG."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pricewatch import __main__ as cli
from pricewatch.config import load_settings
from pricewatch.db import DB
from test_e2e import Shop

CALLS = []


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send({"models": [{"name": "gemma4:e2b"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        CALLS.append(req)
        prompt = req["messages"][-1]["content"]
        if "search query" in prompt:
            reply = {"query": "Acme Kettle AK-200 1.7L"}
        elif "SAME product" in prompt:
            reply = {"same": True}
        else:
            reply = {"ok": True}
        self._send({"message": {"role": "assistant", "content": json.dumps(reply)}})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class CLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shop, cls.ollama = serve(Shop), serve(FakeOllama)
        cls.tmp = tempfile.TemporaryDirectory()
        sp = cls.shop.server_address[1]
        cls.s = load_settings({
            "PW_DATA_DIR": cls.tmp.name, "PW_ALLOW_PRIVATE_HOSTS": "true",
            "PW_SEARCH_PROVIDER": "searxng", "PW_SEARXNG_URL": f"http://127.0.0.1:{sp}",
            "PW_LLM_PROVIDER": "ollama", "PW_LLM_MODEL": "gemma4:e2b",
            "PW_LLM_BASE_URL": f"http://127.0.0.1:{cls.ollama.server_address[1]}",
        })
        cls.s.env_file = cls.tmp.name  # pretend a config file was found
        cls.shop_url = f"http://127.0.0.1:{sp}/kettle"

    @classmethod
    def tearDownClass(cls):
        cls.shop.shutdown(); cls.ollama.shutdown(); cls.tmp.cleanup()

    def run_cli(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(self.s, *args)
        return code, out.getvalue()

    def test_doctor_all_good(self):
        from unittest import mock
        # don't depend on Chrome/Firefox being installed on the machine running the tests
        with mock.patch("pricewatch.fetcher.find_browser", return_value="/usr/bin/fake-browser"):
            code, out = self.run_cli(cli.doctor)
        self.assertNotIn("FAIL", out, out)
        self.assertIn("Model gemma4:e2b downloaded", out)
        self.assertIn("SearXNG search works", out)
        self.assertEqual(code, 0)

    def test_doctor_reports_missing_browser_with_fix(self):
        from unittest import mock
        with mock.patch("pricewatch.fetcher.find_browser", return_value=None):
            code, out = self.run_cli(cli.doctor)
        self.assertEqual(code, 1)
        self.assertIn("fix: install Firefox or Chrome", out)

    def test_doctor_reports_missing_model_with_fix(self):
        s = self.s
        self.s = load_settings({**{"PW_LLM_PROVIDER": "ollama", "PW_LLM_MODEL": "llama9",
                                   "PW_LLM_BASE_URL": s.llm_base_url}})
        try:
            code, out = self.run_cli(cli.doctor)
        finally:
            self.s = s
        self.assertEqual(code, 1)
        self.assertIn("fix: ollama pull llama9", out)

    def test_find_better_adds_cheaper_shop(self):
        db = DB(self.s.db_path)
        iid = db.add_item(url=self.shop_url, title="Acme Kettle AK-200 1.7L", price="49.99",
                          currency="GBP", interval_minutes=1440)
        CALLS.clear()
        code, out = self.run_cli(cli.find_better, iid)
        self.assertEqual(code, 0, out)
        self.assertIn("CHEAPER", out)
        self.assertIn("£44.00", out)
        self.assertRegex(out, r"link: http://localhost:\d+/other/kettle")
        # what the model was sent: titles/prices/host+path only - no query strings, no secrets
        sent = json.dumps(CALLS)
        self.assertNotIn("utm_", sent)
        match_prompts = [m["messages"][-1]["content"] for m in CALLS if "SAME product" in m["messages"][-1]["content"]]
        self.assertTrue(match_prompts)
        for p in match_prompts:
            url = p.split(" at ", 1)[1].split()[0]
            self.assertNotIn("?", url)  # URLs reach the model as scheme://host/path only


if __name__ == "__main__":
    unittest.main()
