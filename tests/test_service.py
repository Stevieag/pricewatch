# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""start / status / stop of the PriceWatch server in the background (search + AI off, so no
Docker or Ollama is needed)."""
import contextlib
import io
import os
import socket
import tempfile
import unittest
from unittest import mock

from pricewatch import service
from pricewatch.config import load_settings


def free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


class StartStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"PW_DATA_DIR": self.tmp.name, "PW_PORT": str(free_port()),
               "PW_SEARCH_PROVIDER": "none", "PW_LLM_PROVIDER": "none", "PW_FAIR_INTERVAL_HOURS": "0",
               "XDG_CONFIG_HOME": self.tmp.name, "PW_ENV_FILE": os.path.join(self.tmp.name, "none.env")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.s = load_settings({k: v for k, v in env.items() if k.startswith("PW_")})

    def tearDown(self):
        with contextlib.redirect_stdout(io.StringIO()):
            service.stop(self.s)
        self.env.stop()
        self.tmp.cleanup()

    def run_quiet(self, fn, *a, **k):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(*a, **k)
        return code, out.getvalue()

    def test_start_status_stop(self):
        code, out = self.run_quiet(service.start, self.s, open_browser=False)
        self.assertEqual(code, 0, out)
        self.assertIn("PriceWatch started", out)
        pid = service.server_pid(self.s)
        self.assertTrue(pid)
        self.assertEqual(oct(os.stat(service.log_file(self.s)).st_mode & 0o777), "0o600")

        code, out = self.run_quiet(service.start, self.s, open_browser=False)   # idempotent
        self.assertIn("already running", out)
        self.assertEqual(service.server_pid(self.s), pid)

        code, out = self.run_quiet(service.status, self.s)
        self.assertEqual(code, 0)
        self.assertIn(f"pid {pid}", out)

        code, out = self.run_quiet(service.stop, self.s)
        self.assertIn("PriceWatch stopped", out)
        self.assertIsNone(service.server_pid(self.s))
        self.assertIn("PriceWatch running at", open(service.log_file(self.s)).read())  # it really ran

        code, out = self.run_quiet(service.status, self.s)
        self.assertEqual(code, 1)
        self.assertIn("not running", out)

    def test_detects_searxng_and_ollama_already_running(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Up(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                b = json.dumps({"models": []}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b)))
                self.end_headers(); self.wfile.write(b)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Up)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        self.s.search_provider, self.s.searxng_url = "searxng", url
        self.s.llm_provider, self.s.llm_base_url = "ollama", url
        try:
            with mock.patch.object(service, "start_docker") as docker, \
                    mock.patch.object(service, "start_server", return_value=True):
                code, out = self.run_quiet(service.start, self.s, open_browser=False)
            self.assertEqual(code, 0, out)
            self.assertIn("SearXNG running", out)
            self.assertIn("Ollama running", out)
            docker.assert_not_called()       # nothing to start: Docker isn't touched
        finally:
            srv.shutdown()
            self.s.search_provider = self.s.llm_provider = "none"

    def test_autostart_launch_agent(self):
        import plistlib
        import sys
        with mock.patch.object(service, "IS_MAC", True), \
                mock.patch.object(service, "launch_agent_path",
                                  return_value=__import__("pathlib").Path(self.tmp.name) / "LA" / "x.plist"), \
                mock.patch.object(service, "_run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            code, out = self.run_quiet(service.autostart, self.s, "on")
            self.assertEqual(code, 0, out)
            plist = plistlib.loads(service.launch_agent_path().read_bytes())
            self.assertEqual(plist["ProgramArguments"], [sys.executable, "-m", "pricewatch", "start", "--no-browser"])
            self.assertTrue(plist["RunAtLoad"] and plist["AbandonProcessGroup"])
            self.assertIn("/usr/local/bin", plist["EnvironmentVariables"]["PATH"])
            self.assertTrue(any(c.args[0][:2] == ["launchctl", "bootstrap"] for c in run.call_args_list))
            code, out = self.run_quiet(service.autostart, self.s, "off")
            self.assertFalse(service.launch_agent_path().exists())
            self.assertIn("turned off", out)

    def test_stale_pid_file_is_ignored(self):
        os.makedirs(self.s.data_dir, exist_ok=True)
        service.pid_file(self.s).write_text("999999")
        self.assertIsNone(service.server_pid(self.s))
        self.assertFalse(service.pid_file(self.s).exists())


if __name__ == "__main__":
    unittest.main()
