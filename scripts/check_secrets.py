#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""Zero-dependency secret scanner. Fails (exit 1) if anything secret-looking would be committed.

Usage:
  python scripts/check_secrets.py            # scan all git-tracked + staged files
  python scripts/check_secrets.py --staged   # pre-commit mode: staged files only
  python scripts/check_secrets.py --all      # every file under the repo (no git needed)

Complements (does not replace) gitleaks, which runs in CI (GitHub Actions / GitLab CI).
Matches are reported by file and line, with the value masked - never printed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_FILES = [
    re.compile(p) for p in (
        r"(^|/)\.env$", r"(^|/)\.env\.(?!example$)[^/]+$", r"(^|/)[^/]+\.env$",
        r"\.(pem|key|p12|pfx|db|sqlite3?|db-wal)$", r"(^|/)secrets/", r"(^|/)\.netrc$",
        r"(^|/)credentials[^/]*\.json$", r"(^|/)\.claude/settings\.local\.json$",
    )
]

PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI-style key": re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}"),
    "GitLab token": re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    "Slack token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "credentials in URL": re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s@'\"]{3,}@[^\s'\"]+", re.I),
    # assignment of a non-empty value to a sensitive-looking variable in env/config style files
    "secret assignment": re.compile(
        r"^[ \t]*(export[ \t]+)?[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|PROXIES)[A-Z0-9_]*[ \t]*[=:][ \t]*['\"]?[^\s'\"#$]{8,}",
        re.M),
}
ASSIGNMENT_FILES = re.compile(r"(\.env[^/]*|\.ya?ml|\.toml|\.ini|\.cfg|\.conf|\.json|\.sh|Dockerfile)$")
ALLOW_MARKER = "pw-secret-scan: allow"  # put on a line to allow a deliberate fake value (tests)
SKIP = {"scripts/check_secrets.py"}


def files(mode: str) -> list[str]:
    if mode == "--all":
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout  # noqa
    if mode == "--staged":
        out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        out = git("ls-files") + git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return sorted({f for f in out.splitlines() if f})


def mask(s: str) -> str:
    return s[:4] + "…" + f"({len(s)} chars)"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--tracked"
    problems = []
    for rel in files(mode):
        if rel in SKIP:
            continue
        if any(p.search(rel) for p in FORBIDDEN_FILES):
            problems.append(f"{rel}: this file type must never be committed")
            continue
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for name, rx in PATTERNS.items():
            if name == "secret assignment" and not ASSIGNMENT_FILES.search(rel):
                continue
            for m in rx.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                line = text.splitlines()[line_no - 1] if text else ""
                if ALLOW_MARKER in line:
                    continue
                problems.append(f"{rel}:{line_no}: possible {name}: {mask(m.group(0).strip())}")
    if problems:
        print("✗ Secret scan FAILED - nothing was committed.\n", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        print("\nMove real values to ~/.config/pricewatch/pricewatch.env (see SECURITY.md).", file=sys.stderr)
        return 1
    print("✓ Secret scan passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
