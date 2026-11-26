# Contributing to PriceWatch

Thanks for helping! Bug reports, shop fixes, ideas and code are all welcome.

## Ground rules

- **Licence:** PriceWatch is AGPL-3.0-or-later. By opening a pull request you agree your
  contribution is licensed the same way. Only submit code you wrote or are allowed to
  license under AGPL.
- **No secrets, ever:** never commit `.env` files, tokens, API keys or databases. Run
  `python3 scripts/check_secrets.py` before committing (or install the pre-commit hook,
  see README). GitHub push protection and CI scan every change too.
- **Standard library only:** PriceWatch has no runtime dependencies, on purpose. Please
  open an issue before proposing one.
- **Be kind:** assume good intent, and keep discussion about the code.

## How changes get in

`main` is protected: every change, including the maintainer's own, arrives through a
**pull request** that can only be merged when all CI checks pass (tests on Python 3.9 and
3.12, plus the secret scans). History on `main` can't be rewritten or deleted, and
release tags can't be moved.

1. Fork the repo and create a branch.
2. Make your change, adding a test for any bug fix: `python3 -m unittest discover -s tests`
3. Update README / SECURITY.md / `.env.example` / CHANGELOG if behaviour or settings change.
4. Open a pull request and fill in the checklist.

Using an AI coding assistant? It will pick up [AGENTS.md](AGENTS.md); please keep to it.

## Security issues

Please report vulnerabilities **privately** via the repository's *Security → Report a
vulnerability*, not as a public issue. See [SECURITY.md](SECURITY.md).
