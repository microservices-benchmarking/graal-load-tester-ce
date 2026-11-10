#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/venv"

if [[ "$#" -eq 0 ]]; then
    echo "Usage: $0 COMMAND [ARG ...]" >&2
    exit 2
fi

if [[ -n "${VIRTUAL_ENV:-}" && "$VIRTUAL_ENV" != "$VENV" ]]; then
    echo "Refusing to enter the CE venv from active venv '$VIRTUAL_ENV'." >&2
    exit 1
fi

# Require the explicit one-shot setup instead of starting it from a command wrapper.
if [[ ! -x "$VENV/bin/gos-scenario" ]]; then
    echo "Benchmark environment is not installed." >&2
    echo "Run $SCRIPT_DIR/install.sh first." >&2
    exit 1
fi
if ! "$VENV/bin/python" -c \
    'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] < (3, 14)))' \
    >/dev/null 2>&1; then
    echo "Benchmark environment uses an unsupported Python version." >&2
    echo "Run $SCRIPT_DIR/install.sh to recreate it with Python 3.11, 3.12, or 3.13." >&2
    exit 1
fi

source "$SCRIPT_DIR/setup-env.sh"
exec "$@"
