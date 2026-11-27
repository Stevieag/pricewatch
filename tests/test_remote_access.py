# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Remote access (e.g. `tailscale serve`) must never work without an access token."""
import contextlib
import io
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from pricewatch import __main__ as cli
from pricewatch.agent import PriceAgent
from pricewatch.config import load_settings, parse_env_file, validate_settings
from pricewatch.db import DB
from pricewatch.web import serve


class StartupRefusal(unittest.TestCase):
    def test_remote_host_without_token_is_refused(self):
        s = load_settings({"PW_ALLOWED_HOSTS": "my-mac.example-tailnet.ts.net"})
        problems = validate_settings(s)
        self.assertTrue(any("PW_ALLOWED_HOSTS lets other devices in" in p for p in problems))

    def test_remote_host_with_token_is_fine(self):
        s = load_settings({"PW_ALLOWED_HOSTS": "my-mac.example-tailnet.ts.net", "PW_ACCESS_TOKEN": "t" * 32})
        self.assertEqual(validate_settings(s), [])

    def test_loopback_names_only_need_no_token(self):
        self.assertEqual(validate_settings(load_settings({"PW_ALLOWED_HOSTS": "localhost,127.0.0.1"})), [])


class RuntimeGuard(unittest.TestCase):
    """Even if the server was started some other way, a proxied request without a token is refused."""

    def test_proxied_host_without_token_gets_403(self):
        with tempfile.TemporaryDirectory() as d:
            s = load_settings({"PW_DATA_DIR": d, "PW_PORT": "0",
                               "PW_ALLOWED_HOSTS": "my-mac.example-tailnet.ts.net"})
            db = DB(s.db_path)
            httpd = serve(s, db, PriceAgent(s, db))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/", timeout=10) as r:          # local: fine
                    self.assertEqual(r.status, 200)
                req = urllib.request.Request(base + "/", headers={"Host": "my-mac.example-tailnet.ts.net"})
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(cm.exception.code, 403)
            finally:
                httpd.shutdown()


class SetToken(unittest.TestCase):
    def run_in(self, cfg_dir, content=None):
        env_file = os.path.join(cfg_dir, "pricewatch", "pricewatch.env")
        if content is not None:
            os.makedirs(os.path.dirname(env_file), exist_ok=True)
            with open(env_file, "w") as f:
                f.write(content)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg_dir}, clear=False), \
                mock.patch.dict(os.environ, {"PW_ENV_FILE": ""}), contextlib.redirect_stdout(out):
            code = cli.set_token()
        return code, out.getvalue(), env_file

    def test_replaces_existing_empty_line(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, f = self.run_in(d, "PW_HOST=127.0.0.1\nPW_ACCESS_TOKEN=\nPW_PORT=8765\n")
            self.assertEqual(code, 0)
            vals = parse_env_file(__import__("pathlib").Path(f))
            self.assertEqual(len(vals["PW_ACCESS_TOKEN"]), 43)
            self.assertIn(vals["PW_ACCESS_TOKEN"], out)                      # shown only after saving
            self.assertEqual((vals["PW_HOST"], vals["PW_PORT"]), ("127.0.0.1", "8765"))  # rest kept
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
            self.assertIn("Replaced", out)

    def test_adds_line_when_missing_and_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, f = self.run_in(d, "PW_HOST=127.0.0.1\n")
            self.assertEqual(code, 0)
            self.assertIn("PW_ACCESS_TOKEN", parse_env_file(__import__("pathlib").Path(f)))
        with tempfile.TemporaryDirectory() as d:
            code, out, f = self.run_in(d)                                     # no file at all
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(f))
            self.assertIn("Saved", out)


if __name__ == "__main__":
    unittest.main()
