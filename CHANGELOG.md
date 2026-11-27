# Changelog

## Unreleased

- **Fair Price on Shopify shops** now checks the size/option your link selects. It compared
  the shop's lowest price (e.g. £2,430 for a smaller, canvas-only awning) with yours (£3,678);
  earlier checks of such links are ignored once a correct one exists.
- The bookmarklet captures the price of the size/option you've selected on Shopify shops (it
  sent the shop's lowest price, so Fair Price wrongly said you were shown less). Re-drag it.
  Prices read from Shopify size data are labelled "shop data", not "page text".
- The **+ PriceWatch** bookmarklet opens PriceWatch in a new tab, so the shop's page stays open
  (falls back to the same tab if the browser blocks it). Re-drag it from Share setup to update.
- **Shopify shops: the right size or option.** Shopify puts the product's lowest variant price
  in its page data, so a link to a bigger size read too low (a £1,979 awning showed £1,389).
  PriceWatch now reads the linked variant's own price from the shop's `/products/<name>.js`
  data (or the shop's default option when the link has no `?variant=`). The prices are read from
  the page itself where Shopify embeds them, so no extra request is needed (shops rate-limit).
- Phone deal and comparison sites (Smartphone Checker, Uswitch, MoneySuperMarket and others) are
  no longer treated as shops; the Google Store now is. Shops you've marked "wrong price" say why
  they're still listed and what to do.
- **Better phone matches.** The agent no longer adds refurbished, renewed, used, open-box or
  network contract listings next to a new SIM-free phone (it searches for "SIM free" and skips
  them, offering **Add anyway**). Ones added earlier are flagged at their next check and no
  longer count as cheaper. A shop's SIM-free page is still tried after its contract page is skipped.
- AI matching hardened against prompt injection from shop pages: titles and your notes are
  fenced as data the model must not take instructions from, answers must be true/false (otherwise
  the word-match fallback decides), and a "same product" verdict is only accepted when the names
  also agree by PriceWatch's own check (otherwise it's offered under the skipped shops with
  **Add anyway**). Emails, passwords, card/account and phone numbers in your notes are masked
  before they reach the model (reported by GitHub's AI security review).
- **Type the price yourself** for shops that block automatic checks (such as Currys): when a
  link can't be read, PriceWatch offers a form for the price you see, optionally compared with a
  product you already watch. Typed prices count in history, comparisons and alerts, are never
  fetched automatically, and are updated with an **Update** box. Alternatives whose checks fail
  link to the same form.
- **Fix: wrong prices from add-ons.** An Amazon phone was recorded at £115.99 (the
  "1-year Accidental Damage insurance" offer) instead of £1,139.99. Extraction now skips
  insurance, protection plans, warranties, AppleCare, trade-in, delivery and monthly
  ("/mo", "for 10 months") amounts, and prefers the shop's main price box (Amazon
  `priceToPay`/`corePrice`, "current price", "sale price" and similar).
- **Safety net for your own products:** a big jump or drop (under 60% or over 2×) read only
  from page text is held, not recorded. The watchlist shows **⚠ check price**; the product
  page shows where the price was found, with **Use** / **Keep** buttons.
- `docs/remote-access.md`: when (not) to enable remote access, the safe Tailscale setup
  (`tailscale serve` + HTTPS + access token, never Funnel), hardening, and what never to do.
  The iPhone Shortcut guide and SECURITY.md now follow it.
- PriceWatch refuses to start when `PW_ALLOWED_HOSTS` lists another device but no access
  token is set, and refuses proxied requests while no token is set (closes a gap with
  `tailscale serve`, reported by GitHub's AI security review). New `pricewatch set-token`
  writes and verifies the token in the env file.
- README screenshots (light and dark) made from demo data, plus
  `docs/screenshots/make_screenshots.py` to regenerate them.
- CI hardening: actions pinned to commit SHAs (kept current by Dependabot), checkout without
  persisted credentials, job time limits.
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
