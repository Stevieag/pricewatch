# Changelog

## Unreleased

- Repository protection: rulesets (PR + passing CI with no bypass, no force-push/deletion,
  immutable release tags) exported for import, Dependabot for CI actions, CODEOWNERS,
  CONTRIBUTING.md, issue/PR templates, and a maintainer checklist (`docs/repo-protection.md`).
- **Licence changed from MIT to AGPL-3.0-or-later.** Source files carry SPDX licence
  identifiers. The web UI footer links to the source code (configurable with `PW_SOURCE_URL`
  for modified copies run for others, as AGPL section 13 requires).
- Watchlist **Refresh all prices** button: re-checks everything now and auto-updates the
  page until done. Headless browsers are limited to two at once.
- Cheaper prices get a bold green **⚑ £X cheaper (Y%)** flag and a green row stripe on the
  watchlist and product page (trusted prices only), and a bold green line in `find-better`.
- **`start` / `stop [--all]` / `status`**: one command starts Docker Desktop, SearXNG, Ollama
  and PriceWatch in the background (waiting until each answers) and opens the browser; stop
  shuts down PriceWatch and the SearXNG container, and with `--all` quits Ollama and Docker.
- **`autostart on|off`** (macOS): a LaunchAgent runs `start` at login so the bookmarklet
  always works.
- **In-app search**: type a product name in the watchlist box (or open Search) to find shop
  pages via SearXNG, with non-shop and list pages hidden, and add any with **Watch this**.
- Agent filters from a real run: skips social/video/news/review/price-comparison sites and
  search/category pages; only auto-adds prices from the shop's structured data (page-text
  prices go to "Add anyway"); ignores monthly/instalment offers in structured data; trusts a
  shared manufacturer model number over the AI; strips the shop's own name from searches.
- Scheduled re-checks re-judge other shops' prices (page text, under 60% or over 2x the
  product's price) and flag them; only you can clear a flag.
- Fair Price shows **evidence** for each profile's price (shop data vs page text, with the
  surrounding words) and a link to the page. Device-pricing warnings now need both prices from
  the shop's structured data; otherwise the verdict is "possible lower price, unconfirmed".
- Page reading skips used / other-seller / refurbished / renewed offers (Amazon-style pages).
- `pricewatch check URL --as android|iphone|windows|mac|linux` shows the price a device
  profile gets, how it was found, and the words around it.
- **Feedback for the AI**: mark each found shop *correct*, *wrong price* (with the corrected
  link) or *not the same product*, and leave notes. Notes become per-shop advice in the AI's
  prompt. Corrected links that add options (e.g. `?paymentTab=pay-in-full`) are learned per shop,
  and rejected links are never re-added.
- **Price doubt check**: new shops under 60% of the known price, or that the AI says aren't the
  full one-off price, are flagged "check this price" and excluded from "best elsewhere"/alerts
  until confirmed. The watchlist shows "⚠ N to check".
- **Add anyway**: shops the agent checked but skipped are kept with their price and reason.
- Better searches: titles are cleaned (repeated brands, prices, shop names removed), two
  queries are run and merged, a "UK" hint is added for GBP items, and up to 12 shops are checked.
- `pricewatch doctor`: checks the AI search setup (config, Ollama running, model downloaded
  and answering in JSON, SearXNG, browser), with a fix for each failure.
- `pricewatch find-better [ID]`: runs the AI price search from the terminal and shows what
  it added and the best price elsewhere.
- Cheaper prices come with a **link to the shop**: the watchlist's "Best elsewhere" price,
  the alert banner, and `find-better` output all link straight to the product page.
- Default local model is now `gemma4:e2b` (Ollama's quickstart model).
- **Alert price** on every watchlist row: set or clear it inline, see hit / near / how far to
  go, and a banner listing products at or below their alert (another shop's price counts too).

## 0.1.0 (2026-11-23)

First public release.

### v1: watch a price
- Paste or share a product link. The price is read from structured data (JSON-LD,
  microdata, meta tags), then from visible "price" elements and headings, skipping
  "was", monthly-finance and delivery amounts.
- Check frequency from minutes to a year, an optional target price, a background scheduler.
- Sharing: bookmarklet, Android/desktop share target (installable web app), iPhone
  Shortcut (`/api/share`).
- Shops that block automated requests (e.g. Argos) are read through an installed
  browser running headless with a throw-away profile: Chrome/Edge/Brave/Chromium,
  otherwise Firefox.
- `pricewatch check URL` and `pricewatch check-all` commands.

### v2: AI price search
- Self-hosted SearXNG search plus a local LLM (Ollama), an OpenAI-compatible server, or
  Anthropic. Every candidate price is fetched and verified by PriceWatch itself. The LLM
  only answers "same product?". Without an LLM, a heuristic match is used instead.

### v3: price history graph
- Server-side SVG step chart of the original and up to five alternatives, with hover
  crosshair, time ranges, legend, table view and dark mode.

### v4: Fair Price
- Personalised/surveillance pricing: the price you see (captured by the bookmarklet)
  compared with anonymous, cookie-free visits.
- Profile pricing: five device/language profiles (on shops without bot protection),
  plus optional proxy exits.
- Surge pricing: current price vs the 30-day median, and the cheapest weekday and time of day.

### Security
- Config and secrets live outside the repository. The `Secret` type never renders its value.
- AI-agent guardrails: `AGENTS.md`/`CLAUDE.md`, Claude Code deny rules, ignore files for
  other AI tools.
- Secret scanning: stdlib scanner, pre-commit hook, and GitHub Actions running gitleaks
  over the full history (a GitLab CI equivalent is included too).
- Web hardening: Host allowlist, CSRF, strict CSP, token auth off-loopback, SSRF guard.
