#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
KERNEL="${FIRECRACKER_KERNEL:-}"
OUTPUT_DIR="$REPO_ROOT/results/smoke"
DRY_RUN=false

usage() {
    echo "Usage: $0 --kernel VMLINUX [--output-dir DIR] [--dry-run]"
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --kernel) KERNEL="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

[[ "$DRY_RUN" == true || -f "$KERNEL" ]] || {
    echo "Firecracker kernel not found; pass --kernel VMLINUX." >&2
    exit 2
}

WORKLOADS=(
    "http-handler-mn:http-handler-mn-1.0-SNAPSHOT"
    "micronaut-shopcart-function:micronaut-shopcart-function"
    "micronaut-similarity-function:micronaut-similarity-function"
    "quarkus-hello-world-function:quarkus-hello-world-function"
    "spring-hello-world-function:spring-hello-world-function"
    "spring-petclinic-function:spring-petclinic-function"
)

for deployment in nginx-tinyinit-graalhost nginx-tinyinit-gvisor nginx-tinyinit-firecracker; do
    for entry in "${WORKLOADS[@]}"; do
        workload="${entry%%:*}"
        binary="${entry#*:}"
        scenario="$REPO_ROOT/scenarios/graal-ci/$deployment/single-app/$workload-2048.toml"
        run_dir="$OUTPUT_DIR/$deployment/$workload"
        command=(
            "$SCRIPT_DIR/run_under_venv.sh" gos-scenario "$scenario" --skip-upload
            -p "app_base_dir=$REPO_ROOT"
            -p "app_binary_path=workloads/$binary"
            -p single_app_total_requests=1
            -p single_app_request_iteration=1
            -p single_app_request_interval=0
            -p single_app_request_start_offset=0
            -p single_app_latency_generators_count=0
            -p single_app_throughput_count=0
        )
        [[ "$deployment" != nginx-tinyinit-firecracker ]] \
            || command+=(-p "app_firecracker_kernel_image=$KERNEL")

        echo "==> $deployment / $workload"
        if [[ "$DRY_RUN" == true ]]; then
            printf '%q ' "${command[@]}"
            echo
        else
            mkdir -p "$run_dir"
            (cd "$run_dir" && "${command[@]}")
        fi
    done
done
