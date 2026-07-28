#!/usr/bin/env bash
#
# Purpose:
#   Download a Firecracker-compatible uncompressed Linux kernel image
#   (`vmlinux-*`) for the current host architecture.
#
# What it does:
#   Resolves the latest Firecracker release line, queries the Firecracker CI
#   artifact bucket for the newest matching kernel image for the current
#   architecture, and downloads it into the requested output directory.
#
# Note:
#   This file was initially generated with AI assistance and then integrated
#   into the repository workflow.

set -euo pipefail

ARCH="${ARCH:-$(uname -m)}"
OUT_DIR="${1:-.}"
RELEASE_URL="https://github.com/firecracker-microvm/firecracker/releases"

mkdir -p "$OUT_DIR"

latest_version="$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' "${RELEASE_URL}/latest")")"
ci_version="${latest_version%.*}"

latest_kernel_key="$(
  curl -fsSL "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${ci_version}/${ARCH}/vmlinux-&list-type=2" \
    | grep -oP "(?<=<Key>)(firecracker-ci/${ci_version}/${ARCH}/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3})(?=</Key>)" \
    | sort -V \
    | tail -1
)"

if [[ -z "$latest_kernel_key" ]]; then
  echo "Could not resolve a Firecracker kernel image for ARCH=${ARCH} and CI version ${ci_version}." >&2
  exit 1
fi

wget -P "$OUT_DIR" "https://s3.amazonaws.com/spec.ccfc.min/${latest_kernel_key}"

