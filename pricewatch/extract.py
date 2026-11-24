"""Pull a product title + price out of an HTML page.

Strategy, most to least reliable:
  1. schema.org JSON-LD (Product / Offer / AggregateOffer)  - most shops have this
  2. Microdata  (itemprop="price" / "priceCurrency")
  3. Open Graph / product meta tags (product:price:amount, og:price:amount)
  4. Heuristic: first currency-symbol amount in the visible text
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser

from .util import CURRENCY_SYMBOLS, parse_price


@dataclass
class Extracted:
    title: str | None
    price: Decimal | None
    currency: str | None
    method: str  # jsonld | microdata | meta | price-element | heading | heuristic | none
    in_stock: bool | None = None
    context: str | None = None  # the words around the price, so a person can judge it
    listing: bool = False       # the page lists many products (search/category), not one

    @property
    def reliable(self) -> bool:
        """True when the price came from the shop's own structured product data."""
        return self.method.split("+")[0] in RELIABLE_METHODS


RELIABLE_METHODS = ("jsonld", "microdata", "meta")


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "source", "track", "wbr"}
# class/id words meaning "this is NOT the current selling price"
_OTHER_OFFER = re.compile(r"(?:^|[\s_-])(?:used|olp|other-?sellers?|offer-?listing|more-?buying|"
                          r"secondary|renewed|refurb\w*|pre-?owned|aod)(?:$|[\s_-])")
_NOT_THE_PRICE = ("was", "old", "strike", "previous", "rrp", "compare", "month", "finance",
                  "credit", "klarna", "instalment", "installment", "per-", "saving", "save",
                  "delivery", "shipping", "unit-price", "unitprice",
                  # other offers on the same page (Amazon & marketplaces): used, other sellers...
                  "used", "olp", "other-seller", "othersellers", "offer-listing", "more-buying",
                  "secondary", "renewed", "refurb", "pre-owned", "preowned", "trade")
# text right after an amount that means it is not the item price
_NOT_PRICE_AFTER = re.compile(r"^\s*(per\s+month|a\s+month|/\s*m(on)?th|p/?m\b|monthly|x\s*\d|"
                              r"for\s+\d+\s+months|delivery|postage|off\b|saving)", re.I)


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld: list[str] = []
        self.meta: dict[str, str] = {}
        self.itemprops: dict[str, str] = {}
        self.title = ""
        self.text: list[str] = []
        self._in = None  # 'jsonld' | 'title' | 'skip' | None
        self._buf: list[str] = []
        self._pending_itemprop: str | None = None
        # element context, so visible prices inside "price" elements / headings win
        self._stack: list[tuple[str, str | None]] = []
        self.price_text: list[str] = []
        self.heading_text: list[str] = []

    def _ctx(self, tag: str, a: dict) -> str | None:
        parent = self._stack[-1][1] if self._stack else None
        if parent == "bad" or tag in ("del", "s", "strike"):
            return "bad"
        hint = " ".join(a.get(k, "") for k in ("class", "id", "data-test", "data-testid",
                                                "data-qa", "itemprop", "aria-label")).lower()
        if _OTHER_OFFER.search(hint):
            return "bad"  # a box for used / other sellers / refurbished: nothing inside is the price
        if "price" in hint:
            return "bad" if any(w in hint for w in _NOT_THE_PRICE) else "price"
        if parent:
            return parent
        return "heading" if tag in ("h1", "h2", "h3") else None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag not in _VOID:
            self._stack.append((tag, self._ctx(tag, a)))
        if tag == "script":
            self._in = "jsonld" if "ld+json" in a.get("type", "").lower() else "skip"
            self._buf = []
        elif tag in ("style", "noscript", "template"):
            self._in = "skip"
        elif tag == "title" and not self.title:
            self._in, self._buf = "title", []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a:
                self.meta.setdefault(key, a["content"])
        ip = a.get("itemprop", "").lower()
        if ip in ("price", "pricecurrency", "name", "lowprice", "availability"):
            val = a.get("content") or a.get("href") or a.get("value")
            if val:
                self.itemprops.setdefault(ip, val)
            elif tag != "meta":
                self._pending_itemprop = ip

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):   # tolerate badly nested HTML
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break
        if tag == "script" and self._in == "jsonld":
            self.jsonld.append("".join(self._buf))
        if tag == "title" and self._in == "title":
            self.title = " ".join("".join(self._buf).split())
        if tag in ("script", "style", "noscript", "template", "title"):
            self._in = None

    def handle_data(self, data):
        if self._in in ("jsonld", "title"):
            self._buf.append(data)
            return
        if self._in == "skip":
            return
        if self._pending_itemprop and data.strip():
            self.itemprops.setdefault(self._pending_itemprop, data.strip())
            self._pending_itemprop = None
        if data.strip():
            self.text.append(data.strip())
            ctx = self._stack[-1][1] if self._stack else None
            if ctx == "price":
                self.price_text.append(data.strip())
            elif ctx == "heading":
                self.heading_text.append(data.strip())


_LIST_TYPES = {"itemlist", "collectionpage", "searchresultspage", "offercatalog"}


def _walk_jsonld(node, found: list[dict], seen_types: set | None = None):
    # NB: deliberately does not descend into itemListElement: on a search/category page the
    # first listed product's price is not "the" price.
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, found, seen_types)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = {str(x).lower() for x in (t if isinstance(t, list) else [t])}
        if seen_types is not None:
            seen_types |= types
        if types & {"product", "productgroup", "offer", "aggregateoffer", "individualproduct"}:
            found.append(node)
        for k in ("@graph", "mainEntity", "hasVariant"):
            if k in node:
                _walk_jsonld(node[k], found, seen_types)


_RECURRING = ("month", "week", "year", "annual", "instal", "installment", "per ", "/mo", "deposit",
              "upfront", "subscription", "contract")


def _is_recurring_offer(offer: dict) -> bool:
    """O2/EE/Vodafone-style offers: 'from £20 a month' sits beside the pay-in-full price."""
    spec = offer.get("priceSpecification")
    specs = spec if isinstance(spec, list) else [spec] if spec else []
    for sp in specs:
        if isinstance(sp, dict) and (sp.get("billingDuration") or sp.get("billingIncrement")
                                     or str(sp.get("unitCode", "")).upper() in {"MON", "ANN", "WEE"}
                                     or any(w in str(sp.get("unitText", "")).lower() for w in _RECURRING)):
            return True
    text = " ".join(str(offer.get(k, "")) for k in ("name", "description", "category")).lower()
    return any(w in text for w in _RECURRING)


def _offer_price(offer) -> tuple[Decimal | None, str | None, bool | None]:
    if isinstance(offer, list):
        one_off = [o for o in offer if not (isinstance(o, dict) and _is_recurring_offer(o))]
        best = None
        for o in one_off or offer:
            p, c, s = _offer_price(o)
            if p is not None and (best is None or p < best[0]):
                best = (p, c, s)
        return best or (None, None, None)
    if not isinstance(offer, dict):
        return None, None, None
    price = parse_price(offer.get("price") if offer.get("price") not in (None, "") else offer.get("lowPrice"))
    cur = offer.get("priceCurrency")
    spec = offer.get("priceSpecification")
    if price is None and spec:
        spec0 = spec[0] if isinstance(spec, list) and spec else spec
        if isinstance(spec0, dict):
            price, cur = parse_price(spec0.get("price")), cur or spec0.get("priceCurrency")
    avail = str(offer.get("availability", "")).lower()
    stock = None if not avail else ("instock" in avail or "limitedavailability" in avail)
    return price, cur, stock


def _from_jsonld(blocks: list[str]):
    found: list[dict] = []
    types: set[str] = set()
    for raw in blocks:
        try:
            _walk_jsonld(json.loads(raw.strip().rstrip(";")), found, types)
        except (json.JSONDecodeError, ValueError):
            continue
    title = None
    for node in found:
        title = title or (node.get("name") if isinstance(node.get("name"), str) else None)
        offers = node.get("offers", node if "price" in node or "lowPrice" in node else None)
        p, c, s = _offer_price(offers)
        if p is not None:
            return title or node.get("name"), p, c, s, False
    return title, None, None, None, bool(types & _LIST_TYPES)


_SYM_RE = re.compile(r"(US\$|A\$|C\$|NZ\$|£|\$|€|¥|₹)\s?(\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)")


def extract(html: str) -> Extracted:
    c = _Collector()
    try:
        c.feed(html)
        c.close()
    except Exception:  # malformed HTML: use whatever we got
        pass

    meta_title = c.meta.get("og:title") or c.meta.get("twitter:title")
    title, price, cur, stock, is_listing = _from_jsonld(c.jsonld)
    title = title or meta_title or c.itemprops.get("name") or c.title or None
    if title:
        title = unescape(" ".join(str(title).split()))[:300]

    if price is not None:
        return Extracted(title, price, (cur or "").upper() or None, "jsonld", stock,
                         context="the shop's structured product data (schema.org offer)")

    p = parse_price(c.itemprops.get("price") or c.itemprops.get("lowprice"))
    if p is not None:
        avail = c.itemprops.get("availability", "").lower()
        return Extracted(title, p, (c.itemprops.get("pricecurrency") or "").upper() or None,
                         "microdata", ("instock" in avail) if avail else None,
                         context="the shop's product markup (itemprop=price)")

    for k_amt, k_cur in (("product:price:amount", "product:price:currency"),
                         ("og:price:amount", "og:price:currency"),
                         ("price", "pricecurrency")):
        p = parse_price(c.meta.get(k_amt))
        if p is not None:
            return Extracted(title, p, (c.meta.get(k_cur) or "").upper() or None, "meta",
                             context=f"the page's {k_amt} tag")

    if is_listing:  # a search/category page: don't guess a price from its text
        return Extracted(title, None, None, "none", listing=True)

    # visible text: price-labelled elements first, then headings, then the whole page
    for method, chunks in (("price-element", c.price_text), ("heading", c.heading_text),
                           ("heuristic", c.text)):
        found = _first_amount(" ".join(chunks))
        if found:
            ctx = found[2]
            if method != "heuristic":  # show the words around it on the page, not just the figure
                full = " ".join(c.text)
                at = full.find(ctx.split()[0]) if ctx else -1
                for m in _SYM_RE.finditer(full):
                    if parse_price(m.group(2).replace(" ", "")) == found[0]:
                        at = m.start()
                        break
                if at >= 0:
                    ctx = " ".join(full[max(0, at - 45):at + 45].split())
            return Extracted(title, found[0], found[1], method, context=ctx)
    return Extracted(title, None, None, "none")


def _first_amount(text: str):
    for m in _SYM_RE.finditer(text):
        if _NOT_PRICE_AFTER.match(text[m.end():m.end() + 25]):
            continue
        before = text[max(0, m.start() - 30):m.start()].lower()
        if any(w in before[-12:] for w in ("was", "from", "save", "rrp", "+")):
            continue
        if re.search(r"\b(used|pre-owned|refurbished|renewed|other sellers?|new & used)\b[^£$€]{0,20}$", before):
            continue  # "Used from £41.29", "Other sellers on Amazon £41.29"
        p = parse_price(m.group(2).replace(" ", ""))
        if p is not None and p > 0:
            ctx = " ".join(text[max(0, m.start() - 45):m.end() + 35].split())
            return p, CURRENCY_SYMBOLS.get(m.group(1)), ctx
    return None
