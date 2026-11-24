# Security model

PriceWatch is designed so that **no secret ever appears in the repository, in an AI
agent's context, in logs, or in an LLM prompt.**

## 1. Where secrets live

| What | Where | Why |
|---|---|---|
| Config + secrets | `~/.config/pricewatch/pricewatch.env` (`chmod 600`) | Outside the repo: `git add .` can't pick it up, and an AI coding agent opened in the project folder never sees it |
| Price database | `~/.local/share/pricewatch/pricewatch.db` (`chmod 600`, dir `700`) | Your shopping history is personal data |
| Template | `.env.example` (committed, **empty values only**) | Documents every variable |

PriceWatch warns at startup if the env file is readable by other users. Values from
the file are **not** exported to `os.environ`, so subprocesses never inherit them.

Only four values are sensitive, all marked `[SECRET]` in `.env.example`:
`PW_ACCESS_TOKEN`, `PW_LLM_API_KEY`, `PW_FAIR_PROXIES`, `SEARXNG_SECRET`. With the
recommended fully-local setup (loopback + Ollama + SearXNG) you only need `SEARXNG_SECRET`.

## 2. Secrets inside the code

`config.Secret` wraps every sensitive value. `str()`, `repr()`, f-strings, logging and
pickling all show `***` or fail. The only way to get the value is `.reveal()`. It's
called only at the point of use (token checks, the LLM auth header, the proxy fetch), so audit
with `grep -rn "reveal()" pricewatch/`.

`python -m pricewatch config` prints effective config with secrets shown only as
`*_set: true/false`.

## 3. AI coding agents (Claude Code, Cursor, Copilot, Codex, Gemini, Aider…)

- `AGENTS.md` / `CLAUDE.md`: rules agents load automatically (never read env files,
  never dump the environment, use `python -m pricewatch config`).
- `.claude/settings.json`: Claude Code **deny** rules for `.env*`, the config and data
  folders, `printenv`/`env`, `sqlite3`, plus a rule stopping the agent from editing this
  settings file.
- `.aiexclude`, `.cursorignore`, `.codeiumignore`: the equivalents for other tools.

These are defence in depth. Ignore files and deny rules reduce accidental exposure, but
they are **not** a sandbox: a determined shell command can get around them. The real
guarantee is §1: the secrets aren't in the folder the agent works in. Don't paste
secrets into an agent chat, either.

## 4. The app's own AI agent (v2)

The LLM receives only truncated product titles, prices, URLs cut down to
`scheme://host/path`, and your own feedback notes about a shop (capped at 600 characters and
labelled as guidance, not instructions). It never receives query strings (they can carry session or
affiliate tokens), raw page HTML, config, or database contents. It has no tools. Its
output is parsed as strict JSON (`query`, or `same` + `full_price`), and every price it
"finds" is independently fetched and verified by PriceWatch. Text planted on a shop page
to inject instructions can, at worst, flip one yes/no match. You can delete any
alternative it adds.

Recommended: `PW_LLM_PROVIDER=ollama`, so nothing leaves your machine. If you choose
`anthropic` or a remote `openai_compatible` server, the titles and prices above are sent
to that provider.

## 5. The web server

Being on localhost doesn't make a server safe, because any website you visit can try
to reach it. So PriceWatch has:
- **Host-header allowlist:** blocks DNS-rebinding attacks.
- **CSRF token + Origin check on every form:** other sites can't add, delete, or change items.
- **Strict CSP** (`script-src 'self'`, no inline JS), `X-Frame-Options: DENY`, `nosniff`,
  `no-referrer`.
- Binds to `127.0.0.1` by default. It **refuses to start** on any other interface
  without a `PW_ACCESS_TOKEN` (≥ 24 chars, constant-time comparison, `HttpOnly`
  `SameSite=Strict` cookie).
- Request logs drop query strings.
- JSON API writes (`/api/share`) need `Authorization: Bearer <token>`.

LAN or phone access sends the token over plain HTTP. Put PriceWatch behind Tailscale,
or a TLS reverse proxy such as Caddy, rather than exposing port 8765 directly.

## 6. Outbound requests

Every product-page fetch goes through `fetcher.safe_get`:
- http/https only
- no embedded credentials
- refuses hosts that resolve to private, loopback, link-local or reserved addresses,
  with each redirect hop re-checked (SSRF guard)
- 5 MB / 20 s caps
- no cookies

When a shop blocks plain requests, PriceWatch can fall back to an installed browser:
Chrome, Edge, Brave or Chromium first, otherwise Firefox. It runs headless with a temporary
profile that is deleted afterwards, so your real browser's cookies, logins and history are
never touched. Extensions are disabled, the run has a timeout, and the URL is SSRF-checked
before launch, and for Firefox the final URL after redirects is checked too.

Firefox is driven over Marionette, its built-in automation protocol. While a fetch runs
(about 10 s), that throw-away Firefox listens on a random port on `127.0.0.1` only. It is
then shut down and its profile deleted.

When Fair Price falls back to a browser, it never disguises the browser's identity. A
browser claiming to be a different device trips bot protection, and an honest anonymous
visit is the meaningful comparison anyway.

Tracking parameters (`utm_*`, `fbclid`, …) and anything that looks like a session or
token are stripped from stored URLs.

## 7. Repository scanning

- `scripts/check_secrets.py`: stdlib scanner for forbidden files and key patterns.
  Masks matches and never prints them.
- `.pre-commit-config.yaml` / `scripts/pre-commit-hook.sh`: run the scanner (plus
  gitleaks if installed) before every commit.
- `.github/workflows/ci.yml`: tests on Python 3.9 and 3.12, the stdlib scanner, and
  gitleaks over the **full history** on every push and pull request.
- `.gitlab-ci.yml`: the same checks for a GitLab mirror, using GitLab Secret Detection.

**Also turn on in GitHub:** *Settings → Advanced Security → Secret Protection* and
*Push protection*. Both are free once the repository is public; on a private repository
they need GitHub Secret Protection. These block known secret formats before they land.

If a secret is ever committed, **rotate it first**, then rewrite history
(`git filter-repo`). Deleting it in a later commit is not enough.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → *Report a
vulnerability*) rather than a public issue.
