#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "Source this script: source ${BASH_SOURCE[0]}" >&2
    exit 1
fi

_ce_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_ce_venv="$_ce_root/venv"

if [[ -n "${VIRTUAL_ENV:-}" && "$VIRTUAL_ENV" != "$_ce_venv" ]]; then
    echo "Refusing to enter the CE venv from active venv '$VIRTUAL_ENV'." >&2
    unset _ce_root _ce_venv
    return 1
fi

_ce_binaries=(
    "venv/bin/gos-scenario"
    "bin/tinyinit"
    "bin/nginx"
    "bin/curl"
    "bin/wrk"
    "bin/wrk2"
    "bin/graalhost/graalhost"
    "bin/gvisor/runsc"
    "bin/firecracker/firecracker"
)

for _ce_binary in "${_ce_binaries[@]}"; do
    if [[ ! -x "$_ce_root/$_ce_binary" ]]; then
        echo "Missing executable: $_ce_root/$_ce_binary" >&2
        unset _ce_root _ce_venv _ce_binaries _ce_binary
        return 1
    fi
done

source "$_ce_venv/bin/activate"
export PATH="$_ce_root/bin:$_ce_root/bin/graalhost:$_ce_root/bin/gvisor:$_ce_root/bin/firecracker${PATH:+:$PATH}"
unset _ce_root _ce_venv _ce_binaries _ce_binary
