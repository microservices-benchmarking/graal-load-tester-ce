#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "Source this script: source ${BASH_SOURCE[0]}" >&2
    exit 1
fi

_ce_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_ce_venv="$_ce_root/venv"
_ce_graalvm_graalos_home="${GRAALVM_GRAALOS_HOME:-$_ce_root/bin/graalvm/ce-graalos}"
_ce_graalvm_ni_home="${GRAALVM_NI_HOME:-$_ce_root/bin/graalvm/ni-ce}"
_ce_graalhost_home="${GRAALHOST_HOME:-}"
_ce_gvisor_home="${GVISOR_HOME:-$_ce_root/bin/gvisor}"
_ce_firecracker_home="${FIRECRACKER_HOME:-$_ce_root/bin/firecracker}"
_ce_firecracker_kernel="${FIRECRACKER_KERNEL:-$_ce_firecracker_home/vmlinux}"
_ce_wsl="${GRAAL_LOAD_TESTER_WSL:-false}"

if [[ "$_ce_wsl" != true && "$_ce_wsl" != false ]]; then
    echo "GRAAL_LOAD_TESTER_WSL must be 'true' or 'false', got: $_ce_wsl" >&2
    unset _ce_root _ce_venv _ce_graalvm_graalos_home _ce_graalvm_ni_home
    unset _ce_graalhost_home _ce_gvisor_home
    unset _ce_firecracker_home _ce_firecracker_kernel _ce_wsl
    return 1
fi

if [[ -r /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]] &&
    [[ "$(</proc/sys/kernel/apparmor_restrict_unprivileged_userns)" == 1 ]]; then
    echo "Warning: AppArmor restricts unprivileged user namespaces." >&2
    echo "Rootless gVisor requires a runsc AppArmor profile granting userns access." >&2
    echo "Temporary workaround until reboot:" >&2
    echo "  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" >&2
fi

if [[ -n "${VIRTUAL_ENV:-}" && "$VIRTUAL_ENV" != "$_ce_venv" ]]; then
    echo "Refusing to enter the CE venv from active venv '$VIRTUAL_ENV'." >&2
    unset _ce_root _ce_venv _ce_graalvm_graalos_home _ce_graalvm_ni_home
    unset _ce_graalhost_home _ce_gvisor_home
    unset _ce_firecracker_home _ce_firecracker_kernel _ce_wsl
    return 1
fi

if [[ -z "$_ce_graalhost_home" ]]; then
    echo "GraalHost/GraalVisor is not installed by this repository." >&2
    echo "Set GRAALHOST_HOME to the GraalVisor runtime directory before sourcing this script:" >&2
    echo "  export GRAALHOST_HOME=/path/to/graalvisor" >&2
    unset _ce_root _ce_venv _ce_graalvm_graalos_home _ce_graalvm_ni_home
    unset _ce_graalhost_home _ce_gvisor_home
    unset _ce_firecracker_home _ce_firecracker_kernel _ce_wsl
    return 1
fi

_ce_binaries=(
    "$_ce_root/venv/bin/python"
    "$_ce_root/venv/bin/gos-scenario"
    "$_ce_root/bin/tinyinit"
    "$_ce_root/bin/nginx"
    "$_ce_root/bin/curl"
    "$_ce_root/bin/wrk"
    "$_ce_root/bin/wrk2"
    "$_ce_graalhost_home/graalhost"
    "$_ce_gvisor_home/runsc"
    "$_ce_gvisor_home/gvisor-bin/gvisor_sentry"
    "$_ce_firecracker_home/firecracker"
    "$_ce_graalvm_graalos_home/bin/java"
    "$_ce_graalvm_graalos_home/bin/native-image"
    "$_ce_graalvm_ni_home/bin/java"
    "$_ce_graalvm_ni_home/bin/native-image"
)

for _ce_binary in "${_ce_binaries[@]}"; do
    if [[ ! -x "$_ce_binary" ]]; then
        echo "Missing executable: $_ce_binary" >&2
        unset _ce_root _ce_venv _ce_graalvm_graalos_home _ce_graalvm_ni_home
        unset _ce_graalhost_home _ce_gvisor_home
        unset _ce_firecracker_home _ce_firecracker_kernel _ce_wsl
        unset _ce_binaries _ce_binary
        return 1
    fi
done

# Reject a stale venv created with an unsupported Python version.
if ! "$_ce_venv/bin/python" -c \
    'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] < (3, 14)))' \
    >/dev/null 2>&1; then
    echo "The CE virtual environment must use Python 3.11, 3.12, or 3.13: $_ce_venv" >&2
    echo "Run $_ce_root/scripts/install.sh to recreate it." >&2
    unset _ce_root _ce_venv _ce_graalvm_graalos_home _ce_graalvm_ni_home
    unset _ce_graalhost_home _ce_gvisor_home
    unset _ce_firecracker_home _ce_firecracker_kernel _ce_wsl
    unset _ce_binaries _ce_binary
    return 1
fi

if [[ ! -f "$_ce_firecracker_kernel" ]]; then
    echo "Missing Firecracker guest kernel: $_ce_firecracker_kernel" >&2
    unset _ce_root _ce_venv _ce_graalvm_graalos_home _ce_graalvm_ni_home
    unset _ce_graalhost_home _ce_gvisor_home
    unset _ce_firecracker_home _ce_firecracker_kernel _ce_wsl
    unset _ce_binaries _ce_binary
    return 1
fi

source "$_ce_venv/bin/activate"
export PATH="$_ce_graalvm_ni_home/bin:$_ce_root/bin:$_ce_graalhost_home:$_ce_gvisor_home:$_ce_firecracker_home${PATH:+:$PATH}"
export GRAALVM_GRAALOS_HOME="$_ce_graalvm_graalos_home"
export GRAALVM_NI_HOME="$_ce_graalvm_ni_home"
export GRAALHOST_HOME="$_ce_graalhost_home"
export GVISOR_HOME="$_ce_gvisor_home"
export FIRECRACKER_HOME="$_ce_firecracker_home"
export FIRECRACKER_KERNEL="$_ce_firecracker_kernel"
export GRAAL_LOAD_TESTER_WSL="$_ce_wsl"
unset _ce_root _ce_venv _ce_graalvm_graalos_home _ce_graalvm_ni_home
unset _ce_graalhost_home _ce_gvisor_home
unset _ce_firecracker_home _ce_firecracker_kernel _ce_wsl
unset _ce_binaries _ce_binary
