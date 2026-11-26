# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""v3 - price history chart, rendered server-side as SVG (no JS libraries, no CDN).

Step lines (a price holds until the next check changes it), one y-axis, the
original item first in the fixed categorical order, alternatives after it.
Hover crosshair + tooltip is added by static/app.js using the embedded JSON.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from html import escape

MAX_SERIES = 6  # original + 5 cheapest alternatives; more would hurt legibility
W, H = 760, 300
M_L, M_R, M_T, M_B = 64, 16, 12, 32


def _nice_step(span: float, target_ticks: int = 5) -> float:
    if span <= 0:
        return 1.0
    raw = span / target_ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _fmt_money(v: float, sym: str) -> str:
    return f"{sym}{v:,.0f}" if abs(v) >= 100 else f"{sym}{v:,.2f}"


def _time_ticks(t0: int, t1: int):
    span = max(1, t1 - t0)
    for step, fmt in ((3600, "%H:%M"), (6 * 3600, "%d %b %H:%M"), (86400, "%d %b"),
                      (7 * 86400, "%d %b"), (30 * 86400, "%b %Y"), (365 * 86400, "%Y")):
        if span / step <= 7:
            break
    first = (t0 // step + 1) * step
    ticks = list(range(first, t1 + 1, step))[:8]
    return [(t, datetime.fromtimestamp(t, timezone.utc).astimezone().strftime(fmt)) for t in ticks]


def render(series: list[dict], currency_symbol: str = "") -> tuple[str, str]:
    """series: [{"name": str, "points": [(ts:int, price:float), ...]}]
    Returns (svg_markup, json_payload_for_hover)."""
    series = [s for s in series if s["points"]][:MAX_SERIES]
    if not series:
        return "", "{}"
    ts = [t for s in series for t, _ in s["points"]]
    vals = [v for s in series for _, v in s["points"]]
    t0, t1 = min(ts), max(max(ts), int(time.time()))
    if t1 == t0:
        t0 -= 3600
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.1 or max(1.0, hi * 0.05)
    step = _nice_step(hi - lo + 2 * pad)
    y0 = math.floor((lo - pad) / step) * step
    y1 = math.ceil((hi + pad) / step) * step
    if y0 < 0 <= lo:
        y0 = 0.0

    pw, ph = W - M_L - M_R, H - M_T - M_B
    X = lambda t: M_L + (t - t0) / (t1 - t0) * pw  # noqa: E731
    Y = lambda v: M_T + (1 - (v - y0) / (y1 - y0)) * ph  # noqa: E731

    out = [f'<svg class="pw-chart" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Price history line chart" preserveAspectRatio="xMidYMid meet">']
    # grid + y labels (recessive)
    v = y0
    while v <= y1 + 1e-9:
        y = Y(v)
        out.append(f'<line class="grid" x1="{M_L}" x2="{W - M_R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="axis" x="{M_L - 8}" y="{y + 4:.1f}" text-anchor="end">'
                   f'{escape(_fmt_money(v, currency_symbol))}</text>')
        v += step
    for t, label in _time_ticks(t0, t1):
        out.append(f'<text class="axis" x="{X(t):.1f}" y="{H - 10}" text-anchor="middle">{escape(label)}</text>')
    out.append(f'<line class="baseline" x1="{M_L}" x2="{W - M_R}" y1="{M_T + ph}" y2="{M_T + ph}"/>')

    # step lines, extended to "now" so the current price is visible
    for i, s in enumerate(series):
        pts = sorted(s["points"])
        d = f"M{X(pts[0][0]):.1f},{Y(pts[0][1]):.1f}"
        for (ta, va), (tb, vb) in zip(pts, pts[1:]):
            d += f" H{X(tb):.1f} V{Y(vb):.1f}"
        d += f" H{X(t1):.1f}"
        out.append(f'<path class="line s{i + 1}" d="{d}"/>')
        lt, lv = pts[-1]
        out.append(f'<circle class="dot s{i + 1}" cx="{X(t1):.1f}" cy="{Y(lv):.1f}" r="4"/>')
    # hover layer (populated by app.js)
    out.append(f'<line class="crosshair" x1="0" x2="0" y1="{M_T}" y2="{M_T + ph}" visibility="hidden"/>')
    out.append(f'<rect class="hit" x="{M_L}" y="{M_T}" width="{pw}" height="{ph}"/>')
    out.append("</svg>")

    payload = {"t0": t0, "t1": t1, "x": [M_L, pw], "w": W, "sym": currency_symbol,
               "series": [{"name": s["name"], "points": sorted(s["points"])} for s in series]}
    # make it safe to embed in <script type="application/json">
    js = json.dumps(payload).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return "".join(out), js
