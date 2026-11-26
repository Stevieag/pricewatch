# Instructions for AI coding agents

These rules apply to any AI assistant working in this repository (Claude Code, Codex,
Cursor, Copilot, Aider, Gemini, ...). They exist so contributors can use agents without
exposing credentials.

## Secrets — hard rules

1. **Never read, print, open, cat, grep, copy or summarise** any of:
   `.env`, `*.env` (except `.env.example`), `~/.config/pricewatch/**`,
   `~/.local/share/pricewatch/**`, `data/`, `*.db`, `secrets/`, `*.pem`, `*.key`.
2. **Never run** `env`, `printenv`, `set`, `export -p`, or anything that dumps environment
   variables or process environments (`/proc/*/environ`).
3. **Never write a real value** into any tracked file. Config changes go in
   `.env.example` with an **empty** value and a comment. Mark sensitive vars `[SECRET]`.
4. Need to know the config? Run `python -m pricewatch config` — it prints only whether
   secrets are set, never their values.
5. Any new sensitive setting must be wrapped in `config.Secret` and read only via
   `.reveal()` at the exact point of use (an HTTP header). Never pass a Secret into a
   log message, exception, HTML page, LLM prompt, or the database.
6. Before proposing a commit, run `python scripts/check_secrets.py`. It must pass.

## The app's own AI agent (v2)

What the LLM may receive is defined in `pricewatch/agent.py` (module docstring). Do not
widen it: no raw HTML, no query strings (use `util.url_for_ai`), no config, no database
dumps. The LLM must stay tool-less, with JSON-only output.

## Project conventions

- Licence: **AGPL-3.0-or-later**. New source files start with
  `# SPDX-License-Identifier: AGPL-3.0-or-later`. Only add code you are allowed to
  license under AGPL (no copying from incompatible sources). Keep the footer's
  *Source code* link working.

- Python ≥ 3.9, **standard library only** (no runtime dependencies — keeps the supply
  chain empty). Discuss before adding any dependency.
- Tests: `python -m unittest discover -s tests`. Add a test for every bug fix.
- Outbound fetches of product pages must go through `monitor.fetch_price` /
  `fetcher.safe_get` / `fetcher.browser_get` (SSRF guard, throw-away browser profile).
  Never point the browser fallback at the user's real browser profile.
- Before a release, run `python3 scripts/live_test.py` against a real shop.
- Keep `README.md`, `SECURITY.md`, `.env.example` and `CHANGELOG.md` in step with code changes.
