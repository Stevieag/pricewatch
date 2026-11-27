# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Local web UI + small JSON API, stdlib only (http.server).

Hardening for a "localhost" app (localhost is NOT automatically safe):
  * Host-header allowlist      -> blocks DNS-rebinding attacks from web pages
  * CSRF token on every form   -> other sites can't POST to your local server
  * Strict CSP, no inline JS   -> injected markup can't run scripts
  * Optional access token      -> required when bound to anything but loopback
  * Request logs omit query strings (shared links can carry session tokens)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import re
import secrets
import time
from decimal import Decimal
from html import escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from . import __version__, chart, fair
from .agent import PriceAgent
from .extract import RELIABLE_METHODS
from .config import Settings
from .db import DB, now
from .fetcher import FetchError
from .monitor import fetch_price
from .util import (FORM_UNITS, clean_url, domain_of, extract_url, fmt_price, human_interval,
                   parse_frequency, parse_price)

log = logging.getLogger("pricewatch.web")
STATIC = Path(__file__).parent / "static"
SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥", "INR": "₹"}
RANGES = {"7d": 7, "30d": 30, "90d": 90, "1y": 365, "all": None}
MAX_BODY = 64 * 1024


def e(v) -> str:
    return escape("" if v is None else str(v), quote=True)


def ago(ts) -> str:
    if not ts:
        return "never"
    d = now() - int(ts)
    future = d < 0
    d = abs(d)
    for size, unit in ((31536000, "y"), (86400, "d"), (3600, "h"), (60, "min")):
        if d >= size:
            s = f"{d // size} {unit}"
            break
    else:
        s = "moments" if not future else "<1 min"
    return f"in {s}" if future else f"{s} ago"


def _learn_params(old_url: str, new_url: str) -> str | None:
    """If the corrected link is the same page plus extra options (e.g. ?paymentTab=pay-in-full),
    return those options so they can be added to this shop's links in future."""
    from urllib.parse import parse_qsl
    o, n = urlsplit(old_url), urlsplit(new_url)
    if (o.netloc, o.path.rstrip("/")) != (n.netloc, n.path.rstrip("/")):
        return None
    old_q = dict(parse_qsl(o.query, keep_blank_values=True))
    added = {k: v for k, v in parse_qsl(n.query, keep_blank_values=True) if old_q.get(k) != v}
    return urlencode(added) if added else None


class App:
    def __init__(self, settings: Settings, db: DB, agent: PriceAgent, fair_runner=None):
        self.s, self.db, self.agent = settings, db, agent
        self.fair = fair_runner or fair.FairRunner(settings, db)
        self._csrf_key = secrets.token_bytes(32)  # per-process; never stored
        self.allowed_hosts = {"127.0.0.1", "localhost", "[::1]", settings.host, *settings.allowed_hosts}

    # ------------------------------------------------------------ security helpers
    def csrf_token(self) -> str:
        return hmac.new(self._csrf_key, b"csrf", hashlib.sha256).hexdigest()

    def session_value(self) -> str:
        return hmac.new(self.s.access_token.reveal().encode(), b"pricewatch-session",
                        hashlib.sha256).hexdigest()

    def host_ok(self, host_header: str | None) -> bool:
        if not host_header:
            return False
        host = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
        return host.lower() in {h.lower() for h in self.allowed_hosts}

    def is_local_host(self, host_header: str | None) -> bool:
        host = (host_header or "").rsplit(":", 1)[0] if not (host_header or "").endswith("]") else host_header
        return (host or "").lower() in {"127.0.0.1", "localhost", "[::1]"}

    def bearer_ok(self, auth_header: str | None) -> bool:
        if not self.s.access_token or not auth_header or not auth_header.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth_header[7:].strip(), self.s.access_token.reveal())

    def cookie_ok(self, cookie_header: str | None) -> bool:
        if not self.s.access_token:
            return True
        c = SimpleCookie()
        try:
            c.load(cookie_header or "")
        except Exception:
            return False
        v = c.get("pw_auth")
        return bool(v) and hmac.compare_digest(v.value, self.session_value())


# ======================================================================= HTML

def layout(app: App, title: str, body: str, flash: str = "", auto_refresh: int = 0) -> str:
    fl = f'<div class="flash">{e(flash)}</div>' if flash else ""
    meta_refresh = f'<meta http-equiv="refresh" content="{int(auto_refresh)}">' if auto_refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta_refresh}
<title>{e(title)} · PriceWatch</title>
<link rel="stylesheet" href="/static/app.css"><link rel="manifest" href="/manifest.webmanifest">
<script src="/static/app.js" defer></script></head><body>
<header><a class="brand" href="/">PriceWatch</a>
<nav><a href="/">Watchlist</a><a href="/search">Search</a><a href="/share">Share setup</a></nav></header>
<main>{fl}{body}</main>
<footer>PriceWatch v{__version__} · runs locally · free software under the
<a href="https://www.gnu.org/licenses/agpl-3.0.html" rel="noopener noreferrer">AGPL-3.0</a> ·
<a href="{e(app.s.source_url)}" rel="noopener noreferrer">Source code</a></footer>
</body></html>"""


def csrf_field(app: App) -> str:
    return f'<input type="hidden" name="csrf" value="{app.csrf_token()}">'


def freq_inputs(minutes: int = 1440) -> str:
    qty, unit = minutes, 1
    for _, size in reversed(FORM_UNITS):
        if minutes % size == 0:
            qty, unit = minutes // size, size
            break
    opts = "".join(f'<option value="{size}"{" selected" if size == unit else ""}>{name}</option>'
                   for name, size in FORM_UNITS)
    return (f'<span class="freq">every <input type="number" name="every" min="1" value="{qty}" '
            f'required aria-label="How often"> <select name="unit" aria-label="Unit">{opts}</select></span>')


def cheaper_flag(price, than, currency) -> str:
    """Green flag for a price below the product's own: '⚑ £5.00 cheaper (7%)'. '' otherwise."""
    if price is None or not than:
        return ""
    p, ref = Decimal(str(price)), Decimal(str(than))
    if p >= ref or ref <= 0:
        return ""
    saving = ref - p
    return (f'<span class="flag-cheaper" title="Cheaper than the shop you are watching">'
            f'⚑ {e(fmt_price(saving, currency))} cheaper ({saving / ref * 100:.0f}%)</span>')


def alert_status(it) -> tuple[str, str]:
    """(kind, text) for a watchlist row's alert price: kind is hit | near | wait | none."""
    if not it["target_price"]:
        return "none", ""
    target = Decimal(it["target_price"])
    prices = [Decimal(p) for p in (it["current_price"],) if p]
    if it["best_alt_price"] is not None:
        prices.append(Decimal(str(it["best_alt_price"])))
    if not prices:
        return "wait", "waiting for a price"
    best = min(prices)
    if best <= target:
        where = "here" if it["current_price"] and Decimal(it["current_price"]) <= target else "at another shop"
        return "hit", f"✓ hit {where}"
    gap = best - target
    pct = gap / best * 100
    return ("near" if pct <= 5 else "wait"), f"{fmt_price(gap, it['currency'])} to go ({pct:.0f}%)"


def page_index(app: App, flash="") -> str:
    rows = []
    hits = []
    items = app.db.top_items()
    for it in items:
        cur = it["current_price"]
        change = ""
        if cur and it["first_price"]:
            d = Decimal(cur) - Decimal(it["first_price"])
            if d:
                cls = "down" if d < 0 else "up"
                change = f'<span class="{cls}">{"▼" if d < 0 else "▲"} {fmt_price(abs(d), it["currency"])}</span>'
        best = ""
        flag = ""
        if it["best_alt_price"] is not None:
            b = Decimal(str(it["best_alt_price"]))
            flag = cheaper_flag(b, cur, it["currency"])
            best = (f'<a href="{e(it["best_alt_url"])}" target="_blank" rel="noopener noreferrer" '
                    f'title="Open this shop">{e(fmt_price(b, it["currency"]))} ↗</a>'
                    f'{f"<div>{flag}</div>" if flag else ""}'
                    f'<div class="muted">at {e(domain_of(it["best_alt_url"]))}</div>')
        kind, alert_text = alert_status(it)
        if kind == "hit":
            hits.append(it)
        alert_cell = f"""<form method="post" action="/items/{it['id']}/target" class="alert-form">{csrf_field(app)}
<input type="text" name="target" inputmode="decimal" value="{e(it['target_price'] or '')}"
 placeholder="none" aria-label="Alert price for {e(it['title'] or domain_of(it['url']))}" size="7">
<button class="small">Set</button></form>{f'<div class="alert-{kind}">{e(alert_text)}</div>' if alert_text else ''}"""
        status = (f'<span class="badge warn" title="{e(it["last_error"])}">⚠ check failed</span>'
                  if it["last_error"] else e(ago(it["last_checked_at"])))
        if it["manual"]:
            status = f"typed by you {e(ago(it['last_checked_at']))}"
        if it["pending_price"] is not None:
            status = f'<a class="badge warn" href="/items/{it["id"]}" title="{e(it["review_note"])}">⚠ check price</a>'
        rows.append(f"""<tr{' class="row-cheaper"' if flag else ''}>
<td><a href="/items/{it['id']}">{e(it['title'] or domain_of(it['url']))}</a>
  <div class="muted">{e(domain_of(it['url']))}{'' if it['active'] else ' · paused'}</div></td>
<td class="num">{e(fmt_price(cur, it['currency']))}</td><td class="num">{change}</td>
<td class="num">{best}<div class="muted">{it['alt_count']} alt.{f' · <a href="/items/{it["id"]}">⚠ {it["review_count"]} to check</a>' if it["review_count"] else ''}</div></td>
<td class="alert-col">{alert_cell}</td>
<td>{e(human_interval(it['interval_minutes']))}<div class="muted">{status}</div></td></tr>""")
    table = (f"""<table class="list"><thead><tr><th>Product</th><th class="num">Price</th>
<th class="num">Change</th><th class="num">Best elsewhere</th><th>Alert at or below</th><th>Checks</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>""" if rows else
             '<p class="empty">Nothing watched yet. Paste a product link above, or set up sharing.</p>')

    banner = ""
    if hits:
        def hit_line(h):
            here = Decimal(h["current_price"]) if h["current_price"] else None
            alt = Decimal(str(h["best_alt_price"])) if h["best_alt_price"] is not None else None
            price, url = (alt, h["best_alt_url"]) if alt is not None and (here is None or alt < here) else (here, h["url"])
            return (f'<li><a href="/items/{h["id"]}">{e(h["title"] or domain_of(h["url"]))}</a>: '
                    f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(fmt_price(price, h["currency"]))} '
                    f'at {e(domain_of(url))} ↗</a> (alert {e(fmt_price(h["target_price"], h["currency"]))})</li>')
        links = "".join(hit_line(h) for h in hits)
        banner = (f'<section class="card alert-banner" role="status"><h2>🔔 {len(hits)} '
                  f'price{"s" if len(hits) != 1 else ""} at or below your alert</h2><ul>{links}</ul></section>')

    pending = app.db.pending_count()
    refresh = ""
    if items:
        refresh = (f'<form method="post" action="/refresh-all" class="row">{csrf_field(app)}'
                   f'<button{" disabled" if pending else ""}>'
                   f'{f"Checking {pending} listing(s)…" if pending else "↻ Refresh all prices"}</button>'
                   + ('<span class="muted">This page updates itself until the checks finish.</span>' if pending else '')
                   + '</form>')
    body = f"""{banner}<section class="card"><h1>Watch a price</h1>
<form method="get" action="/add" class="row">
<input type="text" name="text" placeholder="Paste a product link, or type a product to search for" required autofocus
 aria-label="Product link or search">
<button>Go</button></form>
<p class="muted">{'A link looks up its price. Words search the shops via your private SearXNG.' if app.agent.available
                   else 'Paste a product link. (Searching by name needs SearXNG: run python3 -m pricewatch doctor.)'}</p></section>
<section><div class="row spread"><h2>Watchlist</h2>{refresh}</div>{table}</section>"""
    return layout(app, "Watchlist", body, flash, auto_refresh=10 if pending else 0)


def page_search(app: App, q: str) -> str:
    """Search shops by product name via SearXNG; each result can be watched in one click."""
    from .agent import AgentError, searxng_results
    from .review import looks_like_listing, not_a_shop
    q = " ".join((q or "").split())[:150]
    form = f"""<form method="get" action="/search" class="row">
<input type="text" name="q" value="{e(q)}" placeholder="e.g. Black and Decker 1200W hover mower" required autofocus
 aria-label="Search for a product"><button>Search</button></form>"""
    if not q:
        return layout(app, "Search", f'<section class="card"><h1>Search for a product</h1>{form}</section>')
    if not app.agent.available:
        return layout(app, "Search", f"""<section class="card"><h1>Search for a product</h1>{form}
<p class="flash">Searching needs SearXNG. Run <code>python3 -m pricewatch doctor</code> for the steps,
or paste a product link on the watchlist instead.</p></section>""")
    try:
        results = searxng_results(app.s, q, 40)
    except AgentError as err:
        return layout(app, "Search", f'<section class="card"><h1>Search for a product</h1>{form}'
                                     f'<p class="flash">{e(err)}</p></section>')
    shown, hidden, per_domain = [], 0, {}
    for r in results:
        if not_a_shop(r["url"]) or looks_like_listing(r["url"]):
            hidden += 1
            continue
        d = domain_of(r["url"])
        if per_domain.get(d, 0) >= 2:  # keep the list varied: at most 2 per shop
            continue
        per_domain[d] = per_domain.get(d, 0) + 1
        shown.append(r)
        if len(shown) >= 15:
            break
    watched = {row["url"] for row in app.db.q("SELECT url FROM items")}
    rows = "".join(f"""<li class="result"><div class="result-main">
<a href="{e(r['url'])}" target="_blank" rel="noopener noreferrer"><b>{e(r['title'] or domain_of(r['url']))}</b> ↗</a>
<div class="muted">{e(domain_of(r['url']))}</div>
{f'<div class="snippet">{e(r["content"])}</div>' if r['content'] else ''}</div>
<div>{'<span class="badge good">watching</span>' if r['url'] in watched else
      f'<a class="pill on" href="/add?{e(urlencode({"text": r["url"]}))}">Watch this</a>'}</div></li>"""
                   for r in shown)
    note = (f'<p class="muted">Hid {hidden} result(s) that aren\'t shop product pages '
            '(social, news, reviews, price-comparison, search or category pages).</p>' if hidden else "")
    body = f"""<section class="card"><h1>Search for a product</h1>{form}</section>
<section class="card"><h2>{len(shown)} shop page(s) for “{e(q)}”</h2>{note}
{f'<ul class="results">{rows}</ul>' if shown else '<p class="empty">No shop pages found. Try fewer or different words, e.g. the model number.</p>'}
<p class="muted">“Watch this” reads the page's price and asks how often to check it. Prices aren't
fetched here, to keep searching quick.</p></section>"""
    return layout(app, f"Search: {q}", body)


def page_add(app: App, raw: str, seen: str = "", flash="") -> str:
    url = extract_url(raw)
    if not url:
        return page_index(app, "No link found in what you shared.")
    try:
        url = clean_url(url)
        ex, final = fetch_price(app.s, url)
        url = clean_url(final)
    except ValueError as err:
        return page_index(app, f"Couldn't read that page: {err}")
    except FetchError as err:
        return page_manual(app, url, flash=f"Couldn't read that page: {err}")
    found = (f'<p class="big">{e(fmt_price(ex.price, ex.currency))}</p>'
             f'<p class="muted">found via {e(ex.method)}</p>' if ex.price is not None else
             '<p class="flash">No price found on this page (it may be rendered with JavaScript). '
             'You can still watch it; PriceWatch will keep trying.</p>')
    seen_note = ""
    sp = parse_price(seen)
    if sp is not None and ex.price:
        diff = (sp - ex.price) / ex.price * 100
        if diff > app.s.fair_threshold_pct:
            seen_note = (f'<p class="flash bad">You were shown {e(fmt_price(sp, ex.currency))} but an anonymous, '
                         f'cookie-free visit got {e(fmt_price(ex.price, ex.currency))} ({diff:+.1f}%). '
                         'This may be personalised pricing.</p>')
        elif diff < -app.s.fair_threshold_pct:
            seen_note = f'<p class="muted">You were shown less than the anonymous price ({diff:+.1f}%).</p>'
        else:
            seen_note = '<p class="muted">The price you saw matches an anonymous visit.</p>'
    body = f"""<section class="card"><h1>{e(ex.title or domain_of(url))}</h1>
<p class="muted">{e(url)}</p>{found}
<form method="post" action="/items" class="stack">{csrf_field(app)}
<input type="hidden" name="url" value="{e(url)}">
<input type="hidden" name="title" value="{e(ex.title or '')}">
<input type="hidden" name="price" value="{e(ex.price if ex.price is not None else '')}">
<input type="hidden" name="currency" value="{e(ex.currency or '')}">
<input type="hidden" name="method" value="{e(ex.method)}">
<label>Check {freq_inputs()}</label>
<p class="muted">Anything from {app.s.min_interval_minutes} minutes to 1 year. Please be polite to shops: hourly or daily is usually plenty.</p>
<label>Alert me at or below (optional) <input type="text" name="target" inputmode="decimal" placeholder="e.g. 199.99"></label>
<label>Price <i>you</i> were shown (optional, for Fair Price)
<input type="text" name="seen" inputmode="decimal" value="{e(seen)}" placeholder="captured by the bookmarklet"></label>
{seen_note}
<button>Monitor this price</button></form></section>"""
    return layout(app, "Add", body, flash)


def _title_from_url(url: str) -> str:
    """'…/google-pixel-11-pro-xl-1-tb-obsidian-10307505.html' -> 'Google Pixel 11 Pro Xl 1 Tb Obsidian'."""
    seg = [p for p in urlsplit(url).path.split("/") if p]
    words = re.sub(r"\.[a-z]{2,5}$", "", seg[-1] if seg else "").replace("_", "-").split("-")
    words = [w for w in words if w and not (w.isdigit() and len(w) >= 5)]
    return " ".join(w.capitalize() for w in words)[:150]


def page_manual(app: App, url: str, parent: int | None = None, flash="") -> str:
    """Watch a link whose shop blocks automatic checks: you type the price you see."""
    tops = app.db.q("SELECT id, title, url, currency FROM items WHERE parent_id IS NULL ORDER BY title")
    opts = "".join(f'<option value="{t["id"]}"{" selected" if t["id"] == parent else ""}>'
                   f'{e((t["title"] or domain_of(t["url"]))[:80])}</option>' for t in tops)
    cur_opts = "".join(f'<option{" selected" if c == "GBP" else ""}>{c}</option>' for c in ("GBP", "EUR", "USD"))
    body = f"""<section class="card"><h1>Type the price yourself</h1>
<p class="muted">{e(url)}</p>
<p>{e(domain_of(url))} blocks automatic checks, so PriceWatch can't read this page. Open it in your
browser and type the price you see. It's saved in the price history and comparisons; update it on the
product page whenever you look again. PriceWatch won't check this link itself.</p>
<p><a href="{e(url)}" target="_blank" rel="noopener noreferrer">Open the shop's page ↗</a></p>
<form method="post" action="/items/manual" class="stack">{csrf_field(app)}
<input type="hidden" name="url" value="{e(url)}">
<label>Product name <input type="text" name="title" maxlength="200" value="{e(_title_from_url(url))}" required></label>
<label>Price <input type="text" name="price" inputmode="decimal" placeholder="e.g. 1139.99" required></label>
<label>Currency <select name="currency">{cur_opts}</select></label>
<label>Compare with
<select name="parent"><option value="">Nothing: watch it as its own product</option>{opts}</select></label>
<p class="muted">Pick the same product you already watch elsewhere to show this shop under "Other shops".</p>
<label>Alert me at or below (optional) <input type="text" name="target" inputmode="decimal" placeholder="e.g. 999.99"></label>
<button>Save this price</button></form></section>"""
    return layout(app, "Type the price", body, flash)


REVIEW_BADGES = {
    "suspect": '<span class="badge warn">⚠ check this price</span>',
    "wrong": '<span class="badge bad">✕ wrong price</span>',
    "confirmed": '<span class="badge good">✓ confirmed</span>',
}


def alt_row(app: App, a, parent_price=None) -> str:
    """One alternative shop: price, review status, and a feedback form for the AI."""
    review = a["review"] or "ok"
    note = f'<div class="review-note">{e(a["review_note"])}</div>' if a["review_note"] else ""
    if review == "wrong":
        note += ('<div class="muted">Not counted as a price. Give the right link below and it\'s checked '
                 'again, or remove it if this shop doesn\'t sell it outright.</div>')
    status = ('<span class="badge warn">⚠ check failed</span>' if a["last_error"]
              else e(ago(a["last_checked_at"])))
    how = "checked"
    typed = ""
    if a["manual"]:
        how, status = "price typed by you", e(ago(a["last_checked_at"]))
        typed = price_form(app, a, "Update")
    elif a["last_error"]:
        typed = (f'<a class="small" href="/add-manual?{e(urlencode({"url": a["url"], "parent": a["parent_id"]}))}">'
                 'Blocked? Type the price yourself</a>')
    open_attr = " open" if review in ("suspect", "wrong") else ""
    # only trusted prices get the green flag; flagged ones still need your check
    flag = cheaper_flag(a["current_price"], parent_price, a["currency"]) if review in ("ok", "confirmed") else ""
    return f"""<tr class="review-{e(review)}{' row-cheaper' if flag else ''}"><td><a href="{e(a['url'])}" rel="noopener noreferrer" target="_blank">
{e(a['title'] or domain_of(a['url']))} ↗</a> {REVIEW_BADGES.get(review, '')}
<div class="muted">{e(domain_of(a['url']))} · added by {e(a['source'])} · {how} {status}</div>{note}{typed}
<details class="feedback"{open_attr}><summary>Feedback for the AI</summary>
<form method="post" action="/items/{a['id']}/feedback" class="stack">{csrf_field(app)}
<label class="inline"><input type="radio" name="kind" value="correct" required> ✓ Correct: count this price</label>
<label class="inline"><input type="radio" name="kind" value="wrong_price"> £ Wrong price (e.g. monthly or deposit)</label>
<label class="inline"><input type="radio" name="kind" value="not_same"> ✕ Not the same product: remove it</label>
<label>Correct link for this shop (optional), e.g. the "pay in full" page
<input type="url" name="url" placeholder="{e(a['url'])}"></label>
<label>Note for the AI about {e(domain_of(a['url']))} (optional)
<input type="text" name="note" maxlength="300" placeholder="e.g. The first price shown is monthly; the full price is on the pay-in-full tab"></label>
<button class="small">Send feedback</button></form></details></td>
<td class="num">{e(fmt_price(a['current_price'], a['currency']))}{f"<div>{flag}</div>" if flag else ""}</td>
<td><form method="post" action="/items/{a['id']}/delete">{csrf_field(app)}<button class="link">remove</button></form></td></tr>"""


def price_form(app: App, it, label: str) -> str:
    return (f'<form method="post" action="/items/{it["id"]}/set-price" class="row">{csrf_field(app)}'
            f'<input type="text" name="price" inputmode="decimal" size="9" required '
            f'aria-label="New price" placeholder="{e(it["current_price"] or "")}">'
            f'<button class="small">{e(label)}</button></form>')


def _range_since(rng: str):
    days = RANGES.get(rng, 30)
    return 0 if days is None else now() - days * 86400


def page_item(app: App, item_id: int, rng: str = "30d", flash="") -> str | None:
    it = app.db.get_item(item_id)
    if not it:
        return None
    if it["parent_id"]:
        parent = it["parent_id"]
        return page_item(app, parent, rng, flash)
    alts = app.db.alternatives(item_id)
    since = _range_since(rng)
    sym = SYMBOLS.get((it["currency"] or "").upper(), "")

    series = [{"name": f"{domain_of(it['url'])} (original)",
               "points": [(r["checked_at"], float(r["price"])) for r in app.db.history(item_id, since)]}]
    for a in alts[: chart.MAX_SERIES - 1]:
        if (a["currency"] or "") == (it["currency"] or ""):
            series.append({"name": domain_of(a["url"]),
                           "points": [(r["checked_at"], float(r["price"])) for r in app.db.history(a["id"], since)]})
    series = [s for s in series if s["points"]]
    svg, payload = chart.render(series, sym)
    legend = "".join(f'<li><span class="key s{i + 1}"></span>{e(s["name"])}</li>' for i, s in enumerate(series))
    range_links = " ".join(
        f'<a class="pill{" on" if r == rng else ""}" href="/items/{item_id}?range={r}">{r}</a>' for r in RANGES)
    table_rows = "".join(
        f"<tr><td>{e(s['name'])}</td><td>{e(time.strftime('%Y-%m-%d %H:%M', time.localtime(t)))}</td>"
        f"<td class='num'>{e(fmt_price(v, it['currency']))}</td></tr>"
        for s in series for t, v in reversed(s["points"]))
    graph = (f"""<div class="chart-wrap" data-chart>{svg}<div class="tip" hidden></div></div>
<script type="application/json" class="chart-data">{payload}</script>
{'<ul class="legend">' + legend + '</ul>' if len(series) > 1 else ''}
<details><summary>Table view</summary><table class="list"><thead><tr><th>Shop</th><th>Checked</th>
<th class="num">Price</th></tr></thead><tbody>{table_rows}</tbody></table></details>"""
             if svg else '<p class="empty">No price history in this range yet.</p>')

    alt_rows = "".join(alt_row(app, a, it["current_price"]) for a in alts)
    skipped = app.db.skipped_for(item_id)
    skipped_html = ""
    if skipped:
        srows = "".join(f"""<tr><td><a href="{e(k['url'])}" rel="noopener noreferrer" target="_blank">
{e(k['title'] or domain_of(k['url']))} ↗</a><div class="muted">{e(domain_of(k['url']))} · {e(k['reason'])}</div></td>
<td class="num">{e(fmt_price(k['price'], k['currency']))}</td>
<td><form method="post" action="/items/{item_id}/add-skipped">{csrf_field(app)}
<input type="hidden" name="url" value="{e(k['url'])}"><button class="small">Add anyway</button></form></td></tr>"""
                        for k in skipped)
        skipped_html = (f'<details class="skipped"><summary>{len(skipped)} shop(s) the agent checked but '
                        f'didn\'t add - add any you think are right</summary><table class="list"><tbody>{srows}'
                        f'</tbody></table></details>')
    advice_rows = [a for a in app.db.all_advice()
                   if a["domain"] in {domain_of(x["url"]) for x in alts} | {domain_of(k["url"]) for k in skipped}]
    advice_html = ""
    if advice_rows:
        items_html = "".join(
            f'<li><b>{e(a["domain"])}</b>'
            + (f' · links get <code>?{e(a["add_params"])}</code>' if a["add_params"] else "")
            + (f'<div class="muted">{"<br>".join(e(n) for n in (a["advice"] or "").splitlines())}</div>' if a["advice"] else "")
            + f'<form method="post" action="/advice/clear" class="inline">{csrf_field(app)}'
              f'<input type="hidden" name="domain" value="{e(a["domain"])}">'
              f'<input type="hidden" name="back" value="/items/{item_id}"><button class="link">forget</button></form></li>'
            for a in advice_rows)
        advice_html = f'<div class="advice"><h3>What PriceWatch has learned from your feedback</h3><ul>{items_html}</ul></div>'
    run = app.db.last_run(item_id)
    running = app.agent.is_running(item_id)
    run_html = ""
    if run:
        run_html = (f'<p class="muted">Last search {e(ago(run["started_at"]))}: {e(run["status"])}</p>'
                    f'<pre class="log">{e(run["summary"] or "")}</pre>')
    agent_btn = (f'<form method="post" action="/items/{item_id}/search">{csrf_field(app)}'
                 f'<button {"disabled" if running or not app.agent.available else ""}>'
                 f'{"Searching…" if running else "Find better prices"}</button></form>')

    fair_html = render_fair(app, it)
    err = f'<p class="flash">Last check failed: {e(it["last_error"])}</p>' if it["last_error"] else ""
    if it["pending_price"] is not None:
        err += f"""<div class="verdict warn"><b>Check this price</b><p>{e(it['review_note'])}</p>
<div class="row"><form method="post" action="/items/{item_id}/accept-price">{csrf_field(app)}
<button>Use {e(fmt_price(it['pending_price'], it['currency']))}</button></form>
<form method="post" action="/items/{item_id}/keep-price">{csrf_field(app)}
<button class="link">Keep {e(fmt_price(it['current_price'], it['currency']))}</button></form>
<a href="{e(it['url'])}" target="_blank" rel="noopener noreferrer">Open the shop's page ↗</a></div></div>"""
    tgt = it["target_price"] or ""
    if it["manual"]:
        checks_line = f"· price typed by you {e(ago(it['last_checked_at']))} · not checked automatically"
        check_btn = price_form(app, it, "Update price")
    else:
        checks_line = (f"· checked every {e(human_interval(it['interval_minutes']))} · last {e(ago(it['last_checked_at']))}"
                       f" · next {e(ago(it['next_check_at']))}{'' if it['active'] else ' · <b>paused</b>'}")
        check_btn = f'<form method="post" action="/items/{item_id}/check">{csrf_field(app)}<button>Check now</button></form>'
        if it["last_error"]:
            check_btn += (f'<a href="/add-manual?{e(urlencode({"url": it["url"]}))}">'
                          'Blocked? Type the price yourself</a>')
    body = f"""<section class="card">
<h1>{e(it['title'] or domain_of(it['url']))}</h1>
<p><a href="{e(it['url'])}" rel="noopener noreferrer" target="_blank">{e(domain_of(it['url']))} ↗</a>
{checks_line}</p>
<p class="big">{e(fmt_price(it['current_price'], it['currency']))}</p>{err}
<div class="row">
{check_btn}
<form method="post" action="/items/{item_id}/delete">{csrf_field(app)}<button class="danger">Delete</button></form>
</div></section>

<section class="card"><h2>Price history</h2><div class="pills">{range_links}</div>{graph}</section>

{fair_html}

<section class="card"><h2>Other shops</h2>
<p class="muted">{e(app.agent.status_text())}</p>{agent_btn}{run_html}
{'<table class="list"><tbody>' + alt_rows + '</tbody></table>' if alts else '<p class="empty">No alternatives yet.</p>'}
{skipped_html}{advice_html}
<form method="post" action="/items/{item_id}/alternatives" class="row">{csrf_field(app)}
<input type="text" name="url" placeholder="Add another shop's link yourself" required><button>Add</button></form>
</section>

<section class="card"><h2>Settings</h2>
<form method="post" action="/items/{item_id}/update" class="stack">{csrf_field(app)}
<label>Check {freq_inputs(it['interval_minutes'])}</label>
<label>Alert at or below <input type="text" name="target" value="{e(tgt)}" inputmode="decimal"></label>
<label class="inline"><input type="checkbox" name="active" value="1"{' checked' if it['active'] else ''}> Monitoring active</label>
<button>Save</button></form></section>"""
    return layout(app, it["title"] or "Item", body, flash)


def render_fair(app: App, it) -> str:
    r = fair.report(app.s, app.db, it["id"])
    cur = it["currency"]
    running = app.fair.is_running(it["id"])
    verdicts = "".join(f'<div class="verdict {v.level}"><b>{e(v.title)}</b><p>{e(v.detail)}</p></div>'
                       for v in r.verdicts) or '<p class="empty">Not enough data yet.</p>'
    def evidence(method, ctx):
        if not method:
            return ""
        base = method.split("+")[0]
        sure = base in RELIABLE_METHODS
        tag = ('<span class="badge good">shop data</span>' if sure
               else '<span class="badge warn" title="Read from the page text; could be used, another seller or edition">page text</span>')
        return f'{tag}<div class="evidence">{e(ctx)}</div>' if ctx and (not sure or base == "shopify") else tag
    rows = "".join(f"<tr><td>{e(label)}</td><td class='num'>{e(fmt_price(p, cur)) if p is not None else ''}</td>"
                   f"<td class='muted'>{evidence(meth, ctx) if p is not None else e(err or '')}</td></tr>"
                   for label, p, err, meth, ctx in r.latest)
    if r.seen is not None:
        rows = f"<tr><td><b>You (your browser)</b></td><td class='num'><b>{e(fmt_price(r.seen, cur))}</b></td><td></td></tr>" + rows
    when = f'<p class="muted">Timing tip: {e(r.cheapest_when)}.</p>' if r.cheapest_when else ""
    return f"""<section class="card"><h2>Fair price</h2>
<div class="row stats"><div><span class="muted">Fair price (30-day anonymous median)</span>
<p class="big">{e(fmt_price(r.fair_price, cur))}</p></div>
<div><span class="muted">Anonymous baseline now</span><p class="big">{e(fmt_price(r.baseline, cur))}</p></div></div>
{verdicts}{when}
{'<table class="list"><thead><tr><th>Shopper profile</th><th class="num">Price</th><th>Where it was found</th></tr></thead><tbody>' + rows + '</tbody></table>' if rows else ''}
<p class="muted"><a href="{e(it['url'])}" target="_blank" rel="noopener noreferrer">Open this product page ↗</a> to compare yourself (try a private window, signed out).</p>
<p class="muted">Last anonymous check: {e(ago(r.last_run))}</p>
<div class="row">
<form method="post" action="/items/{it['id']}/fair">{csrf_field(app)}<button {"disabled" if running else ""}>{"Checking…" if running else "Check as anonymous shoppers"}</button></form>
<form method="post" action="/items/{it['id']}/seen" class="row">{csrf_field(app)}
<input type="text" name="seen" inputmode="decimal" placeholder="Price you were shown" required><button>Compare</button></form>
</div></section>"""


# Runs in YOUR browser on the shop page: reads the price you are being shown and
# sends only {url, price} to your local PriceWatch. No cookies leave the page.
BOOKMARKLET_JS = (
    "(()=>{let p='';try{const m=window.ShopifyAnalytics&&ShopifyAnalytics.meta,"
    "vid=new URLSearchParams(location.search).get('variant')||(m&&m.selectedVariantId),"
    "v=m&&m.product&&m.product.variants.find(x=>String(x.id)===String(vid));"
    "if(v&&v.price)p=(v.price/100).toFixed(2)}catch(e){}"
    "let link=location.href;try{const w=document.querySelector('form.variations_form');if(w){"
    "const q=new URL(location.href);w.querySelectorAll('select[name^=\"attribute_\"]')"
    ".forEach(s=>{if(s.value)q.searchParams.set(s.name,s.value)});link=q.href;"
    "const a=w.querySelector('.woocommerce-variation-price .amount,.single_variation .amount');"
    "if(a&&!p)p=a.textContent.replace(/[^0-9.,]/g,'').replace(/,/g,'')}}catch(e){}const f=o=>{if(!o||p)return;if(Array.isArray(o))return o.forEach(f);"
    "if(typeof o!=='object')return;const of=o.offers;if(of){const x=Array.isArray(of)?of[0]:of;"
    "p=(x&&(x.price||x.lowPrice))||''}if(!p&&o.price)p=o.price;f(o['@graph']);f(o.mainEntity)};"
    "document.querySelectorAll('script[type=\"application/ld+json\"]').forEach(s=>{try{f(JSON.parse(s.textContent))}catch(e){}});"
    "if(!p){const m=document.querySelector('meta[property=\"product:price:amount\"],meta[property=\"og:price:amount\"],[itemprop=price]');"
    "if(m)p=m.content||m.textContent||''}"
    "const u='__BASE__/add?text='+encodeURIComponent(link)+'&seen='+encodeURIComponent(String(p).trim());"
    "if(!window.open(u,'_blank'))location.href=u})()"
)


def page_share(app: App, base: str) -> str:
    bm = "javascript:" + BOOKMARKLET_JS.replace("__BASE__", base)
    body = f"""<section class="card"><h1>Share links into PriceWatch</h1>
<h2>Desktop: bookmarklet</h2>
<p>Drag this to your bookmarks bar, then click it on any product page:
<a class="pill on" href="{e(bm)}">+ PriceWatch</a></p>
<p class="muted">It opens PriceWatch in a new tab and leaves the shop's page where it was. (If your
browser blocks the new tab, it opens in the same tab instead.) Made the bookmark before
November 2026? Delete it and drag this one again to get the new-tab behaviour.</p>
<p class="muted">PriceWatch has to be running for the bookmarklet to work. To keep it ready, run
<code>python3 -m pricewatch autostart on</code> once and it starts whenever you log in.</p>
<p class="muted">It also captures the price <i>you</i> are being shown (while signed in), so Fair Price can
compare it with what anonymous shoppers get. Only the link and that price are sent, and only to this app.</p>
<h2>Android / desktop Chrome: share sheet</h2>
<p>Open PriceWatch in Chrome and choose <i>Install app</i>. It then appears in the system share sheet.</p>
<h2>iPhone: Shortcut</h2>
<p>See <code>docs/ios-shortcut.md</code>. The phone must reach this computer (LAN or Tailscale), so
set <code>PW_HOST</code>, <code>PW_ALLOWED_HOSTS</code> and <code>PW_ACCESS_TOKEN</code> first.
API status: <b>{'enabled' if app.s.access_token else 'disabled (no PW_ACCESS_TOKEN set)'}</b>.</p>
</section>"""
    return layout(app, "Share setup", body)


def page_login(app: App, flash="") -> str:
    body = f"""<section class="card"><h1>Sign in</h1><form method="post" action="/login" class="stack">
{csrf_field(app)}<label>Access token <input type="password" name="token" autocomplete="current-password" required></label>
<button>Sign in</button></form></section>"""
    return layout(app, "Sign in", body, flash)


MANIFEST = {
    "name": "PriceWatch", "short_name": "PriceWatch", "start_url": "/", "display": "standalone",
    "background_color": "#fcfcfb", "theme_color": "#2a78d6",
    "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    "share_target": {"action": "/add", "method": "GET",
                     "params": {"title": "title", "text": "text", "url": "url"}},
}


# ======================================================================= handler

def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PriceWatch"
        sys_version = ""

        # -- plumbing
        def log_message(self, fmt, *args):  # never log query strings
            log.info("%s %s -> %s", self.command, urlsplit(self.path).path, args[1] if len(args) > 1 else "")

        def _headers(self, status, ctype, extra=None, length=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                             "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def html(self, body: str, status=200, extra=None):
            b = body.encode()
            self._headers(status, "text/html; charset=utf-8", extra, len(b))
            self.wfile.write(b)

        def json(self, obj, status=200):
            b = json.dumps(obj, default=str).encode()
            self._headers(status, "application/json", length=len(b))
            self.wfile.write(b)

        def redirect(self, to: str, extra=None):
            self._headers(303, "text/plain", {"Location": to, **(extra or {})}, 0)

        def not_found(self):
            self.html(layout(app, "Not found", "<h1>Not found</h1>"), 404)

        def form(self) -> dict[str, str]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("Request too large")
            raw = self.rfile.read(n).decode("utf-8", errors="replace")
            if "json" in (self.headers.get("Content-Type") or ""):
                data = json.loads(raw or "{}")
                return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def gate(self, path: str) -> bool:
            """Host check + auth. Returns False if a response was already sent."""
            if not app.host_ok(self.headers.get("Host")):
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "Host not allowed")
                return False
            if not app.s.access_token and not app.is_local_host(self.headers.get("Host")):
                # reached through a proxy such as `tailscale serve` but no token is set
                self.send_error(HTTPStatus.FORBIDDEN, "Set PW_ACCESS_TOKEN to use PriceWatch from other devices")
                return False
            if path.startswith("/static/") or path in ("/login", "/manifest.webmanifest", "/healthz"):
                return True
            if path.startswith("/api/"):
                if app.bearer_ok(self.headers.get("Authorization")):
                    return True
                if self.command == "GET" and app.cookie_ok(self.headers.get("Cookie")) and app.s.is_loopback_bind():
                    return True
                self.json({"error": "unauthorised"}, 401)
                return False
            if app.cookie_ok(self.headers.get("Cookie")):
                return True
            self.redirect("/login")
            return False

        def csrf_ok(self, f: dict) -> bool:
            origin = self.headers.get("Origin")
            if origin and origin != "null" and not app.host_ok(urlsplit(origin).netloc):
                return False
            return hmac.compare_digest(f.get("csrf", ""), app.csrf_token())

        # -- GET
        def do_GET(self):
            u = urlsplit(self.path)
            path, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self.gate(path):
                return
            try:
                if path == "/":
                    return self.html(page_index(app, qs.get("msg", "")))
                if path == "/healthz":
                    return self.json({"ok": True})
                if path == "/add":
                    raw = " ".join(qs.get(k, "") for k in ("url", "text", "title"))
                    if not extract_url(raw) and raw.strip():  # words, not a link: search for it
                        return self.redirect("/search?" + urlencode({"q": raw.strip()[:150]}))
                    return self.html(page_add(app, raw, qs.get("seen", "")))
                if path == "/add-manual":
                    url = clean_url(extract_url(qs.get("url", "")) or "")
                    par = qs.get("parent", "")
                    return self.html(page_manual(app, url, int(par) if par.isdigit() else None))
                if path == "/search":
                    return self.html(page_search(app, qs.get("q", "")))
                if path == "/share":
                    base = f"http://{self.headers.get('Host')}"
                    return self.html(page_share(app, base))
                if path == "/login":
                    return self.html(page_login(app))
                if path.startswith("/items/"):
                    iid = int(path.split("/")[2])
                    page = page_item(app, iid, qs.get("range", "30d"), qs.get("msg", ""))
                    return self.html(page) if page else self.not_found()
                if path == "/manifest.webmanifest":
                    b = json.dumps(MANIFEST).encode()
                    self._headers(200, "application/manifest+json", length=len(b))
                    return self.wfile.write(b)
                if path.startswith("/static/"):
                    return self.static(path[8:])
                if path == "/api/items":
                    return self.json([dict(r) for r in app.db.top_items()])
                if path.startswith("/api/items/") and path.endswith("/history"):
                    iid = int(path.split("/")[3])
                    return self.json([dict(r) for r in app.db.history(iid)])
            except ValueError:
                pass
            self.not_found()

        def static(self, name: str):
            f = (STATIC / name).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self.not_found()
            b = f.read_bytes()
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self._headers(200, ctype, {"Cache-Control": "max-age=3600"}, len(b))
            self.wfile.write(b)

        # -- POST
        def do_POST(self):
            path = urlsplit(self.path).path
            if not self.gate(path):
                return
            try:
                f = self.form()
            except (ValueError, json.JSONDecodeError):
                return self.json({"error": "bad request"}, 400)

            if path == "/api/share":  # bearer-authenticated, used by the iOS Shortcut
                return self.api_share(f)

            if not self.csrf_ok(f):
                return self.html(layout(app, "Forbidden", "<h1>Form expired</h1><p>Reload and try again.</p>"), 403)

            if path == "/login":
                tok = f.get("token", "")
                if app.s.access_token and hmac.compare_digest(tok, app.s.access_token.reveal()):
                    return self.redirect("/", {"Set-Cookie": f"pw_auth={app.session_value()}; HttpOnly; "
                                                             f"SameSite=Strict; Path=/; Max-Age=2592000"})
                time.sleep(1)
                return self.html(page_login(app, "Wrong token."), 401)

            if path == "/items":
                return self.create_item(f)
            if path == "/items/manual":
                return self.create_manual(f)
            if path == "/advice/clear":
                app.db.clear_advice(f.get("domain", ""))
                back = f.get("back", "/")
                back = back if back.startswith("/items/") or back == "/" else "/"
                return self.redirect(back + "?" + urlencode({"msg": f"Forgot what PriceWatch learned about {f.get('domain', '')}."}))
            if path == "/refresh-all":
                n = app.db.refresh_all()
                return self.redirect("/?" + urlencode({"msg": f"Refreshing {n} listing(s). Shops that need a browser take ~10 s each."}))
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "items" and parts[1].isdigit():
                return self.item_action(int(parts[1]), parts[2], f)
            self.not_found()

        def _minutes(self, f) -> int:
            n = int(f.get("every") or 1)
            unit = int(f.get("unit") or 1440)
            if unit not in {size for _, size in FORM_UNITS} or n < 1:
                raise ValueError("Bad frequency")
            return parse_frequency(str(n * unit), app.s.min_interval_minutes)

        def create_item(self, f):
            try:
                url = clean_url(f.get("url", ""))
                minutes = self._minutes(f)
            except ValueError as err:
                return self.redirect("/?" + urlencode({"msg": str(err)}))
            iid = app.db.add_item(
                url=url, title=(f.get("title") or None), price=parse_price(f.get("price")),
                currency=(f.get("currency") or None), interval_minutes=minutes,
                target_price=parse_price(f.get("target")), method=f.get("method") or None)
            if not f.get("price"):
                app.db.force_due(iid)
            seen = parse_price(f.get("seen"))
            if seen is not None:
                fair.record_seen(app.db, iid, seen, f.get("currency") or None)
            app.fair.run_in_background(iid)
            self.redirect(f"/items/{iid}?" + urlencode({"msg": f"Watching. Checks every {human_interval(minutes)}."}))

        def create_manual(self, f):
            try:
                url = clean_url(f.get("url", ""))
            except ValueError as err:
                return self.redirect("/?" + urlencode({"msg": str(err)}))
            price = parse_price(f.get("price"))
            if price is None or price <= 0:
                return self.html(page_manual(app, url, flash="Enter the price as a number, e.g. 1139.99"))
            cur = f.get("currency") if f.get("currency") in ("GBP", "EUR", "USD") else "GBP"
            par = f.get("parent", "")
            parent = app.db.get_item(int(par)) if par.isdigit() else None
            if parent and parent["parent_id"]:
                parent = None
            iid = app.db.add_item(
                url=url, title=(f.get("title") or "").strip()[:200] or None, price=None, currency=cur,
                interval_minutes=parent["interval_minutes"] if parent else 1440,
                target_price=parse_price(f.get("target")), parent_id=parent["id"] if parent else None,
                source="you", review="confirmed" if parent else "ok")
            app.db.set_manual_price(iid, price, cur)
            msg = (f"Saved {fmt_price(price, cur)} for {domain_of(url)}. Update it on this page whenever you look again.")
            return self.redirect(f"/items/{parent['id'] if parent else iid}?" + urlencode({"msg": msg}))

        def item_action(self, iid: int, action: str, f):
            it = app.db.get_item(iid)
            if not it:
                return self.not_found()
            back = f"/items/{it['parent_id'] or iid}"
            if action == "delete":
                app.db.delete_item(iid)
                return self.redirect(back if it["parent_id"] else "/?msg=Deleted.")
            if action == "set-price":
                p = parse_price(f.get("price"))
                if p is None or p <= 0:
                    return self.redirect(back + "?msg=Enter+the+price+as+a+number.")
                app.db.set_manual_price(iid, p, it["currency"])
                return self.redirect(back + "?" + urlencode({"msg": f"Saved {fmt_price(p, it['currency'])}."}))
            if action == "check" and it["manual"]:
                return self.redirect(back + "?msg=This+shop+blocks+automatic+checks:+type+the+new+price+instead.")
            if action == "check":
                app.db.force_due(iid)
                return self.redirect(back + "?msg=" + "Check queued - refresh in a few seconds.".replace(" ", "+"))
            if action == "search":
                ok = app.agent.available and app.agent.run_in_background(iid)
                msg = "Searching other shops… refresh in a minute." if ok else app.agent.status_text()
                return self.redirect(back + "?" + urlencode({"msg": msg}))
            if action == "feedback":
                return self.feedback(it, f)
            if action == "add-skipped":
                k = app.db.pop_skipped(iid, f.get("url", ""))
                if not k:
                    return self.redirect(back + "?msg=That+shop+is+no+longer+in+the+list.")
                aid = app.db.add_item(url=k["url"], title=k["title"], price=parse_price(k["price"]),
                                      currency=k["currency"] or it["currency"],
                                      interval_minutes=it["interval_minutes"], parent_id=iid,
                                      source="you", review="confirmed",
                                      review_note="Added by you from the agent's skipped list")
                app.db.record_feedback(app.db.get_item(aid), "correct", "added from skipped list",
                                       domain_of(k["url"]))
                return self.redirect(back + "?" + urlencode({"msg": f"Added {domain_of(k['url'])} - it now counts as a price for this product."}))
            if action in ("accept-price", "keep-price"):
                app.db.resolve_pending(iid, accept=action == "accept-price")
                return self.redirect(back + "?msg=" + ("Price+updated." if action == "accept-price" else "Kept+the+previous+price."))
            if action == "target":
                raw = (f.get("target") or "").strip()
                tp = parse_price(raw) if raw else None
                if raw and (tp is None or tp <= 0):
                    return self.redirect("/?" + urlencode({"msg": "Alert price must be a number, e.g. 49.99"}))
                app.db.update_item(iid, target_price=str(tp) if tp is not None else None)
                name = it["title"] or domain_of(it["url"])
                msg = (f"Alert set: {name} at or below {fmt_price(tp, it['currency'])}." if tp is not None
                       else f"Alert removed for {name}.")
                return self.redirect("/?" + urlencode({"msg": msg}))
            if action == "fair":
                app.fair.run_in_background(iid)
                return self.redirect(back + "?" + urlencode({"msg": "Checking as anonymous shoppers… refresh in ~30 seconds."}))
            if action == "seen":
                sp = parse_price(f.get("seen"))
                if sp is None:
                    return self.redirect(back + "?msg=Enter+a+price.")
                fair.record_seen(app.db, iid, sp, it["currency"])
                return self.redirect(back + "?msg=Saved+the+price+you+saw.")
            if action == "update":
                try:
                    app.db.update_item(iid, interval_minutes=self._minutes(f),
                                       target_price=str(parse_price(f["target"])) if parse_price(f.get("target")) is not None else None,
                                       active=1 if f.get("active") else 0)
                except ValueError as err:
                    return self.redirect(back + "?" + urlencode({"msg": str(err)}))
                return self.redirect(back + "?msg=Saved.")
            if action == "alternatives":
                try:
                    url = clean_url(extract_url(f.get("url", "")) or "")
                except ValueError as err:
                    return self.redirect(back + "?" + urlencode({"msg": str(err)}))
                aid = app.db.add_item(url=url, title=None, price=None, currency=it["currency"],
                                      interval_minutes=it["interval_minutes"], parent_id=iid, source="user")
                app.db.force_due(aid)
                return self.redirect(back + "?msg=Added - first check runs shortly.".replace(" ", "+"))
            self.not_found()

        def feedback(self, it, f):
            """Feedback on an alternative: correct / wrong price (+ fixed link) / not the same."""
            if not it["parent_id"]:
                return self.redirect(f"/items/{it['id']}?msg=Feedback+is+for+shops+found+for+a+product.")
            back = f"/items/{it['parent_id']}"
            kind, note = f.get("kind", ""), " ".join((f.get("note") or "").split())[:300]
            dom = domain_of(it["url"])
            if kind not in {"correct", "wrong_price", "not_same"}:
                return self.redirect(back + "?msg=Choose+one+of+the+options.")
            new_url = None
            if f.get("url", "").strip():
                try:
                    new_url = clean_url(extract_url(f["url"]) or "")
                except ValueError as err:
                    return self.redirect(back + "?" + urlencode({"msg": f"That link didn't work: {err}"}))
                if domain_of(new_url) != dom:
                    return self.redirect(back + "?" + urlencode(
                        {"msg": f"The corrected link must be on {dom}. To add a different shop, use 'Add another shop'."}))
            app.db.record_feedback(it, kind, note or None, dom)
            if note:
                app.db.add_advice(dom, note)
            if kind == "not_same":
                app.db.reject_alternative(it)
                return self.redirect(back + "?" + urlencode({"msg": f"Removed {dom}. The agent won't add that link again."}))
            if new_url and new_url != it["url"]:
                learned = _learn_params(it["url"], new_url)
                if learned:
                    app.db.set_param_rule(dom, learned)
                app.db.set_url(it["id"], new_url)
                app.db.set_review(it["id"], "confirmed" if kind == "correct" else "ok",
                                  "Link corrected by you" + (f"; future {dom} links get ?{learned}" if learned else ""))
                return self.redirect(back + "?" + urlencode({"msg": f"Link updated for {dom}; re-checking the price now."
                                                                    + (f" Future {dom} links will use ?{learned}." if learned else "")}))
            if kind == "correct":
                app.db.set_review(it["id"], "confirmed", "Confirmed by you")
                return self.redirect(back + "?" + urlencode({"msg": f"Thanks - {dom}'s price now counts."}))
            app.db.set_review(it["id"], "wrong", note or "You marked this price as wrong")
            return self.redirect(back + "?" + urlencode(
                {"msg": f"Marked {dom}'s price as wrong; it won't count until you give the correct link."}))

        def api_share(self, f):
            if not app.bearer_ok(self.headers.get("Authorization")):
                return self.json({"error": "unauthorised"}, 401)
            url = extract_url(" ".join([f.get("url", ""), f.get("text", "")]))
            try:
                url = clean_url(url or "")
                minutes = parse_frequency(f.get("frequency") or "1d", app.s.min_interval_minutes)
                ex, final = fetch_price(app.s, url)
            except (ValueError, FetchError) as err:
                return self.json({"error": str(err)}, 422)
            iid = app.db.add_item(url=clean_url(final), title=ex.title, price=ex.price, currency=ex.currency,
                                  interval_minutes=minutes, target_price=parse_price(f.get("target")),
                                  method=ex.method, in_stock=ex.in_stock)
            seen = parse_price(f.get("seen"))
            if seen is not None:
                fair.record_seen(app.db, iid, seen, ex.currency)
            app.fair.run_in_background(iid)
            return self.json({"id": iid, "title": ex.title, "price": ex.price, "currency": ex.currency,
                              "every": human_interval(minutes),
                              "message": f"Watching {ex.title or 'item'} at {fmt_price(ex.price, ex.currency)}"})

    return Handler


def serve(settings: Settings, db: DB, agent: PriceAgent, fair_runner=None) -> ThreadingHTTPServer:
    app = App(settings, db, agent, fair_runner)
    httpd = ThreadingHTTPServer((settings.host, settings.port), make_handler(app))
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd
