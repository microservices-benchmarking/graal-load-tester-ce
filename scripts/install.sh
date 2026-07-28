#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/venv"
PYTHON="${GRAAL_LOAD_TESTER_PYTHON:-python3}"

if [[ -n "${VIRTUAL_ENV:-}" && "$VIRTUAL_ENV" != "$VENV" ]]; then
    echo "Refusing to create a nested virtual environment inside '$VIRTUAL_ENV'." >&2
    exit 1
fi

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "Creating virtual environment at $VENV"
    "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install -e "$REPO_ROOT"
"$VENV/bin/gos-scenario" --version
echo "Installation complete. Run: source $REPO_ROOT/scripts/setup-env.sh"
