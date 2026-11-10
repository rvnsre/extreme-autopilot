#!/usr/bin/env bash
# Run with bash scripts/install.sh coordinator|worker. No sudo or permission changes.
set -euo pipefail
role="${1:-}"
if [[ "$role" != coordinator && "$role" != worker ]]; then
  echo 'Usage: bash scripts/install.sh coordinator|worker' >&2
  exit 2
fi
if [[ "$role" == worker && "${EA_ISOLATED_WORKER:-}" != true ]]; then
  echo 'Worker installation requires EA_ISOLATED_WORKER=true inside a dedicated VM/machine.' >&2
  exit 2
fi
case "$(uname -s)" in Linux|Darwin) ;; *) echo 'Use the PowerShell installers on Windows.' >&2; exit 2;; esac
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
"${PYTHON:-python3}" -m venv .venv
.venv/bin/python -m pip install --requirement "requirements-${role}.lock.txt"
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
if [[ "$role" == coordinator ]]; then
  (cd web && npm ci --no-audit --no-fund && npm run build)
else
  .venv/bin/python -m playwright install chromium
fi
echo 'Installed. Complete platform prerequisites and private configuration in docs/PLATFORMS.md.'
