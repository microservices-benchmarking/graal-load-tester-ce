#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKLOADS = (
    "http-handler-mn",
    "micronaut-shopcart-function",
    "micronaut-similarity-function",
    "quarkus-hello-world-function",
    "spring-hello-world-function",
    "spring-petclinic-function",
)
DEPLOYMENTS = {
    "nginx-tinyinit": "Process",
    "nginx-tinyinit-graalhost": "GraalVisor",
    "nginx-tinyinit-gvisor": "gVisor",
    "nginx-tinyinit-firecracker": "Firecracker",
}
TABLES = ("cold-start", "memory", "throughput")
DEFAULT_REPETITIONS = 3
MEMORY_AGENT_SCRIPTS = (
    ROOT / "scripts/agents/global_agent_meminfo.sh",
    ROOT / "scripts/agents/pid_agent_smaps.sh",
)
CellKey = tuple[str, str, str]
RepeatedCellKey = tuple[int, str, str, str]


def benchmark_groups(selected: set[str]) -> list[tuple[str, str]]:
    """Select the scenario group and memory size required by each table.

    Args:
        selected: Table names requested for the run.
    """
    groups = []
    if selected & {"cold-start", "throughput"}:
        groups.append(("single-app", "2048"))
    if "memory" in selected:
        groups.append(("round-robin", "256"))
    return groups


def workload_path(deployment: str, workload: str) -> Path:
    """Return the workload variant consumed by one deployment.

    Args:
        deployment: Scenario deployment name.
        workload: Workload executable name.
    """
    variant = "graalos" if deployment == "nginx-tinyinit-graalhost" else "native"
    return Path("workloads") / variant / workload


def scenario_path(deployment: str, group: str, workload: str, memory: str) -> Path:
    """Return the scenario file for one benchmark cell.

    Args:
        deployment: Scenario deployment name.
        group: Scenario group, such as ``single-app`` or ``round-robin``.
        workload: Workload scenario name.
        memory: Scenario memory size in MiB.
    """
    return ROOT / "scenarios/graal-ci" / deployment / group / f"{workload}-{memory}.toml"


def result_rows(path: Path) -> list[dict]:
    """Load and de-duplicate metric rows from one result file.

    Args:
        path: Benchmark result JSON file.
    """
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    rows = payload["queries"] if isinstance(payload, dict) else payload
    unique: dict[str, dict] = {}
    for row in rows:
        unique.setdefault(str(row.get("metric.uuid", id(row))), row)
    return list(unique.values())


def result_errors(path: Path, rows: list[dict]) -> list[str]:
    """Describe failed scenario, load-tester, or HTTP result metrics.

    Args:
        path: Result file used to qualify error messages.
        rows: Metric rows loaded from the result file.
    """
    errors = []
    scenario_success = [row for row in rows if row.get("metric.name") == "scenario-success"]
    load_tester_success = [row for row in rows if row.get("metric.name") == "load-tester-success"]
    request_codes = [row for row in rows if row.get("metric.name") == "request-code-count"]

    if not scenario_success or any(float(row.get("metric.value", 0)) != 1 for row in scenario_success):
        errors.append("scenario-success is missing or not 1")
    if not load_tester_success or any(float(row.get("metric.value", 0)) != 1 for row in load_tester_success):
        errors.append("load-tester-success is missing or not 1")
    if not any(
        row.get("http.code") == 200 and float(row.get("metric.value", 0)) > 0
        for row in request_codes
    ):
        errors.append("no successful HTTP 200 request was recorded")
    failed_codes = sorted({
        row.get("http.code")
        for row in request_codes
        if row.get("http.code") != 200 and float(row.get("metric.value", 0)) > 0
    }, key=str)
    if failed_codes:
        errors.append(f"non-200 request codes were recorded: {failed_codes}")
    return [f"{path}: {error}" for error in errors]


def validate_result(path: Path) -> list[dict]:
    """Validate one result file and return its metric rows.

    Args:
        path: Benchmark result JSON file to validate.
    """
    if not path.is_file():
        raise RuntimeError(f"benchmark did not produce {path}")
    try:
        rows = result_rows(path)
        errors = result_errors(path, rows)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"could not validate benchmark result {path}: {exc}") from exc
    if errors:
        raise RuntimeError("invalid benchmark result:\n  - " + "\n  - ".join(errors))
    return rows


def metric_value(table: str, rows: list[dict]) -> float | None:
    """Reduce metric rows to the value displayed in one table cell.

    Args:
        table: Table whose aggregation rule is applied.
        rows: Validated metric rows for one benchmark cell.
    """
    if table == "cold-start":
        values = [
            float(row["metric.value"])
            for row in rows
            if row.get("metric.name") == "request-time"
            and row.get("metric.iteration") == 0
            and row.get("load-tester.method-type") == "requests"
        ]
        return max(values) if values else None
    if table == "throughput":
        values = [float(row["metric.value"]) for row in rows if row.get("metric.name") == "throughput"]
        return max(values) if values else None

    by_process: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        process = str(row.get("app.process-name", ""))
        if row.get("metric.name") == "max-pss" and process.lower() != "tinyinit":
            by_process[process].append(float(row["metric.value"]))
    return max((statistics.mean(values) for values in by_process.values()), default=None)


def result_path(output: Path, table: str, deployment: str, workload: str) -> Path:
    """Return the result file used by one generated table cell.

    Args:
        output: Root directory containing benchmark results.
        table: Table selecting the scenario group.
        deployment: Scenario deployment name.
        workload: Workload result directory name.
    """
    group = "round-robin" if table == "memory" else "single-app"
    return output / group / deployment / workload / "bench-results.json"


def render_table(
    table: str,
    output: Path,
    repetitions: int,
    failures: dict[RepeatedCellKey, str] | None = None,
) -> str:
    """Render one Markdown result table.

    Args:
        table: Table to render.
        output: Root directory containing benchmark results.
        repetitions: Number of benchmark repetitions to aggregate.
        failures: Matrix cells that failed during the current run.
    """
    titles = {
        "cold-start": "Worst cold start [ms]",
        "memory": "Dominant process max-PSS [MB]",
        "throughput": "Peak throughput [op/s]",
    }
    units = {
        "cold-start": "ms",
        "memory": "MB",
        "throughput": "op/s",
    }
    columns = list(DEPLOYMENTS.values())
    lines = [
        f"## {titles[table]}",
        "",
        f"Values are arithmetic means across {repetitions} benchmark runs; "
        "multipliers are relative to Process.",
        "",
        f"| Workload | {' | '.join(columns)} |",
        f"| --- | {' | '.join(['---:'] * len(columns))} |",
    ]
    for workload in WORKLOADS:
        values_by_deployment: dict[str, float | None] = {}
        failed_deployments = set()
        for deployment in DEPLOYMENTS:
            group = "round-robin" if table == "memory" else "single-app"
            if failures and any(
                (repetition, group, deployment, workload) in failures
                for repetition in range(1, repetitions + 1)
            ):
                values_by_deployment[deployment] = None
                failed_deployments.add(deployment)
                continue
            values = []
            for repetition in range(1, repetitions + 1):
                path = result_path(output / f"run-{repetition}", table, deployment, workload)
                rows = validate_result(path) if path.exists() else []
                value = metric_value(table, rows)
                if value is not None:
                    values.append(value)
            values_by_deployment[deployment] = (
                statistics.mean(values) if len(values) == repetitions else None
            )

        process_value = values_by_deployment["nginx-tinyinit"]
        cells = []
        for deployment, value in values_by_deployment.items():
            if value is None:
                cells.append("failed" if deployment in failed_deployments else "no data")
                continue

            displayed_value = f"{value:.1f} {units[table]}"
            if deployment == "nginx-tinyinit":
                cells.append(displayed_value)
            elif process_value is None or process_value == 0:
                cells.append(f"{displayed_value} (no baseline)")
            else:
                multiple = f"{value / process_value:.2f}"
                cells.append(f"{displayed_value} ({multiple}x)")
        lines.append(f"| `{workload}` | {' | '.join(cells)} |")
    return "\n".join(lines)


def run_matrix(
    output: Path,
    selected: set[str],
    kernel: Path | None,
    dry_run: bool,
) -> dict[CellKey, str]:
    """Run every selected deployment/workload cell and collect failures.

    Args:
        output: Root directory for benchmark results.
        selected: Table names requested for the run.
        kernel: Firecracker guest-kernel path.
        dry_run: Print commands without executing them.
    """
    failures = {}
    # Expand each requested scenario group across every deployment and workload.
    for group, memory in benchmark_groups(selected):
        for deployment in DEPLOYMENTS:
            for workload in WORKLOADS:
                scenario = scenario_path(deployment, group, workload, memory)
                run_dir = output / group / deployment / workload
                binary = workload_path(deployment, workload)
                command = [
                    str(ROOT / "scripts/run_under_venv.sh"),
                    "gos-scenario",
                    str(scenario),
                    "--skip-upload",
                    "-p", f"app_base_dir={ROOT}",
                    "-p", f"app_binary_path={binary}",
                ]
                if deployment == "nginx-tinyinit-firecracker":
                    command += ["-p", f"app_firecracker_kernel_image={kernel}"]
                print(f"==> {group} / {deployment} / {workload}", flush=True)
                if dry_run:
                    print(" ".join(command))
                else:
                    # Remove stale evidence, execute the cell, and validate the
                    # fresh result before advancing through the matrix.
                    run_dir.mkdir(parents=True, exist_ok=True)
                    result = run_dir / "bench-results.json"
                    result.unlink(missing_ok=True)
                    try:
                        subprocess.run(command, cwd=run_dir, check=True)
                        validate_result(result)
                    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                        failures[(group, deployment, workload)] = str(exc)
                        print(
                            f"FAILED: {group} / {deployment} / {workload}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
    return failures


def input_errors(selected: set[str]) -> list[str]:
    """Collect missing scripts, scenarios, and workload executables.

    Args:
        selected: Table names requested for the run.
    """
    errors = set()
    # Memory runs depend on both host-side sampling agents.
    if "memory" in selected:
        for agent_script in MEMORY_AGENT_SCRIPTS:
            if not agent_script.is_file() or not os.access(agent_script, os.X_OK):
                errors.add(f"missing executable memory agent: {agent_script}")
    # Every selected cell needs its scenario and role-specific workload image.
    for group, memory in benchmark_groups(selected):
        for deployment in DEPLOYMENTS:
            for workload in WORKLOADS:
                scenario = scenario_path(deployment, group, workload, memory)
                if not scenario.is_file():
                    errors.add(f"missing scenario: {scenario}")
                binary = ROOT / workload_path(deployment, workload)
                if not binary.is_file() or not os.access(binary, os.X_OK):
                    errors.add(f"missing executable workload: {binary}")
    return sorted(errors)


def main() -> int:
    """Parse the requested run and generate its benchmark tables."""
    parser = argparse.ArgumentParser(description="Run the GraalVisor blog benchmark matrix.")
    parser.add_argument("--table", choices=("all",) + TABLES, default="all")
    parser.add_argument("--kernel", type=Path, default=os.environ.get("FIRECRACKER_KERNEL"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/blog")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--tables-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")

    # Resolve the selected tables and reject missing shared inputs before the
    # multi-hour matrix starts.
    selected = set(TABLES) if args.table == "all" else {args.table}
    kernel = args.kernel.resolve() if args.kernel else None
    if not args.tables_only and (kernel is None or not kernel.is_file()):
        parser.error("pass --kernel VMLINUX or set FIRECRACKER_KERNEL")
    if not args.tables_only:
        errors = input_errors(selected)
        if errors:
            parser.error("benchmark inputs are not ready:\n  - " + "\n  - ".join(errors))

    # Run the selected cells unless existing results are being rendered only.
    output = args.output_dir.resolve()
    failures: dict[RepeatedCellKey, str] = {}
    if not args.tables_only:
        for repetition in range(1, args.repetitions + 1):
            print(f"==> Benchmark run {repetition}/{args.repetitions}", flush=True)
            repeated_failures = run_matrix(
                output / f"run-{repetition}", selected, kernel, args.dry_run
            )
            for cell, error in repeated_failures.items():
                failures[(repetition, *cell)] = error
    if args.dry_run:
        return 0

    # Consolidate the retained results into the requested Markdown tables.
    markdown = "\n\n".join(
        render_table(table, output, args.repetitions, failures)
        for table in TABLES
        if table in selected
    ) + "\n"
    output.mkdir(parents=True, exist_ok=True)
    (output / "tables.md").write_text(markdown)
    print(markdown)
    print(f"Saved tables to {output / 'tables.md'}")
    if failures:
        print(f"{len(failures)} benchmark cell(s) failed:", file=sys.stderr)
        for (repetition, group, deployment, workload), error in failures.items():
            print(
                f"  - run {repetition} / {group} / {deployment} / {workload}: {error}",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
