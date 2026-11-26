# Repository protection (maintainer checklist)

These GitHub settings protect `main` from mistakes and from malicious changes. Applying
them is a manual step: GitHub settings deliberately can't be changed by AI tools working
on the repository.

## 1. Rulesets (Settings → Rules → Rulesets)

| Ruleset | What it does | File to import |
|---|---|---|
| Default Rules (existing) | `main` can't be deleted or force-pushed | already active |
| **main: pull request + passing CI (no bypass)** | Every change arrives by pull request (squash only, linear history, resolved conversations) and can only merge when `tests (3.9, …)`, `tests (3.12, …)` and `secret-scan` pass. No one can bypass it, including admins | `.github/rulesets/main-pr-and-ci.json` |
| **release tags: can't be moved or deleted** | Tags `v*` can be created but never moved or deleted | `.github/rulesets/release-tags.json` |

To import: **New ruleset → Import a ruleset**, choose the file, check it, then **Create**.

> If a CI job is ever renamed (e.g. a new Python version), edit the ruleset's required
> checks to match. A required check that never reports would block all merges. Editing
> rulesets is always possible for the repo admin, so this can't lock you out permanently.

## 2. General settings (Settings → General)

- Pull requests: allow **squash merging** only. Turn on **Automatically delete head branches**.
- Features: turn off **Wikis** and **Projects** (unused, and less to moderate).

## 3. Actions (Settings → Actions → General)

- **Fork pull request workflows from outside collaborators:** *Require approval for all
  external contributors*, so strangers' PRs can't run workflows unreviewed.
- **Workflow permissions:** *Read repository contents and packages permissions*. Leave
  "Allow GitHub Actions to create and approve pull requests" **off**.

## 4. Security (Settings → Advanced Security)

Already on: secret scanning, push protection, Dependabot security updates, private
vulnerability reporting. Also turn on, where offered: **Non-provider patterns** and
**Validity checks** for secret scanning, and **Dependabot version updates** (configured by
`.github/dependabot.yml`).

## 5. Your account

- Two-factor authentication (passkey or authenticator app) on your GitHub account.
- Review apps with access to the repo (Settings → GitHub Apps / Integrations), and give each
  the least access it needs.
