#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$REPO_ROOT/scripts"
APPARMOR_USERNS_PATH="/proc/sys/kernel/apparmor_restrict_unprivileged_userns"

# Detect Ubuntu's AppArmor user-namespace restriction before the long install.
if [[ -r "$APPARMOR_USERNS_PATH" ]] && [[ "$(<"$APPARMOR_USERNS_PATH")" == 1 ]]; then
    echo "Ubuntu AppArmor is restricting unprivileged user namespaces."
    echo "Rootless gVisor requires a runsc AppArmor profile that grants userns access,"
    echo "or this temporary workaround until reboot:"
    echo "  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
    if ! read -r -p "Press Enter to recheck and continue: "; then
        echo "Failed to read the AppArmor acknowledgement." >&2
        exit 1
    fi
    if [[ "$(<"$APPARMOR_USERNS_PATH")" == 1 ]]; then
        echo "Warning: the AppArmor restriction is still enabled; continuing." >&2
    else
        echo "The AppArmor restriction is now disabled."
    fi
fi

echo "==> Installing benchmark dependencies and building workloads"
echo "    Expected time: around 1 hour, depending on the host."
if ! "$SCRIPT_DIR/install.sh"; then
    echo "Installation failed." >&2
    exit 1
fi

# GraalHost is not public, so use an existing setting or ask for its location.
if [[ -z "${GRAALHOST_HOME:-}" ]]; then
    if ! read -r -p "Enter the GraalHost runtime directory: " GRAALHOST_HOME; then
        echo "Failed to read the GraalHost runtime directory." >&2
        exit 1
    fi
    if [[ -z "$GRAALHOST_HOME" ]]; then
        echo "The GraalHost runtime directory cannot be empty." >&2
        exit 1
    fi
    export GRAALHOST_HOME
fi
echo "==> Using GraalHost at $GRAALHOST_HOME"

echo "==> Sourcing the benchmark environment"
if ! source "$SCRIPT_DIR/setup-env.sh"; then
    echo "Environment setup failed." >&2
    exit 1
fi

echo "==> Running isolation smoke tests"
echo "    Expected time: about 10 minutes."
if ! "$SCRIPT_DIR/smoke-test.sh" --kernel "$FIRECRACKER_KERNEL"; then
    echo "Smoke tests failed." >&2
    exit 1
fi

echo "==> Running the full blog benchmark and generating tables"
echo "    Expected time: roughly 8 hours for the default three runs, depending on the host."
if ! "$SCRIPT_DIR/run-blog-benchmarks.py" --kernel "$FIRECRACKER_KERNEL"; then
    echo "Blog benchmark failed." >&2
    exit 1
fi

echo "==> Reproduction complete"
echo "    Generated tables: $REPO_ROOT/results/blog/tables.md"
