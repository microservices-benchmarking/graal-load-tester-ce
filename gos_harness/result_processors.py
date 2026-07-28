"""
This module contains postprocessors for the results
"""
from __future__ import annotations

from gos_harness.metrics.metric_list import MetricList
from gos_harness import gos_logging as logging

def remove_cold_start_outliers(raw_results: list[MetricList], worst_percent_to_remove: float) -> list[MetricList]:
    """
    Remove cold start outliers by dropping the worst load generators according to
    their cold start request time (request-time with metric.iteration == 0).

    Prerequisite: each processed result is expected to be a MetricList produced by
    a request load generator and to contain at least one request-time metric with
    metric.iteration == 0.
    """

    def cold_start_time(result: MetricList) -> int | float:
        for request_metric in result.iterate(**{"metric.name": "request-time"}):
            if request_metric.get("metric.iteration") == 0:
                metric_value = request_metric.get("metric.value")
                if isinstance(metric_value, (int, float)):
                    return metric_value
                raise RuntimeError(
                    "Cold start time metric (request-time with metric.iteration == 0) has invalid value "
                    f"{metric_value!r} in result {result}"
                )

        raise RuntimeError(
            "Cold start time metric (request-time with metric.iteration == 0) not found "
            f"in result {result}"
        )

    assert worst_percent_to_remove >= 0 and worst_percent_to_remove < 1, "invalid percent = " + str(worst_percent_to_remove)
    if not raw_results:
        return raw_results

    outliers_to_drop = int(len(raw_results) * worst_percent_to_remove)
    if outliers_to_drop == 0:
        return raw_results

    # Higher request-time is worse, so we drop from the descending order.
    sorted_by_cold_start_desc = sorted(raw_results, key=cold_start_time, reverse=True)
    outliers = sorted_by_cold_start_desc[:outliers_to_drop]
    selected = sorted_by_cold_start_desc[outliers_to_drop:]

    for dropped in outliers:
        logging.log(f"Dropping outlier load-generator '{dropped.load_generator}'")

    return selected
