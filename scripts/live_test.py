#!/usr/bin/env python3
"""Live end-to-end test against a REAL shop, on your own machine.

    python3 scripts/live_test.py ["https://shop.example/product/123"]

Uses a temporary database and a random port; your real PriceWatch data is not touched.
Takes 1-3 minutes (the Fair Price step opens a browser once per shopper profile).
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pricewatch import fair  # noqa: E402
from pricewatch.agent import PriceAgent  # noqa: E402
from pricewatch.config import load_settings  # noqa: E402
from pricewatch.db import DB  # noqa: E402
from pricewatch.fetcher import find_browser  # noqa: E402
from pricewatch.monitor import check_item, fetch_price  # noqa: E402
from pricewatch.web import serve  # noqa: E402

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
URL = ARGS[0] if ARGS else "https://www.argos.co.uk/product/9841129"
EXTRA = {"PW_ALLOW_PRIVATE_HOSTS": "true"} if "--allow-private" in sys.argv else {}  # self-test only
results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  - ' + detail) if detail else ''}", flush=True)


def main() -> int:
    print(f"PriceWatch live test · Python {sys.version.split()[0]} · {URL}\n")
    step("Python 3.9+", sys.version_info >= (3, 9), sys.version.split()[0])
    br = find_browser()
    step("Browser for blocked shops", bool(br), br or "none found (install Chrome or Firefox)")

    print("\n[1] Unit + integration tests")
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       cwd=ROOT, capture_output=True, text=True)
    status = [ln for ln in r.stderr.splitlines() if re.match(r"^(OK|FAILED)\b", ln)]
    last = status[-1] if status else "no result line"
    ran = re.search(r"Ran (\d+) tests", r.stderr)
    step("Test suite", r.returncode == 0, f"{ran.group(1) if ran else '?'} tests, {last}")

    print("\n[2] Secret scan")
    r = subprocess.run([sys.executable, "scripts/check_secrets.py", "--all"], cwd=ROOT,
                       capture_output=True, text=True)
    step("No secrets in project files", r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[0])

    with tempfile.TemporaryDirectory() as tmp:
        token = "live-test-token-" + "x" * 16
        s = load_settings({"PW_DATA_DIR": tmp, "PW_PORT": "0", "PW_ACCESS_TOKEN": token, **EXTRA})
        db = DB(s.db_path)

        print("\n[3] v1 - read the price from the real page")
        t = time.time()
        try:
            ex, final = fetch_price(s, URL)
            step("Price extracted", ex.price is not None,
                 f"{ex.title!r} = {ex.price} {ex.currency} via {ex.method} ({time.time() - t:.0f}s)")
        except Exception as e:  # noqa: BLE001
            step("Price extracted", False, str(e))
            return summary()

        httpd = serve(s, db, PriceAgent(s, db))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        cookie = {"Cookie": f"pw_auth={httpd.app.session_value()}"}

        def get(path, data=None, headers=None):
            req = urllib.request.Request(base + path, data=urlencode(data).encode() if data else None,
                                         headers={**cookie, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.status, resp.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        print("\n[4] Web app: add the item and choose a frequency")
        st, html = get("/add?" + urlencode({"text": URL}))
        step("Share/paste page shows the price", st == 200 and str(ex.price) in html.replace(",", ""), f"HTTP {st}")
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)
        st, html = get("/items", {"url": final, "title": ex.title or "", "price": str(ex.price),
                                  "currency": ex.currency or "", "method": ex.method,
                                  "every": "6", "unit": "60", "csrf": csrf})
        step("Item saved, checking every 6 hours", st == 200 and "every 6 hours" in html, f"HTTP {st}")
        item = db.q("SELECT * FROM items")[0]

        print("\n[5] Scheduled re-check")
        check_item(s, db, item)
        it = db.get_item(item["id"])
        step("Re-check recorded a price", it["last_error"] is None and it["current_price"] is not None,
             it["last_error"] or f"{it['current_price']} {it['currency']}")

        print("\n[6] v3 - price history graph")
        st, html = get(f"/items/{item['id']}?range=all")
        step("Graph rendered", '<svg class="pw-chart"' in html, f"{len(db.history(item['id']))} data points")

        print("\n[7] v4 - Fair Price (anonymous shopper profiles; slowest step)")
        t = time.time()
        n = fair.run_personas(s, db, item["id"])
        rep = fair.report(s, db, item["id"])
        for label, price, err in rep.latest:
            print(f"        {label:<28} {price if price is not None else '-':>10}  {err or ''}")
        step("Anonymous baseline price found", rep.baseline is not None,
             f"{n} profile(s) priced in {time.time() - t:.0f}s, baseline {rep.baseline}")
        for v in rep.verdicts:
            print(f"        [{v.level}] {v.title}: {v.detail}")

        print("\n[8] v2 - AI price search")
        if s.search_provider == "none":
            step("AI search", True, "skipped: needs SearXNG (+ optional Ollama), see README v2")
        else:
            try:
                out = PriceAgent(s, db).run(item["id"])
                step("AI search ran", True, out.splitlines()[0])
            except Exception as e:  # noqa: BLE001
                step("AI search ran", False, str(e))

        print("\n[9] Security checks")
        st, _ = get("/", headers={"Host": "evil.example"})
        step("DNS-rebinding blocked (wrong Host)", st == 421, f"HTTP {st}")
        st, _ = get("/items", {"url": final, "every": "1", "unit": "1440"})
        step("Form without CSRF token rejected", st == 403, f"HTTP {st}")
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(_NoRedirect).open(base + "/", timeout=10)
            unauth = 200
        except urllib.error.HTTPError as e:
            unauth = e.code
        step("Unauthenticated request sent to sign-in", unauth == 303, f"HTTP {unauth}")
        httpd.shutdown()

    return summary()


def summary() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)} passed, {len(failed)} failed")
    for name, _, detail in failed:
        print(f"  FAILED: {name} - {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
