#!/usr/bin/env bash
# Format the Python code (like COLMAP's scripts/format/python.sh).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ruff="${RUFF:-.venv/bin/ruff}"
"$ruff" format prelude tests
"$ruff" check --fix prelude tests
