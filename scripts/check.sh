#!/bin/sh
# Lint, format and type checks: the same ones CI runs. Fast enough for a
# commit hook; the tests are separate (python -m pytest).
#
#   scripts/check.sh         check
#   scripts/check.sh --fix   apply lint fixes and formatting, then check
#
# Expects .venv with requirements-test.txt and requirements-lint.txt:
#   uv venv && uv pip install -r requirements-test.txt -r requirements-lint.txt
set -eu
cd "$(dirname "$0")/.."
bin=.venv/bin
if [ ! -x "$bin/ruff" ] || [ ! -x "$bin/pyright" ]; then
    echo "scripts/check.sh: no .venv with ruff and pyright; see the top of this file" >&2
    exit 1
fi
if [ "${1:-}" = "--fix" ]; then
    "$bin/ruff" check --fix .
    "$bin/ruff" format .
fi
"$bin/ruff" check .
"$bin/ruff" format --check .
"$bin/pyright" --pythonpath "$bin/python"
