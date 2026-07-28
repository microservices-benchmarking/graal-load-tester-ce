#!/usr/bin/env bash
#
# Purpose:
#   Bundle builder for the Graal Load Tester Firecracker local deployment.
#
# What it does:
#   Packages a native binary and its dependencies into a Firecracker rootfs,
#   builds the guest shim, writes the Firecracker config, and emits the helper
#   script used to launch the host bridge.
#
# Note:
#   This file was initially generated with AI assistance and then integrated
#   into the repository workflow.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  make-firecracker-bundle.sh \
    --binary /path/to/app \
    --kernel /path/to/vmlinux \
    --out-dir /path/to/out [options]

Creates a shimmed Firecracker bundle intended for:

  stdin -> host bridge -> Firecracker vsock -> guest shim -> /app -> stdout

This is the minimal topology needed to preserve tinyinit-style "send request
immediately on stdin" semantics while the guest is still booting.

Options:
  --binary PATH           Path to the app binary to run as /app inside the guest
  --kernel PATH           Path to an uncompressed guest kernel image (vmlinux)
  --out-dir PATH          Output directory
  --firecracker-bin PATH  Firecracker binary used by generated helpers (default: firecracker)
  --guest-port N          Guest vsock port listened to by the shim (default: 9000)
  --guest-cid N           Firecracker guest CID (default: 52)
  --mem-mib N             Guest memory in MiB (default: 512)
  --vcpu-count N          Guest vCPU count (default: 1)
  --rootfs-size-mib N     Explicit rootfs size in MiB
  --app-arg VALUE         App argument passed to /app inside the guest (repeatable)
  --help                  Show help
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

copy_abs_path() {
  local src="$1"
  local dst_root="$2"
  local dst="${dst_root}${src}"
  mkdir -p "$(dirname "$dst")"
  cp -L "$src" "$dst"
}

collect_ldd_paths() {
  local bin="$1"
  ldd "$bin" 2>/dev/null | awk '
    /=> \// { print $3 }
    /^\// { print $1 }
  ' | sort -u
}

collect_interp_path() {
  local bin="$1"
  readelf -l "$bin" 2>/dev/null | sed -n 's@.*Requesting program interpreter: \(.*\)]@\1@p' | sort -u
}

add_dynamic_dependencies() {
  local bin="$1"
  local dst_root="$2"
  local dep
  while IFS= read -r dep; do
    [[ -n "$dep" ]] || continue
    [[ -e "$dep" ]] || continue
    copy_abs_path "$dep" "$dst_root"
  done < <(
    {
      collect_interp_path "$bin"
      collect_ldd_paths "$bin"
    } | sort -u
  )
}

compute_rootfs_size_mib() {
  local tree="$1"
  local used_kib used_mib
  used_kib="$(du -sk "$tree" | awk '{print $1}')"
  used_mib="$(( (used_kib + 1023) / 1024 ))"
  if (( used_mib < 32 )); then
    used_mib=32
  fi
  echo $(( used_mib * 4 + 64 ))
}

BINARY=""
KERNEL=""
OUT_DIR=""
FIRECRACKER_BIN="firecracker"
GUEST_PORT=9000
GUEST_CID=52
MEM_MIB=512
VCPU_COUNT=1
ROOTFS_SIZE_MIB=""
HOST_BRIDGE_PYTHON="${HOST_BRIDGE_PYTHON:-python3}"
APP_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --binary)
      BINARY="$2"
      shift 2
      ;;
    --kernel)
      KERNEL="$2"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    --firecracker-bin)
      FIRECRACKER_BIN="$2"
      shift 2
      ;;
    --guest-port)
      GUEST_PORT="$2"
      shift 2
      ;;
    --guest-cid)
      GUEST_CID="$2"
      shift 2
      ;;
    --mem-mib)
      MEM_MIB="$2"
      shift 2
      ;;
    --vcpu-count)
      VCPU_COUNT="$2"
      shift 2
      ;;
    --rootfs-size-mib)
      ROOTFS_SIZE_MIB="$2"
      shift 2
      ;;
    --app-arg)
      APP_ARGS+=("$2")
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$BINARY" || -z "$KERNEL" || -z "$OUT_DIR" ]]; then
  usage >&2
  exit 1
fi

need_cmd gcc
need_cmd "$HOST_BRIDGE_PYTHON"
need_cmd readelf
need_cmd ldd
need_cmd realpath
need_cmd truncate
need_cmd mkfs.ext4
need_cmd cksum

if [[ ! -f "$BINARY" || ! -x "$BINARY" ]]; then
  echo "Binary missing or not executable: $BINARY" >&2
  exit 1
fi
if [[ ! -f "$KERNEL" ]]; then
  echo "Kernel does not exist: $KERNEL" >&2
  exit 1
fi

if [[ "$FIRECRACKER_BIN" = */* ]]; then
  if [[ ! -x "$FIRECRACKER_BIN" ]]; then
    echo "Firecracker binary is not executable: $FIRECRACKER_BIN" >&2
    exit 1
  fi
  FIRECRACKER_BIN="$(realpath "$FIRECRACKER_BIN")"
else
  FIRECRACKER_BIN="$(command -v "$FIRECRACKER_BIN" || true)"
  if [[ -z "$FIRECRACKER_BIN" ]]; then
    echo "Could not resolve firecracker binary on PATH" >&2
    exit 1
  fi
fi

mkdir -p "$OUT_DIR"
ROOTFS_TREE="$OUT_DIR/rootfs-tree"
ROOTFS_IMG="$OUT_DIR/rootfs.ext4"
CONFIG_JSON="$OUT_DIR/firecracker.json"
RUN_BRIDGE="$OUT_DIR/run-bridge.sh"
HOST_BRIDGE="$OUT_DIR/host_bridge.py"
GUEST_SHIM_BIN="$OUT_DIR/guest-shim"
APP_ARGV_FILE="$ROOTFS_TREE/etc/firecracker-app-argv"
DIGEST="$(printf '%s' "$(realpath "$OUT_DIR")" | cksum | awk '{print $1}')"
VSOCK_UDS="/tmp/fc-vsock-${DIGEST}.sock"
API_SOCK="/tmp/fc-api-${DIGEST}.sock"

rm -rf "$ROOTFS_TREE"
mkdir -p "$ROOTFS_TREE"

gcc -O2 -Wall -Wextra -o "$GUEST_SHIM_BIN" "$SCRIPT_DIR/guest_shim.c"
cp "$SCRIPT_DIR/host_bridge.py" "$HOST_BRIDGE"
chmod 0755 "$HOST_BRIDGE"

# Create a filesystem containing the workload + its dependencies
#  - this will be run in the firecracker
mkdir -p "$ROOTFS_TREE"/{dev,proc,sys,tmp,run,root}
cp -L "$GUEST_SHIM_BIN" "$ROOTFS_TREE/init"
chmod 0755 "$ROOTFS_TREE/init"
cp -L "$BINARY" "$ROOTFS_TREE/app"
chmod 0755 "$ROOTFS_TREE/app"
mkdir -p "$(dirname "$APP_ARGV_FILE")"
if [[ ${#APP_ARGS[@]} -gt 0 ]]; then
  # Store guest argv as NUL-delimited entries so the shim can reconstruct execv() args without shell parsing.
  printf '%s\0' "${APP_ARGS[@]}" > "$APP_ARGV_FILE"
fi

add_dynamic_dependencies "$GUEST_SHIM_BIN" "$ROOTFS_TREE"
add_dynamic_dependencies "$BINARY" "$ROOTFS_TREE"

if [[ -z "$ROOTFS_SIZE_MIB" ]]; then
  ROOTFS_SIZE_MIB="$(compute_rootfs_size_mib "$ROOTFS_TREE")"
fi

rm -f "$ROOTFS_IMG"
truncate -s "${ROOTFS_SIZE_MIB}M" "$ROOTFS_IMG"
mkfs.ext4 -q -d "$ROOTFS_TREE" -F "$ROOTFS_IMG"

# Pass guest shim settings through the kernel cmdline because /init inside the guest is the shim entrypoint.
cat > "$CONFIG_JSON" <<EOF
{
  "boot-source": {
    "kernel_image_path": "$(realpath "$KERNEL")",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off quiet loglevel=0 root=/dev/vda rw init=/init shim.port=${GUEST_PORT} shim.app=/app shim.debug=1"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "$(realpath "$ROOTFS_IMG")",
      "is_root_device": true,
      "is_read_only": false
    }
  ],
  "machine-config": {
    "vcpu_count": ${VCPU_COUNT},
    "mem_size_mib": ${MEM_MIB}
  },
  "vsock": {
    "vsock_id": "vsock0",
    "guest_cid": ${GUEST_CID},
    "uds_path": "${VSOCK_UDS}"
  }
}
EOF

cat > "$RUN_BRIDGE" <<EOF
#!/usr/bin/env bash
set -euo pipefail
DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
# Keep socket names runtime-unique, while the bundle directory and guest port stay fixed at generation time.
SUFFIX="\$\$-\${RANDOM}"
VSOCK_UDS="/tmp/fc-vsock-${DIGEST}-\${SUFFIX}.sock"
API_SOCK="/tmp/fc-api-${DIGEST}-\${SUFFIX}.sock"
# Resolve the host-side Python lazily so tinyinit can override it through the environment if needed.
HOST_BRIDGE_PYTHON="\${HOST_BRIDGE_PYTHON:-python3}"
exec "\$HOST_BRIDGE_PYTHON" "\$DIR/host_bridge.py" \
  --config "\$DIR/firecracker.json" \
  --vsock-uds "\${VSOCK_UDS}" \
  --guest-port ${GUEST_PORT} \
  --firecracker-bin "${FIRECRACKER_BIN}" \
  --api-sock "\${API_SOCK}" \
  \${FC_DEBUG:+--debug} \
  \${FC_SHOW_SERIAL:+--show-serial}
EOF
chmod 0755 "$RUN_BRIDGE"

cat <<EOF
Created shimmed Firecracker bundle in: $OUT_DIR

Artifacts:
  guest shim       : $GUEST_SHIM_BIN
  rootfs image     : $ROOTFS_IMG
  firecracker json : $CONFIG_JSON
  host bridge      : $HOST_BRIDGE
  runner           : $RUN_BRIDGE

Run:
  $RUN_BRIDGE

Debug:
  FC_DEBUG=1 FC_SHOW_SERIAL=1 $RUN_BRIDGE

Notes:
  - The host bridge is the intended tinyinit child process.
  - It starts reading stdin immediately and buffers while Firecracker boots.
  - The guest shim accepts the request over vsock and execs /app with stdin/stdout pipes.
  - The default Firecracker API/vsock sockets are placed under /tmp to avoid Unix socket path length limits.
EOF
