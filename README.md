# Graal Load Tester CE

This repository contains the local-only subset of Graal Load Tester used to
compare native workloads with four deployment modes:

- `nginx-tinyinit`
- `nginx-tinyinit-graalhost`
- `nginx-tinyinit-gvisor`
- `nginx-tinyinit-firecracker`

Included workload scenario families:

- `http-handler-mn`
- `micronaut-shopcart-function`
- `micronaut-similarity-function`
- `quarkus-hello-world-function`
- `spring-hello-world-function`
- `spring-petclinic-function`

Service, dataplane, remote load-generator, and container-publishing scenarios,
configuration values, and command-line entry points are intentionally not
included. Their execution modules are replaced with fail-closed compatibility
stubs so shared parser, metric, and result types remain importable without
shipping the non-local command implementations.

## Layout

- `gos_harness/`: load-test runtime.
- `scenarios/`: single-app, round-robin, and smoke-test scenarios with their
  shared templates.
- `scripts/`: load-generator and Firecracker helpers.
- `workloads/`: native-image workload executables.
- `bin/{tinyinit,wrk,wrk2}`: bundled load-test executables.
- `bin/nginx`: nginx 1.18.0 executable downloaded by the install script.
- `bin/curl`: curl 8.10.1 executable downloaded by the install script.
- `bin/libpcre.so.3`: nginx compatibility library downloaded by the install
  script.
- `bin/graalhost/`: empty placeholder for the GraalHost runtime (currently not
  public); provide an external runtime through `GRAALHOST_HOME`.
- `bin/gvisor/`: gVisor installation downloaded by the install script.
- `bin/firecracker/`: Firecracker and guest-kernel installation downloaded by
  the install script.
- `bin/graalvm/{ce-graalos,ni-ce}`: GraalVM installations built from the two
  distinct public CE configurations by the install script.

## One script reproduction

To install the benchmark inputs, run the isolation smoke tests, execute the
full benchmark, and generate the blog tables in one workflow, run:

```bash
./reproduce-blog-results.sh
```

The script uses `GRAALHOST_HOME` when it is already set or prompts for the
external GraalHost runtime directory. The generated tables are written to
`results/blog/tables.md`.

## Prerequisites

The installer checks required host commands and reports the corresponding
Ubuntu/Debian package. On Ubuntu/Debian, install the host commands used by
setup, workload builds, the local harness, and Firecracker bundle generation with:

```bash
sudo apt-get install autoconf automake binutils bzip2 build-essential cmake \
  coreutils curl dpkg e2fsprogs file findutils gawk git grep gzip libc-bin lsof \
  maven ninja-build patch perl pkg-config procps python3.12 python3.12-venv sed \
  tar unzip xz-utils zip
```

Python 3.11 through 3.13 is supported and enforced; the current harness is
incompatible with Python 3.14. The downloaded benchmark artifacts target
x86-64.

Graalhost/visor must be provided using `GRAALHOST_HOME`.

Firecracker requires `/dev/kvm` to exist and be readable and writable by
the current user. [Rootless gVisor](https://gvisor.dev/docs/user_guide/rootless/)
requires enabled user namespaces. KVM and user namespaces are runtime
prerequisites checked when the isolation smoke tests run. If Ubuntu's AppArmor
policy blocks them, temporarily disable the restriction until reboot with
`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. The preferred
solution is an AppArmor profile that grants `userns,` to the exact `runsc` path;
see [Ubuntu's guidance](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces).

Upstream gVisor does not support WSL1 or WSL2. The environment helper defaults
`GRAAL_LOAD_TESTER_WSL` to `false`; set it explicitly to `true` if needed. The
harness then uses gVisor's KVM platform for those WSL checks. WSL results are
not benchmark evidence. Use bare-metal for authoritative reproduction.

### Isolation runtimes

GraalHost/GraalVisor is not public, so this repository neither includes nor
downloads its binary or source. Set `GRAALHOST_HOME` to an externally supplied
runtime directory containing an executable named `graalhost`.

The installer pins gVisor `release-20261026.0`, Firecracker `v1.11.0`, and the
Firecracker CI `vmlinux-5.10.225` guest kernel. The gVisor installation includes
the sidecar executables under `gvisor-bin/` required by current releases.

The environment helper exports `GRAALHOST_HOME`, `GVISOR_HOME`,
`FIRECRACKER_HOME`, and `FIRECRACKER_KERNEL`. The kernel variable points to
`bin/firecracker/vmlinux` and is used automatically by the smoke-test and
blog-benchmark helpers. The harness builds the guest root filesystem separately
for each workload.


## Manual workflow

### Setup

```bash
./scripts/install.sh
export GRAALHOST_HOME=/path/to/graalvisor
source ./scripts/setup-env.sh
```

The installer creates a fresh `venv`, installs the local package, and
verifies `gos-scenario`. It clones pinned public Graal and mx revisions under
`downloads/graalvm-source/`, uses mx to fetch the exact pinned LabsJDK, and
builds the separate `ce-graalos` and `ni-ce` configurations from source. The
resulting distributions are installed under `bin/graalvm/ce-graalos` and
`bin/graalvm/ni-ce`. It then builds all six workloads in both GraalOS and
ordinary Native Image form under `workloads/graalos/` and `workloads/native/`.

The repository includes the x86-64 `tinyinit`, `wrk`, and `wrk2` executables.
The setup downloads pinned versions of nginx, curl, the PCRE compatibility
library, gVisor, Firecracker, and the Firecracker guest kernel. The installed
files are copied under `bin/`. The installer is a one-shot clean-checkout setup:
it clears its generated `bin/` outputs and `venv` before installing.

To run one command without modifying the current shell, use:

```bash
./scripts/run_under_venv.sh gos-scenario --version
```

This requires a completed installation and runs the command with both the
virtual environment and bundled executables on `PATH`.

### Smoke test

Run all six workloads under GraalVisor, gVisor, and Firecracker with one
request each:

```bash
./scripts/smoke-test.sh
```

### Blog benchmarks

Run the process baseline and all three isolation variants, then generate the
three Markdown tables used by the blog. The complete matrix runs three times
by default, retains each repetition under `results/blog/run-N/`, and reports
the arithmetic mean of each cell's per-run value:

```bash
./scripts/run-blog-benchmarks.py
```

Restrict execution and output to one table with `--table memory`,
`--table throughput`, or `--table cold-start`. Change the number of complete
matrix runs with `--repetitions N`. Use `--tables-only` with the same
repetition count to regenerate `results/blog/tables.md` from existing results.

### Running individual experiments

The scenario files default to each workload's source-tree layout. Point them
at any executable under `workloads/` with parameter overrides:

```bash
gos-scenario \
  scenarios/graal-ci/nginx-tinyinit/single-app/http-handler-mn-2048.toml \
  --skip-upload \
  -p app_base_dir="$PWD" \
  -p app_binary_path=workloads/native/http-handler-mn
```

Change the scenario directory to select another isolation mode:

- `nginx-tinyinit-graalhost`
- `nginx-tinyinit-gvisor`
- `nginx-tinyinit-firecracker`

Use `workloads/graalos/<name>` with GraalHost and `workloads/native/<name>`
with the other deployment modes.

Replace `single-app` with `round-robin` for the 100-application experiment.
Small validation scenarios are also available under `test/` for
`nginx-tinyinit` and `nginx-tinyinit-graalhost`.

For Firecracker, also provide the guest kernel:

```bash
-p app_firecracker_kernel_image="$FIRECRACKER_KERNEL"
```

For another workload, override its path and name:

```bash
-p app_binary_path=workloads/native/spring-hello-world-function \
-p app_name=spring-hello-world-function
```

Results are written to `bench-results.json`.

## License

Copyright (c) 2026 Oracle and/or its affiliates.

The original project materials are licensed under the Apache License 2.0.
See [LICENSE](LICENSE) and [NOTICE](NOTICE). Separately distributed third-party
components remain subject to their respective license terms.
