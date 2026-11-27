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


def fetch_price(settings: Settings, url: str, variant_hint: str | None = None) -> tuple[Extracted, str]:
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
            ex, final = _options(settings, res.url, res.text, extract(res.text), variant_hint)
            if ex.price is not None or mode == "never":
                return ex, final
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
    ex, final = _options(settings, res.url, res.text, ex, variant_hint)
    return ex, final


# ------------------------------------------------------------------ product options (size, frame…)
_RANGE = re.compile(r"(\d{3,4})\s*-\s*(\d{3,4})")
_FRAME_WORDS = {"steel", "fibre", "fiber", "fibreglass", "glassfibre", "glass", "fibretech", "easygrip", "carbon", "carbonx",
                "megaframe", "mega", "alu", "aluminium", "aluminum", "canvas", "22", "25", "28", "32"}


def option_hint(title: str | None) -> str | None:
    """The size/frame PriceWatch added to a title for a Shopify link, e.g.
    '… Awning (15 (1000-1025) / Steel 28 mm)' -> '15 (1000-1025) / Steel 28 mm'."""
    t = (title or "").rstrip()
    if not t.endswith(")"):
        return None
    depth = 0
    for i in range(len(t) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(t[i], 0)
        if depth == 0:
            inner = t[i + 1:-1].strip()
            return inner if _RANGE.search(inner) else None   # a size range, e.g. "(1000-1025)"
    return None


def strip_option_hint(title: str | None) -> str:
    hint = option_hint(title)
    return (title or "")[: (title or "").rfind("(" + hint)].rstrip() if hint else (title or "")


def _words(s: str) -> set[str]:
    s = re.sub(r"(\d+)\s*mm\b", r"\1", s.lower())
    w = set(re.findall(r"[a-z]+|\d+", s.replace("-", " ").replace("_", " ")))
    syn = {"fibretech": "fibre", "fibreglass": "fibre", "glassfibre": "fibre", "fiber": "fibre",
           "aluminium": "alu", "aluminum": "alu", "megaframe": "mega"}
    return {syn.get(x, x) for x in w}


def _option_score(hint: str, text: str) -> int | None:
    """None = not your option (wrong size, or a different frame). 2 = your size and frame;
    1 = your size, the label names no frame (the shop has no frame choice)."""
    h_rng, t_rng = _RANGE.search(hint), _RANGE.search(text)
    if h_rng:
        mid = (int(h_rng.group(1)) + int(h_rng.group(2))) / 2
        if t_rng:
            if not (int(t_rng.group(1)) <= mid <= int(t_rng.group(2))):
                return None
        else:   # e.g. "Size 15": same size number as yours
            size = re.match(r"\s*(\d{1,2})\b", hint)
            if not size or size.group(1) not in _words(text):
                return None
    hw = _words(_RANGE.sub(" ", hint)) & _FRAME_WORDS
    tw = _words(_RANGE.sub(" ", text)) & _FRAME_WORDS
    if not tw:
        return 1
    return 2 if _same_frame(hw, tw) else None


def _same_frame(want: set[str], got: set[str]) -> bool:
    """'Steel 28 mm' matches '28mm Frame' and 'Steel 28mm', not 'Steel EasyGrip' or 'Fibre'.
    Thickness must agree when either names one; no material you didn't choose."""
    wn, gn = {w for w in want if w.isdigit()}, {w for w in got if w.isdigit()}
    if (wn or gn) and wn != gn:
        return False
    extra = {w for w in got if not w.isdigit()} - want
    if wn and wn == gn:
        extra -= {"easygrip"}   # Dorema's clamp name: "28mm EasyGrip Steel" is a 28 mm steel frame
    if extra:                    # a material/type you didn't choose
        return False
    # the word that makes your frame different (Mega, EasyGrip, Fibre…) must be there;
    # plain "steel" may be left unsaid ("28mm Frame")
    return {w for w in want if not w.isdigit()} - {"steel"} <= got


def _woo_variations(html: str) -> list[dict]:
    import html as _h
    m = re.search(r'data-product_variations="([^"]+)"', html)
    if not m:
        return []
    try:
        data = json.loads(_h.unescape(m.group(1)))
    except ValueError:
        return []
    if not isinstance(data, list):   # "false": the shop loads its options separately (many options)
        return []
    return [v for v in data if isinstance(v, dict) and isinstance(v.get("attributes"), dict)]


def _woo_options_elsewhere(html: str) -> bool:
    """A WooCommerce product with options that aren't in the page (data-product_variations="false")."""
    return bool(re.search(r'data-product_variations="false"', html))


_ADDON = re.compile(r"([^\n()<>]{2,40})\((\d{1,2})\s*-\s*(\d{1,2})\)([^\n()<>]{0,30})\(\+\s*£\s*([\d,]+(?:\.\d{1,2})?)\)")


def _addon_lines(html: str) -> list[tuple[str, int, int, Decimal]]:
    """Paid extras like '28mm Frame (15-19) (+£130.00)': (label, from size, to size, price)."""
    import html as _h
    text = _h.unescape(re.sub(r"<[^>]+>", " ", html[:2_000_000]))
    out = []
    for m in _ADDON.finditer(text):
        label = " ".join((m.group(1) + " " + m.group(4)).split())
        if _words(label) & _FRAME_WORDS - {"canvas"}:
            try:
                out.append((label, int(m.group(2)), int(m.group(3)), Decimal(m.group(5).replace(",", ""))))
            except ArithmeticError:
                pass
    return out


def _paid_options(html: str) -> list[tuple[str, Decimal]]:
    """Options a shop prices as extras on top of the base price, e.g. Camping Online's
    <option data-wapf-label="size 15-1000-1025" data-wapf-price="500"> or any
    <option>size 15 (+£500.00)</option>. An option with no price adds nothing."""
    import html as _h
    out = []
    for m in re.finditer(r"<option\b([^>]*)>(.*?)</option>", html[:2_000_000], re.S | re.I):
        attrs, inner = m.group(1), _h.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
        lab = re.search(r'data-wapf-label="([^"]*)"', attrs)
        label = _h.unescape(lab.group(1)) if lab else re.sub(r"\(\+\s*£[^)]*\)", "", inner)
        price = re.search(r'data-wapf-price="([\d.]+)"', attrs) or re.search(r"\(\+\s*£\s*([\d,]+(?:\.\d{1,2})?)\)", inner)
        if not (lab or price):
            continue
        try:
            out.append((" ".join(label.split()), Decimal(price.group(1).replace(",", "")) if price else Decimal(0)))
        except ArithmeticError:
            pass
    return out


def _frame_addon(html: str, hint: str) -> tuple[str, Decimal] | None:
    size = re.match(r"\s*(\d{1,2})\b", hint)
    want = _words(_RANGE.sub(" ", hint)) & _FRAME_WORDS
    for label, lo, hi, price in _addon_lines(html):
        lw = _words(label) & _FRAME_WORDS
        if size and lo <= int(size.group(1)) <= hi and _same_frame(want, lw):
            return label, price
    return None


def _page_has_frame_options(html: str, hint: str) -> bool:
    labels = [str(v.get("title") or v.get("public_title") or "") for v in _variants_in_page(html)]
    labels += [" ".join(map(str, v["attributes"].values())) for v in _woo_variations(html)]
    return any(_words(_RANGE.sub(" ", l)) & _FRAME_WORDS for l in labels)


def _jsonld_offers(html: str) -> list[dict]:
    """schema.org offers that name their option, e.g. Attwoolls (Magento): an AggregateOffer
    whose offers have "sku": "ATTE2023-Size 16 (1015-1040cm)" and their own price."""
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html[:2_000_000], re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
                continue
            if not isinstance(o, dict):
                continue
            # a variant (ProductGroup → hasVariant, e.g. Awning Ace) names its size; its offer
            # only has the price, so the offer inherits the variant's size as its label
            size = o.get("size") if isinstance(o.get("size"), str) else ""
            for k in ("@graph", "offers", "hasVariant"):
                kids = o.get(k) if isinstance(o.get(k), list) else ([o.get(k)] if o.get(k) else [])
                for x in kids:
                    if k == "offers" and size and isinstance(x, dict):
                        x = {**x, "_size": size}   # keep the product page's link
                    stack.append(x)
            if "price" in o and str(o.get("@type", "")).lower() == "offer":
                label = o.get("_size") or " ".join(str(o.get(k) or "") for k in ("name", "sku", "url"))
                if _RANGE.search(label) or re.search(r"\bsize\b", label, re.I):
                    out.append({"label": label, "price": o.get("price"), "url": o.get("url"),
                                "in_stock": "instock" in str(o.get("availability", "")).lower().replace(" ", "")})
    return out


def _woo_selects(html: str) -> dict[str, list[str]]:
    """WooCommerce option lists: {"attribute_pa_size": ["size-15-1000-1025-cm", …], …}."""
    import html as _h
    out = {}
    for m in re.finditer(r'<select\b[^>]*name="(attribute_[^"]+)"[^>]*>(.*?)</select>', html[:2_000_000], re.S | re.I):
        vals = [_h.unescape(v) for v in re.findall(r'<option\b[^>]*value="([^"]*)"', m.group(2)) if v]
        if vals:
            out[_h.unescape(m.group(1))] = vals
    return out


_FREE_ITEM = re.compile(r'data-name="([^"]*\bfree\b[^"]*)"[^>]*data-price="0(?:\.0+)?"', re.I)


def _free_frame_upgrade(html: str, want: set[str]) -> str | None:
    """A free add-on that gives you your frame, e.g. Standart Caravans' "FREE 28mm Steel Frame
    Upgrade" bundled at £0 (WPC "bought together" item): its name, else None."""
    if not {w for w in want if w.isdigit()}:
        return None
    for m in _FREE_ITEM.finditer(html):
        name = m.group(1)
        got = _words(name) & _FRAME_WORDS
        if "upgrade" in name.lower() and got and _same_frame(want, got):
            return name
    return None


def _pick_woo_options(html: str, hint: str, title: str | None) -> dict[str, str] | None:
    """Your size and frame from each WooCommerce option list; None if a list that decides the
    size or frame has nothing that is yours."""
    want = _words(_RANGE.sub(" ", hint)) & _FRAME_WORDS
    other = _words(f"{hint} {title or ''}")
    picked = {}
    for name, values in _woo_selects(html).items():
        sized = [v for v in values if _RANGE.search(v)]
        framed = [v for v in values if _words(_RANGE.sub(" ", v)) & _FRAME_WORDS]
        if sized:
            mine = [v for v in sized if _option_score(hint, _RANGE.search(v).group(0)) is not None]
        elif framed:
            mine = [v for v in framed if want and _same_frame(want, _words(v) & _FRAME_WORDS)]
            if mine and _free_frame_upgrade(html, want):   # your frame comes free: pay for the base one
                mine = framed[:1]
        else:   # colour, depth…: the one your product names, else the first
            mine = [v for v in values if _words(v) and _words(v) <= other] or values[:1]
        if not mine:
            return None
        picked[name] = mine[0]
    return picked or None


def _woo_selected(settings: Settings, url: str, html: str, ex: Extracted, hint: str | None = None):
    """A WooCommerce link that names its options (?attribute_size=…&attribute_depth=…, as the
    bookmarklet sends them): that exact combination's price, from the page or, when the shop
    loads options separately, from WooCommerce's own price lookup (?wc-ajax=get_variation)."""
    from urllib.parse import parse_qsl
    p = urlsplit(url)
    attrs = {k: v for k, v in parse_qsl(p.query) if k.startswith("attribute_") and v}
    if not attrs and hint and _woo_options_elsewhere(html):
        attrs = _pick_woo_options(html, hint, ex.title) or {}
        if attrs:   # remember the exact options, so re-checks ask for the same ones
            from urllib.parse import urlencode
            url = urlunsplit((p.scheme, p.netloc, p.path, urlencode(attrs), ""))
    if not attrs or "variations_form" not in html:
        return None
    label = " / ".join(attrs.values())
    data = None
    for v in _woo_variations(html):   # embedded: match every attribute (blank = "any")
        if all(str(v["attributes"].get(k) or val).lower() == val.lower() for k, val in attrs.items()):
            data = v
            break
    if data is None and _woo_options_elsewhere(html):
        m = re.search(r'variations_form[^>]*data-product_id="(\d+)"|data-product_id="(\d+)"[^>]*variations_form', html)
        if m:
            try:
                res = safe_get(urlunsplit((p.scheme, p.netloc, "/", "wc-ajax=get_variation", "")),
                               user_agent=settings.user_agent, timeout=settings.fetch_timeout,
                               allow_private=settings.allow_private_hosts,
                               form={**attrs, "product_id": m.group(1) or m.group(2)})
                got = json.loads(res.text)
                data = got if isinstance(got, dict) else None
            except (FetchError, ValueError):
                data = None
    if not data:
        return Extracted(title=ex.title, price=None, currency=ex.currency, method="none",
                         context=f"the shop has no '{label}' option now"), url
    try:
        price = Decimal(str(data.get("display_price"))).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError):
        return None
    free = _free_frame_upgrade(html, _words(_RANGE.sub(" ", hint)) & _FRAME_WORDS) if hint else None
    if free:
        label = f"{label} + {free.strip()}"
    title = ex.title if not ex.title or label in ex.title else f"{ex.title} ({label})"
    stock = data.get("is_in_stock")
    return Extracted(title=title, price=price, currency=ex.currency, method="woocommerce",
                     in_stock=stock if isinstance(stock, bool) else ex.in_stock,
                     context=f"option matched to your choice: {label}"[:200]), url


def _options(settings: Settings, url: str, html: str, ex: Extracted, hint: str | None):
    """Pick the product option (size, frame, colour…) the link or your choice means, and
    return (extracted, url_of_that_option). Shopify and WooCommerce shops."""
    picked = _woo_selected(settings, url, html, ex, hint)
    if picked:
        return picked
    if not hint:
        return _shopify_variant(settings, url, html, ex), url
    p = urlsplit(url)
    best = None   # (score, price, label, url, in_stock, method)
    m = _SHOPIFY_PATH.match(p.path)
    if m and _SHOPIFY_MARK.search(html[:400_000]):
        variants = _variants_in_page(html)
        if not variants:
            try:
                res = safe_get(urlunsplit((p.scheme, p.netloc, f"/products/{m.group(2)}.js", "", "")),
                               user_agent=settings.user_agent, timeout=settings.fetch_timeout,
                               allow_private=settings.allow_private_hosts)
                variants = [v for v in json.loads(res.text).get("variants", []) if isinstance(v, dict)]
            except (FetchError, ValueError, AttributeError):
                variants = []
        for v in variants:
            label = str(v.get("title") or v.get("public_title") or v.get("name") or "")
            score, raw = _option_score(hint, label), v.get("price")
            if score is None or not isinstance(raw, (int, str)):
                continue
            try:
                price = (Decimal(raw) / 100) if isinstance(raw, int) else Decimal(str(raw))
            except ArithmeticError:
                continue
            u = urlunsplit((p.scheme, p.netloc, p.path, f"variant={v.get('id')}", ""))
            cand = (score, price.quantize(Decimal("0.01")), label, u, v.get("available"), "shopify")
            best = cand if best is None or cand[0] > best[0] else best
    for v in _woo_variations(html):
        attrs = {k: str(val) for k, val in v["attributes"].items() if val}
        label = " / ".join(attrs.values())
        score = _option_score(hint, label)
        try:
            price = Decimal(str(v.get("display_price")))
        except (ArithmeticError, ValueError):
            continue
        if score is None or price <= 0:
            continue
        from urllib.parse import urlencode
        u = urlunsplit((p.scheme, p.netloc, p.path, urlencode(attrs), ""))
        cand = (score, price.quantize(Decimal("0.01")), label, u, v.get("is_in_stock"), "woocommerce")
        best = cand if best is None or cand[0] > best[0] else best
    if best is not None and best[0] == 1 and (_words(_RANGE.sub(" ", hint)) & _FRAME_WORDS):
        # your size, but the option names no frame: the frame may be a paid extra (Grantham:
        # "28mm Frame (15-19) (+£130.00)"), or another option on the page
        addon = _frame_addon(html, hint)
        if addon:
            label, extra = addon
            best = (2, best[1] + extra, f"{best[2]} + {label}", best[3], best[4], best[5])
        elif _page_has_frame_options(html, hint) or _addon_lines(html):
            best = None
    offers = _jsonld_offers(html) if best is None else []
    for o in offers:
        score = _option_score(hint, o["label"])
        try:
            price = Decimal(str(o["price"]))
        except (ArithmeticError, ValueError):
            continue
        if score is None or price <= 0:
            continue
        label = re.sub(r"^\S*?-(?=size\b)", "", o["label"].strip(), flags=re.I) or o["label"]
        u = o["url"] if isinstance(o["url"], str) and o["url"].startswith(("http://", "https://")) else url
        cand = (score, price.quantize(Decimal("0.01")), label.strip()[:80], u, o["in_stock"], "jsonld")
        best = cand if best is None or cand[0] > best[0] else best
    sizes = [(l, p) for l, p in _paid_options(html) if _RANGE.search(l)] if best is None else []
    if sizes and ex.price and _RANGE.search(hint):
        # the size is a paid extra on a fixed base price (Camping Online: £1,479 + £500)
        mine = [(l, p) for l, p in sizes if _option_score(hint, l) is not None]
        if not mine:
            return Extracted(title=ex.title, price=None, currency=ex.currency, method="none",
                             context=f"no '{hint}' option on this page"), url
        label, extra = mine[0]
        total, parts = ex.price + extra, [label]
        want = _words(_RANGE.sub(" ", hint)) & _FRAME_WORDS
        frame = _frame_addon(html, hint) if want and not (want <= _words(ex.title or "")) else None
        if frame:
            total += frame[1]
            parts.append(frame[0])
        label = " + ".join(parts)
        title = ex.title if not ex.title or label in ex.title else f"{ex.title} ({label})"
        return Extracted(title=title, price=total.quantize(Decimal("0.01")), currency=ex.currency,
                         method=ex.method, in_stock=ex.in_stock,
                         context=f"option matched to your choice: {label} "
                                 f"({ex.price} + extras {(total - ex.price).quantize(Decimal('0.01'))})"[:200]), url
    if best is None and _woo_options_elsewhere(html):
        return Extracted(title=ex.title, price=None, currency=ex.currency, method="none",
                         context=f"no '{hint}' option on this page (this shop loads its options "
                                 "separately, so PriceWatch can't pick yours; type the price in instead)"), url
    has_options = bool(offers) or bool(_woo_variations(html)) or bool(m and _SHOPIFY_MARK.search(html[:400_000]))
    if best is None:
        if has_options:   # options exist, none is yours: don't report the default option's price
            return Extracted(title=ex.title, price=None, currency=ex.currency, method="none",
                             context=f"no '{hint}' option on this page"), url
        return ex, url
    score, price, label, u, avail, method = best
    title = ex.title if not ex.title or label in ex.title else f"{ex.title} ({label})"
    return Extracted(title=title, price=price, currency=ex.currency,
                     method=method, in_stock=avail if isinstance(avail, bool) else ex.in_stock,
                     context=f"option matched to your choice: {label}"[:200]), u


_SHOPIFY_PATH = re.compile(r"^((?:/[a-z]{2}(?:-[a-z]{2})?)?(?:/collections/[^/]+)?/products/([^/?#]+))/?$", re.I)
_SHOPIFY_MARK = re.compile(r"cdn\.shopify\.com|shopify\.(?:shop|theme)|myshopify\.com|shopify-section", re.I)


def _variants_in_page(html: str, want: str = "") -> list[dict]:
    """Shopify pages embed variants as JSON (ShopifyAnalytics meta, product JSON scripts):
    "variants":[{"id":…,"price":197900,…}], prices in pence. Pages also embed other products'
    variants (recommendations), so: the list containing the linked variant id, else the first
    list on the page (the product's own, in ShopifyAnalytics meta)."""
    lists: list[list[dict]] = []
    dec = json.JSONDecoder()
    for m in re.finditer(r'"variants"\s*:\s*\[', html[:2_000_000]):
        try:
            arr, _ = dec.raw_decode(html, m.end() - 1)
        except ValueError:
            continue
        vs = [v for v in arr if isinstance(v, dict) and "id" in v and isinstance(v.get("price"), int)]
        if vs:
            lists.append(vs)
    if want:
        return next((vs for vs in lists if any(str(v["id"]) == want for v in vs)), [])
    return lists[0] if lists else []


def _shopify_variant(settings: Settings, url: str, html: str, ex: Extracted) -> Extracted:
    """Shopify shops put the product's *lowest* variant price in their page data, so a link to a
    bigger size (…/products/x?variant=123) reads too low. Shopify also publishes every variant's
    own price at /products/<handle>.js: use the one the link selects (or the shop's default)."""
    p = urlsplit(url)
    m = _SHOPIFY_PATH.match(p.path)
    if not m or not _SHOPIFY_MARK.search(html[:400_000]):
        return ex
    want = (parse_qs(p.query).get("variant") or [""])[0]
    data, variants = {}, _variants_in_page(html, want)   # no extra request: shops rate-limit (429)
    if not variants:
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
    chosen = next((v for v in variants if str(v.get("id")) == want), None)
    if chosen is None:
        if want:   # a variant the shop no longer has: don't guess
            return ex
        chosen = next((v for v in variants if v.get("available")), variants[0])
    raw = chosen.get("price")
    try:   # /products/x.js gives pence as an integer
        price = ((Decimal(raw) / 100) if isinstance(raw, int) else Decimal(str(raw))).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError, TypeError):
        return ex
    if price <= 0:
        return ex
    title = ex.title or data.get("title")
    vt = str(chosen.get("title") or chosen.get("public_title") or "")
    if len(variants) > 1 and vt and vt.lower() != "default title" and title and vt not in title:
        title = f"{title} ({vt})"[:300]
    return Extracted(title=title, price=price, currency=ex.currency, method="shopify",
                     in_stock=chosen.get("available") if isinstance(chosen.get("available"), bool) else ex.in_stock,
                     context=f"Shopify variant {chosen.get('id')}: {vt}"[:200] if vt else None)


def item_option_hint(db: DB, item) -> str | None:
    """Your chosen size/frame for this product (from the product you watch)."""
    top = db.get_item(item["parent_id"]) if item["parent_id"] else item
    return option_hint(top["title"]) if top else None


def check_item(settings: Settings, db: DB, item) -> None:
    if _is_manual(item):   # you type this price in; the shop blocks automatic checks
        return
    try:
        ex, _ = fetch_price(settings, item["url"], item_option_hint(db, item))
    except FetchError as e:
        db.record_check(item["id"], error=str(e))
        log.info("check %s failed: %s", item["id"], e)
        return
    if ex.price is None:
        db.record_check(item["id"], error="No price found on the page (it may need JavaScript).")
        return
    if _hold_doubtful(db, item, ex):
        return
    if item["parent_id"] and ex.title and (ex.context or "").startswith("option matched") \
            and ex.title != item["title"]:
        db.update_item(item["id"], title=ex.title[:300])   # name the option now checked, not an old one
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
