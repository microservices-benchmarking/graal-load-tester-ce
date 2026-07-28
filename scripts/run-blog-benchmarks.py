#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKLOADS = {
    "http-handler-mn": "http-handler-mn-1.0-SNAPSHOT",
    "micronaut-shopcart-function": "micronaut-shopcart-function",
    "micronaut-similarity-function": "micronaut-similarity-function",
    "quarkus-hello-world-function": "quarkus-hello-world-function",
    "spring-hello-world-function": "spring-hello-world-function",
    "spring-petclinic-function": "spring-petclinic-function",
}
DEPLOYMENTS = {
    "nginx-tinyinit": "Process",
    "nginx-tinyinit-graalhost": "GraalVisor",
    "nginx-tinyinit-gvisor": "gVisor",
    "nginx-tinyinit-firecracker": "Firecracker",
}
TABLES = ("cold-start", "memory", "throughput")


def result_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    rows = payload["queries"] if isinstance(payload, dict) else payload
    unique: dict[str, dict] = {}
    for row in rows:
        unique.setdefault(str(row.get("metric.uuid", id(row))), row)
    return list(unique.values())


def metric_value(table: str, rows: list[dict]) -> float | None:
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
    group = "round-robin" if table == "memory" else "single-app"
    return output / group / deployment / workload / "bench-results.json"


def render_table(table: str, output: Path) -> str:
    titles = {
        "cold-start": "Worst cold start [ms]",
        "memory": "Dominant process max-PSS [MB]",
        "throughput": "Peak throughput [op/s]",
    }
    columns = list(DEPLOYMENTS.values())
    lines = [
        f"## {titles[table]}",
        "",
        f"| Workload | {' | '.join(columns)} |",
        f"| --- | {' | '.join(['---:'] * len(columns))} |",
    ]
    for workload in WORKLOADS:
        cells = []
        for deployment in DEPLOYMENTS:
            value = metric_value(table, result_rows(result_path(output, table, deployment, workload)))
            cells.append("no data" if value is None else f"{value:.1f}")
        lines.append(f"| `{workload}` | {' | '.join(cells)} |")
    return "\n".join(lines)


def run_matrix(output: Path, selected: set[str], kernel: Path | None, dry_run: bool) -> None:
    groups = []
    if selected & {"cold-start", "throughput"}:
        groups.append(("single-app", "2048"))
    if "memory" in selected:
        groups.append(("round-robin", "256"))

    for group, memory in groups:
        for deployment in DEPLOYMENTS:
            for workload, binary in WORKLOADS.items():
                scenario = ROOT / "scenarios/graal-ci" / deployment / group / f"{workload}-{memory}.toml"
                run_dir = output / group / deployment / workload
                command = [
                    str(ROOT / "scripts/run_under_venv.sh"),
                    "gos-scenario",
                    str(scenario),
                    "--skip-upload",
                    "-p", f"app_base_dir={ROOT}",
                    "-p", f"app_binary_path=workloads/{binary}",
                ]
                if deployment == "nginx-tinyinit-firecracker":
                    command += ["-p", f"app_firecracker_kernel_image={kernel}"]
                print(f"==> {group} / {deployment} / {workload}", flush=True)
                if dry_run:
                    print(" ".join(command))
                else:
                    run_dir.mkdir(parents=True, exist_ok=True)
                    subprocess.run(command, cwd=run_dir, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GraalVisor blog benchmark matrix.")
    parser.add_argument("--table", choices=("all",) + TABLES, default="all")
    parser.add_argument("--kernel", type=Path, default=os.environ.get("FIRECRACKER_KERNEL"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/blog")
    parser.add_argument("--tables-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    selected = set(TABLES) if args.table == "all" else {args.table}
    kernel = args.kernel.resolve() if args.kernel else None
    if not args.tables_only and not args.dry_run and (kernel is None or not kernel.is_file()):
        parser.error("pass --kernel VMLINUX or set FIRECRACKER_KERNEL")

    output = args.output_dir.resolve()
    if not args.tables_only:
        run_matrix(output, selected, kernel, args.dry_run)
    if args.dry_run:
        return 0

    markdown = "\n\n".join(render_table(table, output) for table in TABLES if table in selected) + "\n"
    output.mkdir(parents=True, exist_ok=True)
    (output / "tables.md").write_text(markdown)
    print(markdown)
    print(f"Saved tables to {output / 'tables.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
