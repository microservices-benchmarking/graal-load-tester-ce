#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/venv"
PYTHON="${GRAAL_LOAD_TESTER_PYTHON:-}"

# Select the oldest available supported Python unless the user chose one.
if [[ -z "$PYTHON" ]]; then
    for python_candidate in python3.11 python3.12 python3.13; do
        if command -v "$python_candidate" >/dev/null 2>&1; then
            PYTHON="$python_candidate"
            break
        fi
    done
    PYTHON="${PYTHON:-python3.11}"
fi

# Public source inputs for the two GraalVM distributions built by setup.
GRAALVM_SOURCE_ROOT="$REPO_ROOT/downloads/graalvm-source"
GRAAL_REPO="$GRAALVM_SOURCE_ROOT/graal"
GRAAL_REPO_URL="https://github.com/oracle/graal.git"
GRAAL_REVISION="014eb69901bdffaba4a24a13a4af9f7f26b3e14a"
MX_REPO="$GRAALVM_SOURCE_ROOT/mx"
MX_REPO_URL="https://github.com/graalvm/mx.git"
MX_REVISION="ef054fc6eca1d516820bd040efa3c0a0f6ef1899"
GRAALVM_BUILD_ROOT="$REPO_ROOT/build/graalvm"
GRAALVM_MX_CACHE="$REPO_ROOT/downloads/mx-cache"
GRAALVM_INSTALL_ROOT="$REPO_ROOT/bin/graalvm"
GRAALVM_GRAALOS_INSTALL_DIR="$GRAALVM_INSTALL_ROOT/ce-graalos"
GRAALVM_NI_INSTALL_DIR="$GRAALVM_INSTALL_ROOT/ni-ce"
LABSJDK_INSTALL_ROOT="$REPO_ROOT/downloads/labsjdk"
LABSJDK_SELECTOR="ce-25.0.4.1.1+1-jvmci-25.5-b24"
LABSJDK_ALIAS="$LABSJDK_INSTALL_ROOT/labsjdk"

# Pinned benchmark executables and nginx's compatibility library.
TOOLS_DOWNLOAD_DIR="$REPO_ROOT/downloads/tools"
CURL_RELEASE="8.10.1"
CURL_ARCHIVE="curl-linux-x86_64-musl-$CURL_RELEASE.tar.xz"
CURL_URL="https://github.com/stunnel/static-curl/releases/download/$CURL_RELEASE/$CURL_ARCHIVE"
NGINX_PACKAGE_VERSION="1.18.0-6ubuntu14.18"
NGINX_PACKAGE="nginx-core_${NGINX_PACKAGE_VERSION}_amd64.deb"
NGINX_URL="https://launchpad.net/ubuntu/+archive/primary/+files/$NGINX_PACKAGE"
PCRE_PACKAGE_VERSION="8.39-13ubuntu0.22.04.1"
PCRE_PACKAGE="libpcre3_${PCRE_PACKAGE_VERSION}_amd64.deb"
PCRE_URL="https://security.ubuntu.com/ubuntu/pool/main/p/pcre3/$PCRE_PACKAGE"

# Pinned isolation runtimes and the separate Firecracker guest kernel.
GVISOR_DOWNLOAD_DIR="$REPO_ROOT/downloads/gvisor"
GVISOR_INSTALL_DIR="$REPO_ROOT/bin/gvisor"
GVISOR_RELEASE="release-20261026.0"
GVISOR_ARCHIVE="gvisor-x86_64.tar.bz2"
GVISOR_URL="https://github.com/google/gvisor/releases/download/$GVISOR_RELEASE/$GVISOR_ARCHIVE"
FIRECRACKER_DOWNLOAD_DIR="$REPO_ROOT/downloads/firecracker"
FIRECRACKER_INSTALL_DIR="$REPO_ROOT/bin/firecracker"
FIRECRACKER_RELEASE="v1.11.0"
FIRECRACKER_ARCHIVE="firecracker-$FIRECRACKER_RELEASE-x86_64.tgz"
FIRECRACKER_URL="https://github.com/firecracker-microvm/firecracker/releases/download/$FIRECRACKER_RELEASE/$FIRECRACKER_ARCHIVE"
FIRECRACKER_BINARY="firecracker-$FIRECRACKER_RELEASE-x86_64"
FIRECRACKER_KERNEL_NAME="vmlinux-5.10.225"
FIRECRACKER_KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.11/x86_64/$FIRECRACKER_KERNEL_NAME"

# Fail early when required host build tools or Python support are missing.
check_prerequisites() {
    local requirement command_name package_name architecture response
    local -a missing_requirements=()
    local requirements=(
        ar:binutils
        awk:gawk
        autoconf:autoconf
        automake:automake
        bash:bash
        bzip2:bzip2
        cat:coreutils
        cmake:cmake
        chmod:coreutils
        cksum:coreutils
        cp:coreutils
        c++:build-essential
        curl:curl
        date:coreutils
        dpkg-deb:dpkg
        du:coreutils
        file:file
        find:findutils
        gcc:build-essential
        git:git
        grep:grep
        gzip:gzip
        install:coreutils
        ldd:libc-bin
        ln:coreutils
        lsof:lsof
        make:build-essential
        mkdir:coreutils
        mkfs.ext4:e2fsprogs
        mktemp:coreutils
        mv:coreutils
        mvn:maven
        ninja:ninja-build
        patch:patch
        perl:perl
        pgrep:procps
        pkg-config:pkg-config
        ps:procps
        ranlib:binutils
        readelf:binutils
        realpath:coreutils
        rm:coreutils
        sed:sed
        sort:coreutils
        strip:binutils
        tar:tar
        truncate:coreutils
        uname:coreutils
        unzip:unzip
        xargs:findutils
        xz:xz-utils
        zip:zip
    )

    # Report every missing command together with its Ubuntu/Debian package.
    echo "Checking host prerequisites..."
    for requirement in "${requirements[@]}"; do
        command_name="${requirement%%:*}"
        package_name="${requirement#*:}"
        if ! command -v "$command_name" >/dev/null 2>&1; then
            missing_requirements+=("$command_name (Ubuntu/Debian package: $package_name)")
        fi
    done

    if ! command -v "$PYTHON" >/dev/null 2>&1; then
        missing_requirements+=("$PYTHON (install Python 3.11, 3.12, or 3.13, or set GRAAL_LOAD_TESTER_PYTHON)")
    fi

    if (( ${#missing_requirements[@]} > 0 )); then
        echo "Missing required host commands:" >&2
        printf '  - %s\n' "${missing_requirements[@]}" >&2
        return 1
    fi

    # The pinned executable downloads support x86-64 only. Warn rather than
    # rejecting an unexpected architecture name that the user knows is compatible.
    architecture="$(uname -m)"
    if [[ "$architecture" != x86_64 && "$architecture" != amd64 ]]; then
        echo "Warning: this installer provides x86-64 artifacts, but uname -m reports '$architecture'." >&2
        if ! read -r -p "Continue with the x86-64 artifacts anyway? [y/N] " response; then
            echo "Failed to read the architecture confirmation." >&2
            return 1
        fi
        if [[ "${response,,}" != y && "${response,,}" != yes ]]; then
            echo "Installation stopped before downloading x86-64 artifacts." >&2
            return 1
        fi
    fi

    # Require a supported Python version with venv support.
    "$PYTHON" -c \
        'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] < (3, 14)))' \
        >/dev/null 2>&1 || {
        echo "Python 3.11, 3.12, or 3.13 is required: $PYTHON reports $("$PYTHON" --version 2>&1)" >&2
        echo "Set GRAAL_LOAD_TESTER_PYTHON to a supported Python executable." >&2
        return 1
    }
    "$PYTHON" -c 'import ensurepip, venv' >/dev/null 2>&1 || {
        echo "Python venv/ensurepip support is missing for $PYTHON." >&2
        return 1
    }

    echo "Host prerequisites satisfied."
}

# Download one pinned upstream artifact.
# Parameters:
#   $1: Artifact URL.
#   $2: Local path where the artifact is downloaded.
#   $3: Human-readable artifact description used in progress output.
download_artifact() {
    local url="$1"
    local output="$2"
    local description="$3"

    mkdir -p "$(dirname "$output")"
    echo "Downloading $description: $url"
    curl --fail --location --output "$output" "$url"
}

# Start the one-shot installation without prior downloads or generated outputs.
clear_install_outputs() {
    rm -f "$REPO_ROOT/bin/curl" "$REPO_ROOT/bin/nginx" \
        "$REPO_ROOT/bin/libpcre.so.3"
    rm -rf "$REPO_ROOT/downloads" "$GRAALVM_BUILD_ROOT" \
        "$GRAALVM_INSTALL_ROOT" "$GVISOR_INSTALL_DIR" \
        "$FIRECRACKER_INSTALL_DIR" "$VENV"
    mkdir -p "$GRAALVM_INSTALL_ROOT" "$GVISOR_INSTALL_DIR" \
        "$FIRECRACKER_INSTALL_DIR"
    touch "$GRAALVM_INSTALL_ROOT/.gitkeep" "$GVISOR_INSTALL_DIR/.gitkeep" \
        "$FIRECRACKER_INSTALL_DIR/.gitkeep"
}

# Install the pinned curl, nginx, and PCRE benchmark inputs.
install_benchmark_tools() {
    local curl_archive nginx_package pcre_package extract_dir
    local curl_binary="$REPO_ROOT/bin/curl"
    local nginx_binary="$REPO_ROOT/bin/nginx"
    local pcre_library="$REPO_ROOT/bin/libpcre.so.3"

    curl_archive="$TOOLS_DOWNLOAD_DIR/$CURL_ARCHIVE"
    nginx_package="$TOOLS_DOWNLOAD_DIR/$NGINX_PACKAGE"
    pcre_package="$TOOLS_DOWNLOAD_DIR/$PCRE_PACKAGE"

    # Download the exact benchmark inputs selected above.
    download_artifact "$CURL_URL" "$curl_archive" \
        "pinned static curl release"
    download_artifact "$NGINX_URL" "$nginx_package" \
        "pinned Ubuntu nginx package"
    download_artifact "$PCRE_URL" "$pcre_package" \
        "pinned Ubuntu PCRE compatibility package"

    # Extract the packages and copy the runtime files with their final permissions.
    extract_dir="$TOOLS_DOWNLOAD_DIR/extracted"
    mkdir -p "$extract_dir/curl" "$extract_dir/nginx" "$extract_dir/pcre"
    tar -xJf "$curl_archive" -C "$extract_dir/curl"
    dpkg-deb -x "$nginx_package" "$extract_dir/nginx"
    dpkg-deb -x "$pcre_package" "$extract_dir/pcre"
    install -m 0755 "$extract_dir/curl/curl" "$curl_binary"
    install -m 0755 "$extract_dir/nginx/usr/sbin/nginx" "$nginx_binary"
    install -m 0644 \
        "$extract_dir/pcre/lib/x86_64-linux-gnu/libpcre.so.3.13.3" \
        "$pcre_library"
    # The installed files are self-contained; discard archives and staging.
    rm -rf "$TOOLS_DOWNLOAD_DIR"

    echo "curl $CURL_RELEASE installed at $curl_binary"
    echo "nginx $NGINX_PACKAGE_VERSION installed at $nginx_binary"
    echo "PCRE $PCRE_PACKAGE_VERSION installed at $pcre_library"
}

# Clone one public repository at the selected revision.
# Parameters:
#   $1: Repository name used in progress output.
#   $2: Repository clone URL.
#   $3: Exact Git revision to check out.
#   $4: Local checkout directory.
clone_repository_at_revision() {
    local name="$1"
    local url="$2"
    local revision="$3"
    local directory="$4"

    # A fresh installation clones once and checks out the exact selected commit.
    mkdir -p "$(dirname "$directory")"
    echo "Cloning $name"
    git clone "$url" "$directory"
    git -C "$directory" checkout --detach "$revision"
    echo "$name source ready at $directory ($revision)"
}

# Fetch the exact bootstrap LabsJDK through mx.
fetch_labsjdk() {
    mkdir -p "$LABSJDK_INSTALL_ROOT"
    echo "Fetching pinned LabsJDK $LABSJDK_SELECTOR through mx..."
    # Run from the Graal checkout so mx uses its matching JDK catalog.
    (
        cd "$GRAAL_REPO"
        "$MX_REPO/mx" --quiet fetch-jdk \
            --to "$LABSJDK_INSTALL_ROOT" \
            --alias labsjdk \
            labsjdk-ce-latest \
            "$LABSJDK_SELECTOR"
    )

    echo "LabsJDK $LABSJDK_SELECTOR installed at $LABSJDK_ALIAS"
}

# Build only the requested mx GraalVM distribution graph, then install it.
# Parameters:
#   $1: mx environment selecting the GraalVM distribution configuration.
#   $2: Directory where the completed distribution is installed.
build_and_install_graalvm_distribution() {
    local configuration="$1"
    local install_dir="$2"
    local distribution source_home

    echo "Building the pinned $configuration GraalVM distribution..."
    (
        cd "$GRAAL_REPO/vm"
        # Resolve and build only the requested distribution dependency graph.
        distribution="$("$MX_REPO/mx" --quiet --env "$configuration" graalvm-dist-name)"
        "$MX_REPO/mx" --env "$configuration" build \
            --dependencies "$distribution" --build-logs=silent
        # Copy the completed mx distribution into its stable bin/ location.
        source_home="$("$MX_REPO/mx" --quiet --env "$configuration" graalvm-home)"
        mkdir -p "$install_dir"
        cp -a "$source_home/." "$install_dir/"
        echo "$configuration installed at $install_dir"
    )
}

# Acquire the public build inputs and build separate GraalOS and Native Image toolchains.
install_graalvm() {
    clone_repository_at_revision "mx" "$MX_REPO_URL" "$MX_REVISION" "$MX_REPO"
    clone_repository_at_revision "Graal" "$GRAAL_REPO_URL" "$GRAAL_REVISION" "$GRAAL_REPO"

    # Keep mx downloads and its Python interpreter explicit and repository-local.
    export MX_CACHE_DIR="$GRAALVM_MX_CACHE"
    export MX_PYTHON="$PYTHON"
    fetch_labsjdk

    # Use the fetched LabsJDK and keep all generated mx output under build/.
    export JAVA_HOME="$LABSJDK_ALIAS"
    export MX_ALT_OUTPUT_ROOT="$GRAALVM_BUILD_ROOT"
    export PATH="$MX_REPO:$JAVA_HOME/bin${PATH:+:$PATH}"
    mkdir -p "$GRAALVM_BUILD_ROOT"

    # Build distinct compiler installations for GraalOS and ordinary images.
    build_and_install_graalvm_distribution \
        "ce-graalos" "$GRAALVM_GRAALOS_INSTALL_DIR"
    build_and_install_graalvm_distribution \
        "ni-ce" "$GRAALVM_NI_INSTALL_DIR"
}

# Install the complete pinned gVisor release bundle.
install_gvisor() {
    local archive_path extract_dir

    archive_path="$GVISOR_DOWNLOAD_DIR/$GVISOR_ARCHIVE"
    download_artifact "$GVISOR_URL" "$archive_path" \
        "pinned gVisor release"

    # Install the complete bundle because runsc requires its sidecar binaries.
    extract_dir="$GVISOR_DOWNLOAD_DIR/extracted"
    mkdir -p "$extract_dir"
    tar -xjf "$archive_path" -C "$extract_dir"
    cp -a "$extract_dir/." "$GVISOR_INSTALL_DIR/"
    # Only the installed bundle is needed after extraction.
    rm -rf "$GVISOR_DOWNLOAD_DIR"

    echo "gVisor $GVISOR_RELEASE installed at $GVISOR_INSTALL_DIR"
}

# Install Firecracker together with its separate guest kernel.
install_firecracker() {
    local archive_path extract_dir kernel_cache kernel_install

    archive_path="$FIRECRACKER_DOWNLOAD_DIR/$FIRECRACKER_ARCHIVE"
    kernel_cache="$FIRECRACKER_DOWNLOAD_DIR/$FIRECRACKER_KERNEL_NAME"
    kernel_install="$FIRECRACKER_INSTALL_DIR/vmlinux"
    download_artifact "$FIRECRACKER_URL" "$archive_path" \
        "pinned Firecracker release"
    download_artifact "$FIRECRACKER_KERNEL_URL" \
        "$kernel_cache" "pinned Firecracker guest kernel"

    # Install the release bundle, stable launcher name, and separate guest kernel.
    extract_dir="$FIRECRACKER_DOWNLOAD_DIR/extracted"
    mkdir -p "$extract_dir"
    tar -xzf "$archive_path" -C "$extract_dir" --strip-components=1
    ln -s "$FIRECRACKER_BINARY" "$extract_dir/firecracker"
    cp -a "$extract_dir/." "$FIRECRACKER_INSTALL_DIR/"
    cp "$kernel_cache" "$kernel_install"
    # The installed bundle and kernel under bin/ are the runtime inputs.
    rm -rf "$FIRECRACKER_DOWNLOAD_DIR"
    echo "Firecracker $FIRECRACKER_RELEASE installed at $FIRECRACKER_INSTALL_DIR"
    echo "Firecracker guest kernel installed at $kernel_install"
}

# Acquire and build every installation prerequisite.
check_prerequisites
clear_install_outputs
install_benchmark_tools
install_graalvm
install_gvisor
install_firecracker

# Install the harness only after its native/runtime inputs are ready.
echo "Creating virtual environment with $("$PYTHON" --version 2>&1) at $VENV"
"$PYTHON" -m venv "$VENV"

"$VENV/bin/python" -m pip install -e "$REPO_ROOT"
"$VENV/bin/gos-scenario" --version
echo "Building GraalOS and ordinary Native Image workload executables..."
# Build both workload forms with their role-specific GraalVM installations.
GRAALVM_GRAALOS_HOME="$GRAALVM_GRAALOS_INSTALL_DIR" \
GRAALVM_NI_HOME="$GRAALVM_NI_INSTALL_DIR" \
    "$SCRIPT_DIR/build-workloads.sh"
echo "Installation complete."
echo "GraalOS workloads: $REPO_ROOT/workloads/graalos"
echo "Native workloads: $REPO_ROOT/workloads/native"
echo "GraalHost/GraalVisor is not public and was not installed."
echo "Set GRAALHOST_HOME to your GraalVisor runtime directory, then activate the environment:"
echo "  export GRAALHOST_HOME=/path/to/graalvisor"
echo "  source $REPO_ROOT/scripts/setup-env.sh"
