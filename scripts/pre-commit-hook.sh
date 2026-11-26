#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Plain git hook (no pre-commit framework needed):
#   ln -s ../../scripts/pre-commit-hook.sh .git/hooks/pre-commit
set -e
cd "$(git rev-parse --show-toplevel)"
python3 scripts/check_secrets.py --staged
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks protect --staged --redact --no-banner
fi
