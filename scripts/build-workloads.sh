#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_ROOT="$REPO_ROOT/workloads/src"
OUTPUT_ROOT="$REPO_ROOT/workloads"

# Workloads represented in the blog benchmark matrix.
WORKLOADS=(
    http-handler-mn
    micronaut-shopcart-function
    micronaut-similarity-function
    quarkus-hello-world-function
    spring-hello-world-function
    spring-petclinic-function
)

# Verify that one configured GraalVM role provides the Java launcher and
# Native Image compiler required by the workload builds.
# Parameters:
#   $1: Name of the environment variable containing the GraalVM home.
require_graalvm() {
    local variable_name="$1"
    local graalvm_home="${!variable_name:-}"

    [[ -n "$graalvm_home" ]] || {
        echo "$variable_name is unset." >&2
        return 1
    }
    [[ -x "$graalvm_home/bin/java" ]] || {
        echo "$variable_name does not contain an executable bin/java: $graalvm_home" >&2
        return 1
    }
    [[ -x "$graalvm_home/bin/native-image" ]] || {
        echo "$variable_name does not contain an executable bin/native-image: $graalvm_home" >&2
        return 1
    }
}

# Both compiler roles are required because this script always builds everything.
require_graalvm GRAALVM_GRAALOS_HOME
require_graalvm GRAALVM_NI_HOME

# Build one workload with the compiler and build profile for the requested
# output variant, then install the resulting executable under workloads/.
# Parameters:
#   $1: Output variant to build: graalos or native.
#   $2: Workload directory and executable name.
build_workload() {
    local variant_name="$1"
    local workload_name="$2"
    local graalvm_home build_profile source_dir app_dir built_binary output_binary

    source_dir="$SOURCE_ROOT/$workload_name"
    app_dir="$source_dir/app"
    output_binary="$OUTPUT_ROOT/$variant_name/$workload_name"

    # Select the compiler role and workload build profile for this variant.
    case "$variant_name" in
        graalos)
            graalvm_home="$GRAALVM_GRAALOS_HOME"
            build_profile="aot-graalos"
            ;;
        native)
            graalvm_home="$GRAALVM_NI_HOME"
            build_profile="aot"
            ;;
    esac

    echo "==> $variant_name / $workload_name"
    rm -f "$output_binary"

    # Build from a clean application directory with the selected GraalVM first
    # on PATH. The Maven workload uses its build helper; the others use Gradle.
    (
        export GRAALVM_HOME="$graalvm_home"
        export JAVA_HOME="$graalvm_home"
        export PATH="$graalvm_home/bin:$PATH"
        cd "$app_dir"
        set -x
        if [[ "$workload_name" == "http-handler-mn" ]]; then
            "$source_dir/build.sh" "$build_profile"
        elif [[ "$variant_name" == "graalos" ]]; then
            ./gradlew clean nativeCompile \
                -PbuildProfile=aot-graalos \
                "-Dorg.gradle.java.home=$graalvm_home"
        else
            ./gradlew clean nativeCompile \
                "-Dorg.gradle.java.home=$graalvm_home"
        fi
    )

    # Resolve the executable location produced by the workload's build system.
    if [[ "$workload_name" == "http-handler-mn" ]]; then
        built_binary="$app_dir/target/http-handler-mn-1.0-SNAPSHOT"
    else
        built_binary="$app_dir/build/native/nativeCompile/$workload_name"
    fi

    # Reject a missing or non-executable build result, then install it at the
    # stable path consumed by the smoke-test and benchmark scripts.
    [[ -x "$built_binary" ]] || {
        echo "Build did not produce the expected executable: $built_binary" >&2
        return 1
    }
    install -D -m 0755 "$built_binary" "$output_binary"
}

# Build all six workloads in both GraalOS and ordinary Native Image form.
for variant_name in graalos native; do
    for workload_name in "${WORKLOADS[@]}"; do
        build_workload "$variant_name" "$workload_name"
    done
done
