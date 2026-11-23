"""Small pure helpers: URL hygiene, frequency parsing, price parsing."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------- frequency

MINUTE = 1
UNITS = {
    "m": MINUTE, "min": MINUTE, "mins": MINUTE, "minute": MINUTE, "minutes": MINUTE,
    "h": 60, "hr": 60, "hour": 60, "hours": 60,
    "d": 1440, "day": 1440, "days": 1440,
    "w": 10080, "week": 10080, "weeks": 10080,
    "mo": 43200, "month": 43200, "months": 43200,   # 30 days
    "y": 525600, "yr": 525600, "year": 525600, "years": 525600,
}
MAX_INTERVAL_MINUTES = 525600  # one year
FORM_UNITS = [("minutes", 1), ("hours", 60), ("days", 1440), ("weeks", 10080),
              ("months", 43200), ("years", 525600)]


def parse_frequency(text: str, min_minutes: int = 5) -> int:
    """'15m', '2 hours', '1y', '90' (minutes) -> minutes, clamped to [min, 1 year]."""
    t = (text or "").strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", t)
    if not m:
        raise ValueError(f"Can't understand frequency {text!r}. Try 15m, 6h, 1d, 1w, 1mo, 1y.")
    qty, unit = float(m.group(1)), m.group(2) or "m"
    if unit not in UNITS:
        raise ValueError(f"Unknown unit {unit!r}. Use m, h, d, w, mo or y.")
    minutes = int(round(qty * UNITS[unit]))
    return max(min_minutes, min(MAX_INTERVAL_MINUTES, minutes))


def human_interval(minutes: int) -> str:
    for name, size in reversed(FORM_UNITS):
        if minutes >= size and minutes % size == 0:
            n = minutes // size
            return f"{n} {name[:-1] if n == 1 else name}"
    return f"{minutes} minutes"


# ---------------------------------------------------------------- URLs

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

# Query params that are tracking/affiliate noise.
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid",
    "_ga", "_gl", "ref", "ref_", "referrer", "spm", "scm", "pd_rd_r", "pd_rd_w",
    "pd_rd_wg", "pf_rd_p", "pf_rd_r", "smid", "linkcode", "linkid", "camp", "creative",
    "share_id", "si", "feature", "rec", "recs", "clickid", "cmpid", "cid", "intcmp", "srsltid",
}
# Params that may carry credentials/sessions. Never stored, never sent anywhere.
SENSITIVE_PARAM_HINTS = ("token", "session", "sid", "auth", "key", "secret", "password",
                         "passwd", "sig", "signature", "jwt", "code", "email", "user")


def extract_url(text: str) -> str | None:
    """Pull the first URL out of shared text like 'Look at this! https://…'."""
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(").,;!?]") if m else None


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(h in k for h in SENSITIVE_PARAM_HINTS)


def clean_url(url: str) -> str:
    """Normalise a product URL for storage: drop fragment, tracking & sensitive params."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only http(s) links are supported.")
    if not parts.hostname:
        raise ValueError("Link has no host.")
    if parts.username or parts.password:
        raise ValueError("Links containing usernames/passwords are not accepted.")
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in TRACKING_PARAMS
        and not _is_sensitive(k)
    ]
    netloc = parts.hostname.lower() + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", urlencode(kept), ""))


def url_for_ai(url: str) -> str:
    """The only form of a URL ever shown to an LLM: scheme://host/path, no query."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.hostname or "", p.path, "", ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- prices

CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "¥": "JPY", "₹": "INR",
                    "A$": "AUD", "C$": "CAD", "NZ$": "NZD", "US$": "USD", "kr": "SEK",
                    "CHF": "CHF", "zł": "PLN"}


def parse_price(raw) -> Decimal | None:
    """Parse '1,299.99', '1.299,99', '£12', 12.5 -> Decimal. None if not a price."""
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            d = Decimal(str(raw))
        except InvalidOperation:
            return None
        return d if d >= 0 else None
    s = re.sub(r"[^\d.,]", "", str(raw))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        # whichever separator comes last is the decimal separator
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = (head.replace(",", "") + "." + tail) if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + ("." + tail if len(tail) in (1, 2) else tail)
    try:
        d = Decimal(s.strip("."))
    except InvalidOperation:
        return None
    return d if d >= 0 else None


def fmt_price(value, currency: str | None) -> str:
    if value is None:
        return "—"
    sym = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥", "INR": "₹"}.get((currency or "").upper())
    v = Decimal(str(value))
    body = f"{v:,.2f}"
    return f"{sym}{body}" if sym else f"{body} {currency or ''}".strip()
