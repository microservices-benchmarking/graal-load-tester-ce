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
- `bin/{tinyinit,nginx,curl,wrk,wrk2}`: bundled load-test executables.
- `bin/graalhost/`: GraalVisor runtime files.
- `bin/gvisor/runsc`: gVisor runtime.
- `bin/firecracker/firecracker`: Firecracker runtime.

## Setup

Python 3.10 or newer is required.

```bash
./scripts/install.sh
source ./scripts/setup-env.sh
```

The installer creates or reuses `venv`, installs the local package, and
verifies `gos-scenario`. Set `GRAAL_LOAD_TESTER_PYTHON` to choose a specific
Python executable.

The repository includes x86-64 builds of `nginx`, `tinyinit`, `curl`, `wrk`,
and `wrk2`. Firecracker additionally requires a usable `/dev/kvm`, an
uncompressed Linux guest kernel, and the filesystem tools used by
`scripts/firecracker/make-firecracker-bundle.sh`.

### Firecracker kernel

Download the latest Firecracker CI kernel for the current architecture into
the ignored `downloads/` directory:

```bash
./scripts/firecracker/download-latest-kernel.sh downloads/firecracker
FIRECRACKER_KERNEL="$(
  find "$PWD/downloads/firecracker" -maxdepth 1 -name 'vmlinux-*' | sort -V | tail -n 1
)"
export FIRECRACKER_KERNEL
```

The smoke-test and blog-benchmark helpers use `FIRECRACKER_KERNEL`
automatically. The kernel is an uncompressed `vmlinux` image; the harness
builds the guest root filesystem separately for each workload.

The environment helper verifies `gos-scenario` and the bundled executables,
then prepends their directories to `PATH`. It can also be sourced by absolute
path from another directory.

To run one command without modifying the current shell, use:

```bash
./scripts/run_under_venv.sh gos-scenario --version
```

This installs the package first when necessary and runs the command with both
the virtual environment and bundled executables on `PATH`.

### Newer tinyinit

The checked-in configuration remains compatible with the older tinyinit used
by CI. On machines using tinyinit 0.1.32 or newer, apply its new activation
schema on demand:

```bash
./scripts/use-new-tinyinit.sh
```

The workaround changes only the local working tree and is safe to run again.

## Smoke test

Run all six workloads under GraalVisor, gVisor, and Firecracker with one
request each:

```bash
./scripts/smoke-test.sh --kernel /path/to/vmlinux
```

`FIRECRACKER_KERNEL` can be set instead of passing `--kernel`.

## Blog benchmarks

Run the process baseline and all three isolation variants, then generate the
three Markdown tables used by the blog:

```bash
./scripts/run-blog-benchmarks.py --kernel /path/to/vmlinux
```

Restrict execution and output to one table with `--table memory`,
`--table throughput`, or `--table cold-start`. Use `--tables-only` to
regenerate `results/blog/tables.md` from existing results.

## Running the experiments

The scenario files default to each workload's source-tree layout. Point them
at any executable under `workloads/` with parameter overrides:

```bash
gos-scenario \
  scenarios/graal-ci/nginx-tinyinit/single-app/http-handler-mn-2048.toml \
  --skip-upload \
  -p app_base_dir="$PWD" \
  -p app_binary_path=workloads/http-handler-mn-1.0-SNAPSHOT
```

Change the scenario directory to select another isolation mode:

- `nginx-tinyinit-graalhost`
- `nginx-tinyinit-gvisor`
- `nginx-tinyinit-firecracker`

Replace `single-app` with `round-robin` for the 100-application experiment.
Small validation scenarios are also available under `test/` for
`nginx-tinyinit` and `nginx-tinyinit-graalhost`.

For Firecracker, also provide the guest kernel:

```bash
-p app_firecracker_kernel_image=/path/to/vmlinux
```

For another workload, override its path and name:

```bash
-p app_binary_path=workloads/spring-hello-world-function \
-p app_name=spring-hello-world-function
```

Results are written to `bench-results.json`.

## License

Copyright (c) 2026 Oracle and/or its affiliates.

The original project materials are licensed under the Apache License 2.0.
See [LICENSE](LICENSE) and [NOTICE](NOTICE). Separately distributed third-party
components remain subject to their respective license terms.
