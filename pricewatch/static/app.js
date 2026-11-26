// SPDX-License-Identifier: AGPL-3.0-or-later
// Chart hover: crosshair + tooltip showing every series' price at that moment.
// Served from 'self' so the strict CSP (no inline scripts) holds.
(function () {
  function priceAt(points, t) {
    let v = null;
    for (const [ts, p] of points) { if (ts <= t) v = p; else break; }
    return v;
  }
  function fmt(v, sym) {
    return v == null ? "—" : sym + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  document.querySelectorAll("[data-chart]").forEach(function (wrap) {
    const dataEl = wrap.nextElementSibling;
    if (!dataEl || !dataEl.classList.contains("chart-data")) return;
    const d = JSON.parse(dataEl.textContent);
    const svg = wrap.querySelector("svg"), hit = svg.querySelector(".hit"),
          cross = svg.querySelector(".crosshair"), tip = wrap.querySelector(".tip");
    function move(ev) {
      const box = svg.getBoundingClientRect();
      const sx = (ev.clientX - box.left) * (d.w / box.width);
      const frac = Math.min(1, Math.max(0, (sx - d.x[0]) / d.x[1]));
      const t = d.t0 + frac * (d.t1 - d.t0);
      cross.setAttribute("x1", sx); cross.setAttribute("x2", sx); cross.setAttribute("visibility", "visible");
      const rows = d.series.map(function (s, i) {
        return '<div class="r"><span><span class="key s' + (i + 1) + '"></span>' +
          s.name.replace(/[&<>]/g, "") + "</span><b>" + fmt(priceAt(s.points, t), d.sym) + "</b></div>";
      }).join("");
      tip.innerHTML = '<div class="t">' + new Date(t * 1000).toLocaleString() + "</div>" + rows;
      tip.hidden = false;
      const px = ev.clientX - box.left;
      tip.style.left = (px > box.width / 2 ? px - tip.offsetWidth - 12 : px + 12) + "px";
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerleave", function () { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  });
})();
