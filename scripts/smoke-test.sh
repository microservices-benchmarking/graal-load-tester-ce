#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
KERNEL="${FIRECRACKER_KERNEL:-}"
OUTPUT_DIR="$REPO_ROOT/results/smoke"
DRY_RUN=false

# Print the supported smoke-test command-line options.
usage() {
    echo "Usage: $0 --kernel VMLINUX [--output-dir DIR] [--dry-run]"
}

# Read the kernel, output-directory, and dry-run selections.
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --kernel) KERNEL="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

# A real run needs the Firecracker guest kernel before any matrix cell starts.
[[ "$DRY_RUN" == true || -f "$KERNEL" ]] || {
    echo "Firecracker kernel not found; pass --kernel VMLINUX." >&2
    exit 2
}
if [[ -n "$KERNEL" ]]; then
    KERNEL="$(realpath -m "$KERNEL")"
fi

# Print one shell-safe command that reruns a matrix cell in its result directory.
# Parameters:
#   $1: Result directory used as the command's working directory.
#   $2...: Command and arguments to execute.
print_cell_command() {
    local run_dir="$1"
    shift

    printf '  (cd %q && ' "$run_dir"
    printf '%q ' "$@"
    echo ')'
}

# Workloads represented in each isolation-runtime smoke matrix.
WORKLOADS=(
    http-handler-mn
    micronaut-shopcart-function
    micronaut-similarity-function
    quarkus-hello-world-function
    spring-hello-world-function
    spring-petclinic-function
)
TOTAL_TESTS=$((3 * ${#WORKLOADS[@]}))

# Run every workload under GraalHost, gVisor, and Firecracker.
for deployment in nginx-tinyinit-graalhost nginx-tinyinit-gvisor nginx-tinyinit-firecracker; do
    # GraalHost consumes GraalOS images; the other runtimes consume ordinary
    # Linux Native Images.
    if [[ "$deployment" == nginx-tinyinit-graalhost ]]; then
        workload_variant=graalos
    else
        workload_variant=native
    fi

    for workload in "${WORKLOADS[@]}"; do
        # Build a one-request scenario command for this matrix cell.
        scenario="$REPO_ROOT/scenarios/graal-ci/$deployment/single-app/$workload-2048.toml"
        run_dir="$OUTPUT_DIR/$deployment/$workload"
        command=(
            "$SCRIPT_DIR/run_under_venv.sh" gos-scenario "$scenario" --skip-upload
            -p "app_base_dir=$REPO_ROOT"
            -p "app_binary_path=workloads/$workload_variant/$workload"
            -p single_app_total_requests=1
            -p single_app_request_iteration=1
            -p single_app_request_interval=0
            -p single_app_request_start_offset=0
            -p single_app_latency_generators_count=0
            -p single_app_throughput_count=0
        )
        # Only Firecracker needs the guest kernel as a scenario parameter.
        [[ "$deployment" != nginx-tinyinit-firecracker ]] \
            || command+=(-p "app_firecracker_kernel_image=$KERNEL")

        echo "==> $deployment / $workload"
        # Dry runs print shell-safe commands. Real runs remove stale evidence
        # before executing the cell in its own result directory.
        if [[ "$DRY_RUN" == true ]]; then
            print_cell_command "$run_dir" "${command[@]}"
        else
            mkdir -p "$run_dir"
            rm -f "$run_dir/bench-results.json"
            if (cd "$run_dir" && "${command[@]}"); then
                continue
            else
                status=$?
                case "$deployment" in
                    nginx-tinyinit-graalhost) runtime=GraalHost ;;
                    nginx-tinyinit-gvisor) runtime=gVisor ;;
                    nginx-tinyinit-firecracker) runtime=Firecracker ;;
                esac
                echo >&2
                echo "Smoke test failed: $runtime / $workload ($deployment)." >&2
                echo "Rerun only this test:" >&2
                print_cell_command "$run_dir" "${command[@]}" >&2
                exit "$status"
            fi
        fi
    done
done

if [[ "$DRY_RUN" == true ]]; then
    echo "Smoke-test dry run complete: printed $TOTAL_TESTS commands."
else
    echo "Smoke tests passed: all $TOTAL_TESTS deployment/workload tests succeeded."
fi
