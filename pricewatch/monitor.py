# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Price checking + the background scheduler."""
from __future__ import annotations

import logging
from decimal import Decimal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Settings
from .db import DB, now
from .extract import Extracted, extract
from .fetcher import FetchError, browser_get, find_browser, safe_get
import json
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

log = logging.getLogger("pricewatch.monitor")


def fetch_price(settings: Settings, url: str) -> tuple[Extracted, str]:
    """Fetch a page and extract its price. Returns (extracted, final_url).

    Plain HTTP first (fast, light). If the shop blocks it or no price is found, and a
    browser is installed (Chrome/Edge/Brave/Chromium, else Firefox), retry with it headless
    (see fetcher)."""
    mode = settings.browser_fetch
    first_error: FetchError | None = None
    if mode != "always":
        try:
            res = safe_get(url, user_agent=settings.user_agent, timeout=settings.fetch_timeout,
                           allow_private=settings.allow_private_hosts)
            ex = _shopify_variant(settings, res.url, res.text, extract(res.text))
            if ex.price is not None or mode == "never":
                return ex, res.url
            plain = (ex, res.url)
        except FetchError as e:
            if mode == "never":
                raise
            first_error, plain = e, None
    else:
        plain = None
    browser = find_browser(settings.browser_path)
    if not browser:
        if plain:
            return plain
        raise FetchError(f"{first_error} Install Google Chrome, Edge, Brave or Firefox so "
                         "PriceWatch can fetch it with a real browser." if first_error else
                         "PW_BROWSER_FETCH=always but no Chrome/Edge/Brave/Chromium/Firefox was found.")
    log.info("using headless browser for %s", url.split("?")[0])
    try:
        res = browser_get(url, browser=browser, user_agent=settings.user_agent,
                          allow_private=settings.allow_private_hosts)
    except FetchError as e:
        if plain:
            return plain
        raise FetchError(f"{first_error or ''} {e}".strip()) from None
    ex = extract(res.text)
    ex.method = f"{ex.method}+browser"
    return _shopify_variant(settings, res.url, res.text, ex), res.url


_SHOPIFY_PATH = re.compile(r"^((?:/[a-z]{2}(?:-[a-z]{2})?)?(?:/collections/[^/]+)?/products/([^/?#]+))/?$", re.I)
_SHOPIFY_MARK = re.compile(r"cdn\.shopify\.com|shopify\.(?:shop|theme)|myshopify\.com|shopify-section", re.I)


def _shopify_variant(settings: Settings, url: str, html: str, ex: Extracted) -> Extracted:
    """Shopify shops put the product's *lowest* variant price in their page data, so a link to a
    bigger size (…/products/x?variant=123) reads too low. Shopify also publishes every variant's
    own price at /products/<handle>.js: use the one the link selects (or the shop's default)."""
    p = urlsplit(url)
    m = _SHOPIFY_PATH.match(p.path)
    if not m or not _SHOPIFY_MARK.search(html[:400_000]):
        return ex
    js_url = urlunsplit((p.scheme, p.netloc, f"/products/{m.group(2)}.js", "", ""))
    try:
        res = safe_get(js_url, user_agent=settings.user_agent, timeout=settings.fetch_timeout,
                       allow_private=settings.allow_private_hosts)
        data = json.loads(res.text)
        variants = [v for v in data.get("variants", []) if isinstance(v, dict)]
    except (FetchError, ValueError, AttributeError):
        return ex
    if not variants:
        return ex
    want = (parse_qs(p.query).get("variant") or [""])[0]
    chosen = next((v for v in variants if str(v.get("id")) == want), None)
    if chosen is None:
        if want:   # a variant the shop no longer has: don't guess
            return ex
        chosen = next((v for v in variants if v.get("available")), variants[0])
    raw = chosen.get("price")
    try:   # /products/x.js gives pence as an integer
        price = (Decimal(raw) / 100) if isinstance(raw, int) else Decimal(str(raw))
    except (ArithmeticError, ValueError, TypeError):
        return ex
    if price <= 0:
        return ex
    title = ex.title or data.get("title")
    vt = str(chosen.get("title") or "")
    if len(variants) > 1 and vt and vt.lower() != "default title" and title and vt not in title:
        title = f"{title} ({vt})"[:300]
    return Extracted(title=title, price=price, currency=ex.currency, method="shopify",
                     in_stock=chosen.get("available") if isinstance(chosen.get("available"), bool) else ex.in_stock,
                     context=f"Shopify variant {chosen.get('id')}: {vt}"[:200] if vt else None)


def check_item(settings: Settings, db: DB, item) -> None:
    if _is_manual(item):   # you type this price in; the shop blocks automatic checks
        return
    try:
        ex, _ = fetch_price(settings, item["url"])
    except FetchError as e:
        db.record_check(item["id"], error=str(e))
        log.info("check %s failed: %s", item["id"], e)
        return
    if ex.price is None:
        db.record_check(item["id"], error="No price found on the page (it may need JavaScript).")
        return
    if _hold_doubtful(db, item, ex):
        return
    db.record_check(item["id"], price=ex.price, currency=ex.currency, title=ex.title,
                    method=ex.method, in_stock=ex.in_stock)
    log.info("check %s: %s %s", item["id"], ex.price, ex.currency or "")
    _review_alternative(db, item, ex)


def _is_manual(item) -> bool:
    return "manual" in item.keys() and bool(item["manual"])


HOLD_LOW, HOLD_HIGH = Decimal("0.6"), Decimal("2")


def _hold_doubtful(db: DB, item, ex) -> bool:
    """Your own products: a big jump or drop read only from page text is probably the wrong
    figure (insurance, instalments, another variant...). Keep the old price and ask."""
    if item["parent_id"] or ex.reliable or not item["current_price"]:
        return False
    cur, new = Decimal(item["current_price"]), Decimal(str(ex.price))
    if cur <= 0 or HOLD_LOW <= new / cur <= HOLD_HIGH:
        return False
    from .util import fmt_price
    ctx = f' (found in: "{ex.context}")' if ex.context else ""
    note = (f"Read {fmt_price(new, ex.currency or item['currency'])} from the page text{ctx}, far from the "
            f"previous {fmt_price(cur, item['currency'])}. Kept the previous price until you check.")
    db.hold_price(item["id"], new, note)
    log.info("check %s: held doubtful %s (was %s)", item["id"], new, cur)
    return True


def _review_alternative(db: DB, item, ex) -> None:
    """After each check of another shop's listing, re-judge whether its price can be trusted
    (e.g. O2 still reporting £20 after the link was corrected). Only you can clear a flag:
    'confirmed' and 'wrong' are left alone."""
    if not item["parent_id"]:
        return
    current = db.get_item(item["id"])
    if not current or (current["review"] or "ok") in ("confirmed", "wrong"):
        return
    from .review import offer_mismatch, price_doubt
    parent = db.get_item(item["parent_id"])
    doubt = (offer_mismatch(parent["title"], parent["url"], current["title"] or ex.title, current["url"])
             if parent else None)
    doubt = doubt or price_doubt(ex.price, parent["current_price"] if parent else None, ex.method)
    if doubt and current["review"] != "suspect":
        db.set_review(item["id"], "suspect", doubt)


class Scheduler(threading.Thread):
    """Wakes every few seconds, checks whatever is due. Daemon thread."""

    def __init__(self, settings: Settings, db: DB, agent_runner=None, fair_runner=None):
        super().__init__(name="pricewatch-scheduler", daemon=True)
        self.s, self.db = settings, db
        self.agent_runner = agent_runner
        self.fair_runner = fair_runner
        self._last_fair_sweep = 0
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pw-check")
        self._inflight: set[int] = set()
        self._lock = threading.Lock()
        self._last_agent_sweep = now()

    def stop(self):
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run_one(self, item):
        try:
            check_item(self.s, self.db, item)
        except Exception:  # never let one bad page kill the scheduler
            log.exception("unexpected error checking item %s", item["id"])
            self.db.record_check(item["id"], error="Internal error while checking.")
        finally:
            with self._lock:
                self._inflight.discard(item["id"])

    def tick(self):
        for item in self.db.due_items():
            with self._lock:
                if item["id"] in self._inflight:
                    continue
                self._inflight.add(item["id"])
            self._pool.submit(self._run_one, item)
        # optional periodic AI search for better prices (v2)
        hrs = self.s.agent_interval_hours
        if self.agent_runner and hrs > 0 and now() - self._last_agent_sweep >= hrs * 3600:
            self._last_agent_sweep = now()
            for item in self.db.q("SELECT id FROM items WHERE parent_id IS NULL AND active=1"):
                self.agent_runner(item["id"])
        # v4 fair-price persona checks (first sweep shortly after start-up)
        fh = self.s.fair_interval_hours
        if self.fair_runner and fh > 0 and now() - self._last_fair_sweep >= fh * 3600:
            self._last_fair_sweep = now()
            for item in self.db.q("SELECT id FROM items WHERE parent_id IS NULL AND active=1 AND manual=0"):
                self.fair_runner(item["id"])

    def run(self):
        log.info("scheduler started (tick=%ss)", self.s.scheduler_tick_seconds)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(self.s.scheduler_tick_seconds)


def wait_idle(sched: Scheduler, timeout=10.0):
    """Test helper: wait until no checks are in flight."""
    end = time.time() + timeout
    while time.time() < end:
        with sched._lock:
            if not sched._inflight:
                return True
        time.sleep(0.05)
    return False
