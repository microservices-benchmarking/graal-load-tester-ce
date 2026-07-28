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

if [[ ! -x "$VENV/bin/gos-scenario" ]]; then
    "$SCRIPT_DIR/install.sh"
fi

source "$SCRIPT_DIR/setup-env.sh"
exec "$@"
