"""Shared metric schema definitions for scenario tests.

Disclaimer: Majority of the schema was generated using AI
"""

from __future__ import annotations


BASE_METRIC_FIELDS = {
    "metric.better",
    "metric.name",
    "metric.type",
    "metric.unit",
    "metric.uuid",
    "metric.value",
}

SCENARIO_FIELDS = {
    "bench-suite",
    "bench-suite-version",
    "benchmark",
    "benchmarking.start-ts",
    "benchmarking.end-ts",
    "scenario.app-count",
    "scenario.group",
    "scenario.id",
    "scenario.name",
    "scenario.deployment",
    "scenario.status",
    "machine.name",
}

APP_FIELDS = {
    "app.deployment",
    "app.index",
    "app.memory",
    "app.name",
    "app.ocpus",
    "app.type",
}

APP_EXTRA_FIELDS = {
    "app.process-name",
    "app.version",
    "app.artifact-id",
    "app.ocid",
    "app.data-plane-image-id",
}

LOAD_GENERATOR_FIELDS = {
    "load-tester.start-ts",
    "load-tester.end-ts",
    "load-tester.id",
    "load-tester.method-type",
    "load-tester.name"
}

LOAD_TIMELINE_FIELDS = {"extra.load-test-ts", "extra.duplicated"}

HTTP_ALL_FIELDS = {"http.code", "http.request-ts", "http.local-ip", "http.local-port"}
HTTP_CODE_AND_TS_FIELDS = {"http.code", "http.request-ts"}
HTTP_TS_ONLY_FIELD = {"http.request-ts"}

APP_URL_FIELD = {"extra.app-url"}
METRIC_ITERATION_FIELD = {"metric.iteration"}
METRIC_OBJECT_FIELD = {"metric.object"}
METRIC_PERCENTILE_FIELD = {"metric.percentile"}

WRK_FIELDS = {"load-tester.connections", "load-tester.duration", "load-tester.options", "load-tester.threads"}
WRK_WITH_RATE_FIELDS = WRK_FIELDS | {"load-tester.rate"}


def _compose_fields(*groups: set[str]) -> set[str]:
    combined: set[str] = set()
    for group in groups:
        combined |= group
    return combined


METRIC_FIELD_REQUIREMENTS = {
    "request-time": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        APP_FIELDS,
        LOAD_GENERATOR_FIELDS,
        LOAD_TIMELINE_FIELDS,
        METRIC_ITERATION_FIELD,
        HTTP_ALL_FIELDS,
        APP_URL_FIELD,
    ),
    "request-time-breakdown": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        APP_FIELDS,
        LOAD_GENERATOR_FIELDS,
        LOAD_TIMELINE_FIELDS,
        METRIC_ITERATION_FIELD,
        METRIC_OBJECT_FIELD,
        HTTP_TS_ONLY_FIELD,
        APP_URL_FIELD,
    ),
    "latency-distribution": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        LOAD_GENERATOR_FIELDS,
        LOAD_TIMELINE_FIELDS,
        METRIC_ITERATION_FIELD,
        METRIC_PERCENTILE_FIELD,
    ),
    "latency-distribution-breakdown": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        LOAD_GENERATOR_FIELDS,
        LOAD_TIMELINE_FIELDS,
        METRIC_ITERATION_FIELD,
        METRIC_OBJECT_FIELD,
        METRIC_PERCENTILE_FIELD,
    ),
    "load-tester-success": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        APP_FIELDS,
        LOAD_GENERATOR_FIELDS,
        LOAD_TIMELINE_FIELDS
    ),
    "request-code-count": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        APP_FIELDS,
        LOAD_GENERATOR_FIELDS,
        LOAD_TIMELINE_FIELDS
    ),
    "throughput": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        APP_FIELDS,
        LOAD_GENERATOR_FIELDS,
        LOAD_TIMELINE_FIELDS,
        METRIC_ITERATION_FIELD,
        APP_URL_FIELD,
        WRK_FIELDS,
    ),
    "scenario-success": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS
    ),
    "scenario-resource-breakdown": _compose_fields(
        BASE_METRIC_FIELDS,
        SCENARIO_FIELDS,
        METRIC_OBJECT_FIELD,
    ),
}

EXTRA_FIELDS = {
    "extra.agent",
    "extra.event",
    "extra.graalos-service",
    "extra.graalos-endpoint",
    "extra.graalos-virtual-cluster-id",
}

METRIC_SCHEMA_KEYS = _compose_fields(
    BASE_METRIC_FIELDS,
    SCENARIO_FIELDS,
    APP_FIELDS,
    APP_EXTRA_FIELDS,
    LOAD_GENERATOR_FIELDS,
    LOAD_TIMELINE_FIELDS,
    HTTP_ALL_FIELDS,
    APP_URL_FIELD,
    METRIC_ITERATION_FIELD,
    METRIC_OBJECT_FIELD,
    METRIC_PERCENTILE_FIELD,
    WRK_FIELDS,
    WRK_WITH_RATE_FIELDS,
    EXTRA_FIELDS,
)


__all__ = [
    "METRIC_FIELD_REQUIREMENTS",
    "BASE_METRIC_FIELDS",
    "SCENARIO_FIELDS",
    "APP_FIELDS",
    "APP_EXTRA_FIELDS",
    "LOAD_GENERATOR_FIELDS",
    "LOAD_TIMELINE_FIELDS",
    "METRIC_SCHEMA_KEYS",
]
