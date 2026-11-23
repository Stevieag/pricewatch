"""v4 - Fair Price: detect personalised / surveillance pricing and surge pricing.

Three independent signals, each explainable to the user:

1. PERSONALISED / SURVEILLANCE PRICING
   The same page is fetched as several *anonymous* shoppers - no cookies, no
   login, no history, different devices and languages (and optionally different
   network exits via PW_FAIR_PROXIES). Their median is the "anonymous baseline".
   The price YOU were shown (captured in your own browser by the bookmarklet, or
   typed in) is compared against it. Seen > baseline  => you may be profiled.

   On shops with strong bot protection (plain requests blocked), devices can't be
   imitated: a browser claiming to be something else is detected. The baseline then
   comes from honest anonymous visits in the installed browser's real identity
   (fresh profile, English + German), and the device rows are marked "skipped".

2. PROFILE SPREAD
   If anonymous personas themselves get different prices (e.g. iPhone vs
   Windows), the shop prices by device/profile even without knowing who you are.

3. SURGE PRICING
   Current price vs the 30-day median of all checks. Sudden jumps well above
   the typical price are flagged, together with the historically cheapest
   weekday / time-of-day once there is enough data.

Privacy: the bookmarklet reads the price from the page in YOUR browser and sends
only {url, price} to your local PriceWatch. No cookies or account data leave the
shop page. Persona fetches never carry cookies.
"""
from __future__ import annotations

import statistics
import threading
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .config import Settings
from .db import DB, now
from .extract import extract
from .fetcher import FetchError, browser_get, find_browser, safe_get
from .util import fmt_price

# Anonymous shopper profiles. Deliberately boring, common configurations.
PERSONAS = [
    {"id": "desktop-win-en", "label": "Windows · Chrome · English",
     "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
     "lang": "en-GB,en;q=0.9"},
    {"id": "mac-safari-en", "label": "Mac · Safari · English",
     "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
     "lang": "en-GB,en;q=0.9"},
    {"id": "iphone-safari", "label": "iPhone · Safari",
     "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
     "lang": "en-GB,en;q=0.9"},
    {"id": "android-chrome", "label": "Android · Chrome",
     "ua": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36",
     "lang": "en-GB,en;q=0.9"},
    {"id": "linux-firefox-de", "label": "Linux · Firefox · German",
     "ua": "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
     "lang": "de-DE,de;q=0.9,en;q=0.5"},
]

def record_seen(db: DB, item_id: int, price: Decimal, currency: str | None) -> None:
    """Store the price the user personally saw (from the bookmarklet / form)."""
    with db.tx() as c:
        c.execute("INSERT INTO fair_checks(item_id,persona,price,currency,checked_at) VALUES(?,?,?,?,?)",
                  (item_id, "you", str(price), currency, now()))


def run_personas(s: Settings, db: DB, item_id: int) -> int:
    item = db.get_item(item_id)
    if not item:
        return 0
    profiles = list(PERSONAS)
    for i, _ in enumerate(s.fair_proxies.reveal().split(",") if s.fair_proxies else []):
        profiles.append({**PERSONAS[0], "id": f"proxy:{i + 1}", "label": f"Network exit #{i + 1}", "proxy_index": i})
    t = now()
    rows = []
    blocked = 0
    for p in profiles:
        price = cur = err = None
        try:
            if "proxy_index" in p:
                text = _fetch_via_proxy(s, item["url"], p)
            elif s.browser_fetch == "always":
                raise FetchError("browser forced")
            else:
                text = safe_get(item["url"], user_agent=p["ua"], timeout=s.fetch_timeout,
                                allow_private=s.allow_private_hosts, accept_language=p["lang"]).text
            ex = extract(text)
            price, cur = ex.price, ex.currency
            err = None if price is not None else "no price found"
        except FetchError as e:
            err = str(e)
            blocked += "proxy_index" not in p
        rows.append([item_id, p["id"], str(price) if price is not None else None, cur, err, t])
        time.sleep(0.5)  # be gentle with the shop

    # Shop blocks plain requests? Device imitation is then impossible: bot protection spots a
    # browser that claims to be something it isn't. Fall back to honest anonymous visits in the
    # installed browser's REAL identity, varying only the language, spaced out.
    browser = find_browser(s.browser_path) if s.browser_fetch != "never" else None
    if blocked and browser:
        for r in rows:
            if r[2] is None and not str(r[1]).startswith("proxy:"):
                r[4] = "skipped: this shop blocks device imitation (bot protection)"
        for i, lang in enumerate(BROWSER_LANGS):
            if i:
                time.sleep(BROWSER_SPACING_SECONDS)
            price = cur = err = None
            try:
                ex = extract(browser_get(item["url"], browser=browser, user_agent=s.user_agent,
                                         allow_private=s.allow_private_hosts, accept_language=lang[1]).text)
                price, cur = ex.price, ex.currency
                err = None if price is not None else "no price found"
            except FetchError as e:
                err = str(e)
            rows.append([item_id, f"browser:{lang[0]}", str(price) if price is not None else None, cur, err, t])
    rows = [tuple(r[:4]) + ((r[4][:200] if r[4] else None), r[5]) for r in rows]
    with db.tx() as c:
        c.executemany("INSERT INTO fair_checks(item_id,persona,price,currency,error,checked_at) "
                      "VALUES(?,?,?,?,?,?)", rows)
    return sum(1 for r in rows if r[2] is not None)


def _fetch_via_proxy(s: Settings, url: str, p: dict) -> str:
    """Fetch through a configured proxy (e.g. a VPN exit). Proxy URLs may contain
    credentials, so they live in a Secret and are never shown or logged."""
    from .fetcher import check_url_allowed
    check_url_allowed(url, s.allow_private_hosts)
    proxy = s.fair_proxies.reveal().split(",")[p["proxy_index"]].strip()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    req = urllib.request.Request(url, headers={"User-Agent": p["ua"], "Accept-Language": p["lang"]})
    try:
        with opener.open(req, timeout=s.fetch_timeout) as r:
            return r.read(5 * 1024 * 1024).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except Exception as err:
        raise FetchError(f"proxy fetch failed ({type(err).__name__})") from None  # no proxy URL in message


BROWSER_LANGS = [("en", "en-GB,en;q=0.9"), ("de", "de-DE,de;q=0.9,en;q=0.5")]
BROWSER_SPACING_SECONDS = 8


def persona_label(pid: str) -> str:
    if pid == "you":
        return "You (your browser)"
    if pid.startswith("browser:"):
        return {"en": "Anonymous browser · English", "de": "Anonymous browser · German"}.get(pid[8:], pid)
    for p in PERSONAS:
        if p["id"] == pid:
            return p["label"]
    return pid.replace("proxy:", "Network exit #")


@dataclass
class Verdict:
    level: str      # good | warn | bad | info
    title: str
    detail: str


@dataclass
class FairReport:
    baseline: Decimal | None           # median anonymous price, latest round
    fair_price: Decimal | None         # median of anonymous prices over 30 days
    latest: list[tuple[str, Decimal | None, str | None]]  # (label, price, error)
    seen: Decimal | None
    verdicts: list[Verdict]
    cheapest_when: str | None
    last_run: int | None


def _median(vals):
    return Decimal(str(statistics.median(vals))) if vals else None


def report(s: Settings, db: DB, item_id: int) -> FairReport:
    item = db.get_item(item_id)
    cur = item["currency"]
    rows = db.q("SELECT * FROM fair_checks WHERE item_id=? ORDER BY checked_at DESC", (item_id,))
    anon = [r for r in rows if r["persona"] != "you"]
    last_run = anon[0]["checked_at"] if anon else None
    latest_rows = [r for r in anon if r["checked_at"] == last_run] if last_run else []
    same_cur = lambda r: r["price"] is not None and (not cur or not r["currency"] or r["currency"] == cur)  # noqa
    latest_prices = [Decimal(r["price"]) for r in latest_rows if same_cur(r)]
    baseline = _median(latest_prices)

    month = now() - 30 * 86400
    anon_30 = [Decimal(r["price"]) for r in anon if r["checked_at"] >= month and same_cur(r)]
    hist_30 = [Decimal(r["price"]) for r in db.history(item_id, month)]
    fair_price = _median(anon_30) or _median(hist_30)

    seen_row = next((r for r in rows if r["persona"] == "you" and r["price"]), None)
    seen = Decimal(seen_row["price"]) if seen_row else None
    pct = s.fair_threshold_pct
    m = lambda x: fmt_price(x, cur)  # noqa: E731
    v: list[Verdict] = []

    # 1. personalised / surveillance pricing
    if seen is not None and baseline:
        diff = (seen - baseline) / baseline * 100
        if diff > pct:
            v.append(Verdict("bad", "Possible personalised pricing",
                             f"You were shown {m(seen)}, but anonymous shoppers got {m(baseline)} "
                             f"({diff:+.1f}%). Try a private window, logging out, or clearing cookies."))
        elif diff < -pct:
            v.append(Verdict("good", "You're getting a better-than-public price",
                             f"You saw {m(seen)} vs anonymous {m(baseline)} ({diff:+.1f}%)."))
        else:
            v.append(Verdict("good", "No sign of personalised pricing",
                             f"The price you saw ({m(seen)}) matches anonymous shoppers ({m(baseline)})."))
    elif baseline:
        v.append(Verdict("info", "Capture the price you see",
                         "Use the bookmarklet on this product while signed in, so PriceWatch can compare "
                         "your price with anonymous shoppers."))

    # 2. spread between anonymous profiles
    if len(latest_prices) >= 2:
        lo, hi = min(latest_prices), max(latest_prices)
        if lo and (hi - lo) / lo * 100 > pct:
            by = sorted(((persona_label(r["persona"]), Decimal(r["price"])) for r in latest_rows if same_cur(r)),
                        key=lambda x: x[1])
            v.append(Verdict("warn", "Price changes by device or profile",
                             f"Cheapest as {by[0][0]} ({m(by[0][1])}), dearest as {by[-1][0]} ({m(by[-1][1])})."))

    # 3. surge pricing
    current = Decimal(item["current_price"]) if item["current_price"] else None
    typical = _median(hist_30)
    if current is not None and typical and len(hist_30) >= 5:
        diff = (current - typical) / typical * 100
        if diff >= s.surge_threshold_pct:
            v.append(Verdict("bad", "Surge: price is above its usual level",
                             f"Now {m(current)}, typical (30-day median) {m(typical)} ({diff:+.1f}%). "
                             "Consider waiting - surges usually fall back."))
        elif diff <= -pct:
            v.append(Verdict("good", "Below the usual price",
                             f"Now {m(current)} vs typical {m(typical)} ({diff:+.1f}%)."))

    return FairReport(baseline, fair_price,
                      [(persona_label(r["persona"]), Decimal(r["price"]) if r["price"] else None, r["error"])
                       for r in latest_rows],
                      seen, v, cheapest_time(db, item_id), last_run)


def cheapest_time(db: DB, item_id: int) -> str | None:
    """Which weekday / part of day has historically been cheapest (needs data)."""
    hist = db.history(item_id, now() - 90 * 86400)
    if len(hist) < 20:
        return None
    by_day, by_part = defaultdict(list), defaultdict(list)
    parts = [(0, "night (00-06)"), (6, "morning (06-12)"), (12, "afternoon (12-18)"), (18, "evening (18-24)")]
    for r in hist:
        lt = time.localtime(r["checked_at"])
        by_day[time.strftime("%A", lt)].append(float(r["price"]))
        by_part[[n for h, n in parts if lt.tm_hour >= h][-1]].append(float(r["price"]))
    avg = lambda d: {k: sum(v) / len(v) for k, v in d.items() if len(v) >= 3}  # noqa
    d, p = avg(by_day), avg(by_part)
    if len(d) < 2 and len(p) < 2:
        return None
    out = []
    if len(d) >= 2 and max(d.values()) > min(d.values()):
        out.append(f"cheapest on {min(d, key=d.get)}s")
    if len(p) >= 2 and max(p.values()) > min(p.values()):
        out.append(f"cheapest in the {min(p, key=p.get)}")
    return ", ".join(out) or None


class FairRunner:
    def __init__(self, s: Settings, db: DB):
        self.s, self.db = s, db
        self._running: set[int] = set()
        self._lock = threading.Lock()

    def is_running(self, iid: int) -> bool:
        with self._lock:
            return iid in self._running

    def run_in_background(self, iid: int) -> bool:
        with self._lock:
            if iid in self._running:
                return False
            self._running.add(iid)

        def work():
            try:
                run_personas(self.s, self.db, iid)
            finally:
                with self._lock:
                    self._running.discard(iid)
        threading.Thread(target=work, daemon=True, name=f"pw-fair-{iid}").start()
        return True
