from __future__ import annotations

import math
import os
import json
import statistics
import subprocess

from collections import defaultdict
from typing import Any, Dict

from gos_harness import gos_logging as logging

DataPoint = Dict[str, Any]


def duplicate_start_ts_results(data_points: list[DataPoint], new_field: str) -> list[DataPoint]:
    """
    The only goal here is to find a way to render data easily in bench server UI.
    We basically want a single field that holds both the start and end time of individual experiments
    such that we can draw a segment between the two timestamps for all apps.

    That way we can chart ops/s over the whole duration of the experiment as a segment by connecting two new dots.

    It's awful. But it works.

    We further set the extra.duplicated to true or false to allow mitigate this issue when counting
      - otherwise we would get twice as many values.

    :param list[dict] data_points:
    :param str new_field:
    :return:
    """
    new_points: list[DataPoint] = []
    for datapoint in data_points:
        if "load-tester.start-ts" in datapoint and "load-tester.end-ts" in datapoint:
            amended_point = datapoint.copy()
            amended_point[new_field] = math.ceil(amended_point["load-tester.start-ts"])
            amended_point["extra.duplicated"] = "false"
            new_points.append(amended_point)
            if amended_point["load-tester.start-ts"] != amended_point["load-tester.end-ts"]:
                cloned_point = datapoint.copy()
                cloned_point[new_field] = math.floor(cloned_point["load-tester.end-ts"])
                cloned_point["extra.duplicated"] = "true"
                new_points.append(cloned_point)
        else:
            new_points.append(datapoint)
    return new_points


def print_summary(datapoints: list[DataPoint]) -> None:
    """Prints the tabular summary of the datapoints with the following columns:

    1. Process: name of the process (app.process-name) to which the metric is accounted for,
    2. Metric Name: metric.name field,
    3. Metric Object: metric.object field,
    4. Min/Mean/Median/Max/Count: statistics of the metric.value for each (metric.name, metric.object, app.process-name).

    ---------------------------------------------------------------------------------------------------------
    Process          Metric Name                Metric Object     Min       Mean     Median    Max      Count
    ---------------------------------------------------------------------------------------------------------
                     memory [MB]                total               311.94   467.81    470.73   550.96     26

    :param datapoints: measured datapoints
    """
    data: defaultdict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for data_point in datapoints:
        data[(f"{data_point['metric.name']} [{data_point['metric.unit']}]", data_point.get('metric.object', ''), data_point.get('app.process-name', ''))].append(data_point['metric.value'])

    def format_number(num: float) -> int | float:
        return int(num) if num == int(num) else round(num, 2)
    headers = ["Process", "Metric Name", "Metric Object", "Min", "Mean", "Median", "Max", "Count"]
    rows: list[tuple[str, str, str, int | float | str, int | float | str, int | float | str, int | float | str, int]] = [
        (
            app_process,
            metric_name,
            metric_object,
            format_number(min(values)),
            format_number(statistics.mean(values)),
            format_number(statistics.median(values)),
            format_number(max(values)),
            len(values)
        ) for (metric_name, metric_object, app_process), values in data.items()
        if all(isinstance(v, (int, float)) for v in values)
    ]
    rows = sorted(rows, key=lambda row: row[0])

    # Add summary row and calculate column widths
    rows.append(("total", "", "", "", "", "", "", sum(len(val) for val in data.values())))
    column_widths = [max(len(str(value)) for value in column) for column in zip(*([headers] + rows))]

    # Print header
    header_row = "  ".join(f"{headers[i]:<{column_widths[i]}}" for i in range(len(headers)))
    logging.raw_log("-" * len(header_row))
    logging.raw_log(header_row)
    logging.raw_log("-" * len(header_row))

    # Print rows
    for row in rows:
        formatted_row = "  ".join(f"{str(row[i]):{'>' if i > 2 else '<'}{column_widths[i]}}" for i in range(len(row)))
        if row[0] == "total":
            logging.raw_log("-" * len(header_row))
        logging.raw_log(formatted_row)
    logging.raw_log("-" * len(header_row))

def print_http_requests_stats(datapoints: list[DataPoint]) -> None:
    """
    Prints a summary of HTTP codes received throughout the scenario.

    :param datapoints: results datapoints
    :return:
    """
    total_counts: defaultdict[Any, float] = defaultdict(float)

    for datapoint in datapoints:
        if datapoint.get('metric.name') == 'request-code-count':
            http_code = datapoint.get('http.code')
            if http_code is not None:
                total_counts[http_code] += datapoint['metric.value']

    if not total_counts:
        return

    total_requests = sum(total_counts.values())
    headers = ["HTTP Code", "Count", "Percentage"]
    rows = [
        (
            str(code),
            count,
            f"{round(count / total_requests * 100, 1)}%"
        ) for code, count in sorted(total_counts.items())
    ]

    rows.append(("Total", total_requests, "100.0%"))

    column_widths = [max(len(str(value)) for value in column) for column in zip(*([headers] + rows))]

    header_row = "  ".join(f"{headers[i]:<{column_widths[i]}}" for i in range(len(headers)))
    logging.raw_log("-" * len(header_row))
    logging.raw_log(header_row)
    logging.raw_log("-" * len(header_row))

    for row in rows:
        formatted_row = "  ".join(f"{str(row[i]):{'>' if i > 0 else '<'}{column_widths[i]}}" for i in range(len(row)))
        if row[0] == "Total":
            logging.raw_log("-" * len(header_row))
        logging.raw_log(formatted_row)
    logging.raw_log("-" * len(header_row))


def save_results_json(datapoints: list[DataPoint]) -> str:
    file_path = os.path.join(os.getcwd(), "bench-results.json")
    with open(file_path, mode="w") as fp:
        json.dump({"queries": datapoints}, fp)
    logging.log(f"Dumped {len(datapoints)} data points")
    logging.log(f"  - saved to: {file_path}")
    return file_path


def bench_server_upload(datapoints: list[DataPoint]) -> None:
    required_env_vars = ['BENCH_UPLOADER_SCRIPT', 'BENCH_ES_URL', 'BENCH_VALIDATION_URL']
    for env_var in required_env_vars:
        if not env_var in os.environ:
            raise ValueError(f"Missing env var '{env_var} for bench server upload !")

    file_path = save_results_json(datapoints)

    cmd = ['python', os.environ['BENCH_UPLOADER_SCRIPT'], '--data-type', 'benchmark', file_path]
    logging.log('> ' + ' '.join(cmd))
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    logging.raw_log(result.stdout.decode('utf-8'))
    if result.returncode != 0:
        logging.log("Upload failed ! Json payload was:")
        logging.log_pprint({"queries": datapoints}, indent=2)
