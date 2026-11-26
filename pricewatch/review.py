# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Shared judgement about candidate shops and prices, used by the agent, the scheduled
re-checks and the in-app search. Pure functions, no I/O."""
from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

from .extract import RELIABLE_METHODS
from .util import domain_of

LOW_RATIO = Decimal("0.6")    # under 60% of the known price: maybe monthly / deposit / used
HIGH_RATIO = Decimal("2")     # over 2x the known price: maybe a bundle or another product

# Sites that are never the place to buy: social, video, news/reviews, price-comparison.
NOT_SHOPS = (
    "facebook.com", "instagram.com", "tiktok.com", "youtube.com", "youtu.be", "x.com", "twitter.com",
    "reddit.com", "pinterest.com", "pinterest.co.uk", "linkedin.com", "threads.net", "quora.com",
    "medium.com", "wikipedia.org", "wikihow.com",
    "bbc.co.uk", "theguardian.com", "radiotimes.com", "liverpoolecho.co.uk", "manchestereveningnews.co.uk",
    "mirror.co.uk", "express.co.uk", "dailymail.co.uk", "independent.co.uk", "standard.co.uk",
    "thesun.co.uk", "telegraph.co.uk", "which.co.uk", "techradar.com", "pcmag.com", "trustedreviews.com",
    "t3.com", "stuff.tv", "theverge.com", "ign.com", "eurogamer.net", "gamesradar.com", "pushsquare.com",
    "purexbox.com", "tomsguide.com", "cnet.com", "engadget.com", "wired.com", "forbes.com",
    "pricespy.co.uk", "pricerunner.com", "idealo.co.uk", "hotukdeals.com", "latestdeals.co.uk",
    "camelcamelcamel.com", "keepa.com", "lyst.co.uk", "lyst.com", "google.com", "google.co.uk",
    "bing.com", "duckduckgo.com",
)
NOT_SHOP_PREFIXES = ("support.", "help.", "community.", "forum.", "forums.", "blog.", "news.", "docs.")

_LISTING_PATH = re.compile(
    r"/(?:s|search|browse|b|c|cat|category|categories|collections?|list|lists|discover|brands?|"
    r"deals|tag|tags|sch)(?:/|$)", re.I)
_LISTING_QUERY = {"k", "q", "query", "search", "searchterm", "_nkw", "keyword", "keywords", "text"}


def not_a_shop(url: str) -> str | None:
    """Reason this URL is not somewhere you can buy, or None."""
    host = (urlsplit(url).hostname or "").lower()
    d = domain_of(url)
    if host.startswith(NOT_SHOP_PREFIXES):
        return "a help/blog/forum page, not a shop"
    for ns in NOT_SHOPS:
        if d == ns or d.endswith("." + ns):
            return "not a shop (social, video, news, review or price-comparison site)"
    return None


def looks_like_listing(url: str) -> bool:
    """Search results / category pages show many products; their first price is meaningless."""
    p = urlsplit(url)
    if _LISTING_PATH.search(p.path):
        return True
    return any(k.lower() in _LISTING_QUERY for k, _ in parse_qsl(p.query))


def price_doubt(price, ref_price, method: str | None) -> str | None:
    """Why a found price shouldn't be trusted without the user's say-so, or None."""
    if method and method.split("+")[0] not in RELIABLE_METHODS:
        return "price read from the page text, not the shop's product data - please check it"
    if price is None or not ref_price:
        return None
    price, ref = Decimal(str(price)), Decimal(str(ref_price))
    if price < ref * LOW_RATIO:
        return f"much lower than {ref} elsewhere - could be a monthly, deposit, trade-in or used price"
    if price > ref * HIGH_RATIO:
        return f"much higher than {ref} elsewhere - could be a bundle or a different product"
    return None


_UNIT = re.compile(r"^\d+(?:\.\d+)?(?:w|kw|gb|tb|mb|cm|mm|m|kg|g|ml|l|v|mah|hz|in|inch|x|p|k)$")


def model_codes(text: str | None) -> set[str]:
    """Manufacturer model codes in a title, e.g. BEMWH551-GB -> 'bemwh551gb', WH-1000XM5."""
    out = set()
    for tok in re.split(r"[^A-Za-z0-9-]+", text or ""):
        t = tok.lower().replace("-", "")
        if len(t) >= 5 and re.search(r"[a-z].*[a-z]|[a-z]{2}", t) and re.search(r"\d", t) \
                and not _UNIT.match(t) and not re.fullmatch(r"(19|20)\d\d", t):
            out.add(t)
    return out


def shares_model_code(a: set[str], b: set[str]) -> str | None:
    for x in a:
        for y in b:
            short, long_ = sorted((x, y), key=len)
            if len(short) >= 5 and long_.startswith(short):
                return short.upper()
    return None
