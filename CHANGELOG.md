# Changelog

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
