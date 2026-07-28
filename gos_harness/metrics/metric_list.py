"""Metric list object."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Any, Optional, TYPE_CHECKING

from gos_harness.metrics.metrics import MetricBase
from gos_harness.scenario_parser import (
    MetricGroupConfig,
    MetricGroupFilterConfig,
    MetricGroupFilterByFieldsConfig,
    MetricGroupFilterByQuantileRanges,
    MetricGroupSamplerConfig,
    MetricGroupDiscreteSampler,
)
from gos_harness.scenario_execution_context import ScenarioExecutionContext

if TYPE_CHECKING:
    from gos_harness.load_generator import LoadGenerator

class MetricList:
    """Represents list of collected metrics, possibly associated to concrete load tester

    :param metrics: list of metrics
    :param load_generator: associated generator with the list of metrics
    """
    def __init__(self, load_generator: Optional[LoadGenerator] = None) -> None:
        self._metrics: list[MetricBase] = []
        self._load_generator: Optional[LoadGenerator] = load_generator

    @property
    def metrics(self) -> list[MetricBase]:
        return self._metrics

    @property
    def load_generator(self) -> Optional[LoadGenerator]:
        return self._load_generator

    @property
    def load_generator_type(self) -> Optional[str]:
        if self._load_generator and self._load_generator.configuration:
            return self._load_generator.configuration.method.type
        else:
            return None

    def filter(self, **kwargs: dict[str, Any]) -> MetricList:
        """Creates a filtered copy of the metric list

        Example of usage:
        filter(**{"metric.name": "request-time"})
        filter(**{"scenario.deployment": "service"})

        :param kwargs: dictionary of simple predicates that tests if metric[key] == value
        """
        filtered_metric_list = MetricList(self.load_generator)
        for metric in self._metrics:
            if all(metric.get(key) == value for (key, value) in kwargs.items()):
                filtered_metric_list.add_metric(metric, metric.metric_groups)
        return filtered_metric_list

    def iterate(self, **filters: Any) -> Iterable[MetricBase]:
        """Iterates over the metrics applying filters

        :param filters: dictionary of simple predicates that tests if metric[key] == value
        """
        for metric in self._metrics:
            if not filters or all(metric.get(key) == value for (key, value) in filters.items()):
                yield metric

    def add_metric(self, metric: MetricBase, metric_groups: list[MetricGroupConfig]) -> MetricList:
        metric.with_metric_groups(metric_groups)
        self._metrics.append(metric)
        return self

    def join(self, *metric_lists: MetricList) -> MetricList:
        """Joins metrics from the provided MetricLists into this list.

        :param metric_lists: MetricLists to append into this list.
        """
        for metric_list in metric_lists:
            if metric_list:
                if metric_list.load_generator is not None and self._load_generator is None:
                    self._load_generator = metric_list.load_generator
                self._metrics.extend(metric_list.metrics)
        return self

    @classmethod
    def join_all(cls, metric_lists: Iterable[MetricList], load_generator: Optional[LoadGenerator] = None) -> MetricList:
        """Joins metrics from multiple MetricLists into a new MetricList.

        :param metric_lists: MetricLists to join.
        :param load_generator: optional load generator to set on the result.
        """
        result = cls(load_generator=load_generator)
        return result.join(*metric_lists)

    def apply_metric_groups(self, all_metric_groups: Optional[Iterable[MetricGroupConfig]] = None) -> MetricList:
        """Applies metric group collect, filters, and samplers to the metric list.

        The resulting MetricList includes the union of all metric group outputs, with
        any duplicates removed (by metric.uuid).
        """
        # If there are no metrics, we return empty metric list
        if not self._metrics:
            return MetricList(self.load_generator)

        # If no metric groups were provided for checking, we collect metric groups from the metrics
        enabled_groups = all_metric_groups or {group for metric in self._metrics for group in metric.metric_groups if group.enabled}

        # If there were no metric groups enabled, we return empty Metric List
        if not enabled_groups:
            return MetricList(self.load_generator)

        # We group the metrics to MetricConfigGroup -> list[MetricBase]
        metrics_by_group = self._collect_metrics_by_group()
        selected_metric_ids: set[Any] = set()
        missing_groups: list[MetricGroupConfig] = []

        # For each group, we apply the postprocessing steps
        #  - each metric's ID is marked: at the end, we will collect all of the metrics only once
        for group in enabled_groups:
            metrics = metrics_by_group.get(self._metric_group_identifier(group), [])
            processed_metrics = self._apply_postprocess(metrics, group.postprocess)
            if not processed_metrics:
                if group.fail_on_no_metrics:
                    missing_groups.append(group)
                continue
            for metric in processed_metrics:
                selected_metric_ids.add(self._metric_identifier(metric))

        if missing_groups:
            formatted_groups = ", ".join( f"'{group.group or '<unnamed>'}' (collect={group.collect})" for group in missing_groups)
            raise RuntimeError("Metric group filtering produced no metrics for enabled group(s): " f"{formatted_groups}. " "Set `fail_on_no_metrics = false` to ignore empty groups.")

        # Create the final list of metrics
        return self._filter_by_metric_ids(selected_metric_ids)

    def _collect_metrics_by_group(self) -> dict[tuple[Any, ...], list[MetricBase]]:
        """Groups the metric to metric groups

        Each metric can be part of multiple groups

        :return: dictionary mapping metric groups to list of metrics
        """
        grouped: dict[tuple[Any, ...], list[MetricBase]] = defaultdict(list)
        for metric in self._metrics:
            metric_name = metric.get("metric.name")
            for group in metric.metric_groups:
                if group.enabled and metric_name in group.collect:
                    grouped[self._metric_group_identifier(group)].append(metric)
        return grouped

    def _metric_group_identifier(self, group: MetricGroupConfig) -> tuple[Any, ...]:
        return (
            tuple(group.collect),
            group.group,
            group.enabled,
            group.fail_on_no_metrics,
            self._postprocess_identifier(group.postprocess),
        )

    def _postprocess_identifier(
        self,
        postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig],
    ) -> tuple[Any, ...] | None:
        if postprocess is None:
            return None
        if isinstance(postprocess, MetricGroupFilterByFieldsConfig):
            fields = tuple(
                sorted(
                    (
                        key,
                        tuple(value) if isinstance(value, list) else value,
                    )
                    for key, value in postprocess.fields.items()
                )
            )
            return ("fields", fields, self._postprocess_identifier(postprocess.next_postprocess))
        if isinstance(postprocess, MetricGroupFilterByQuantileRanges):
            groupby = tuple(postprocess.groupby) if postprocess.groupby else None
            return (
                "quantile-range",
                groupby,
                postprocess.ranges,
                self._postprocess_identifier(postprocess.next_postprocess),
            )
        if isinstance(postprocess, MetricGroupDiscreteSampler):
            groupby = tuple(postprocess.groupby) if postprocess.groupby else None
            return (
                "discrete",
                postprocess.sampleby,
                groupby,
                postprocess.rate,
                self._postprocess_identifier(postprocess.next_postprocess),
            )
        raise ValueError(f"Unsupported metric postprocess type: {type(postprocess)}")

    def _apply_postprocess(
        self,
        metrics: list[MetricBase],
        postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig],
    ) -> list[MetricBase]:
        """Runs the postprocessing step on the list of metrics.

        If there is other postprocess in the chain, it is run next

        :param metrics: list of metrics that are postprocessed
        :param postprocess: filter or sampler or nothing
        :return: list of filtered metrics
        """
        # Nothing to be run, we return
        if postprocess is None:
            return metrics

        if isinstance(postprocess, MetricGroupFilterByFieldsConfig):
            metrics = self._apply_filter_by_fields(metrics, postprocess)
        elif isinstance(postprocess, MetricGroupFilterByQuantileRanges):
            metrics = self._apply_filter_by_quantile_ranges(metrics, postprocess)
        elif isinstance(postprocess, MetricGroupDiscreteSampler):
            metrics = self._apply_discrete_sampler(metrics, postprocess)
        else:
            raise ValueError(f"Unsupported metric postprocess type: {type(postprocess)}")

        # Call the next postprocessing step in the chain
        return self._apply_postprocess(metrics, postprocess.next_postprocess)

    def _apply_filter_by_fields(
        self,
        metrics: list[MetricBase],
        filter_config: MetricGroupFilterByFieldsConfig,
    ) -> list[MetricBase]:
        """Filters the list of metrics based on values of the fields

        Example of the filters:
          - filter the metrics that have set `metric.object` to one of ('pss', 'rss')
          - filter the metrics that have `metric.object` set to `connect-time` and `metric.iteration` to one of (0, 1, 2)

        :param metrics: list of metrics to be filtered by fields
        :param filter_config: configuration through which we filter
        """
        return [
            metric
            for metric in metrics
            if all(self._metric_field_matches(metric.get(key), value) for key, value in filter_config.fields.items())
        ]

    def _metric_field_matches(self, metric_value: Any, filter_value: Any) -> bool:
        """Helper function fo checking that metric_value matches the filter_value

        :param metric_value: checked value(s)
        :param filter_value: value(s) accepted by the filter
        :return: true if the filter matches the metric(s)
        """
        # Nothing to check
        if metric_value is None:
            return False
        filter_values = filter_value if isinstance(filter_value, list) else [filter_value]
        # Check if any of the value in the list matches the filter
        if isinstance(metric_value, list):
            return any(value in filter_values for value in metric_value)
        # Check that the value is allowed in the filter
        return metric_value in filter_values

    def _apply_filter_by_quantile_ranges(
        self,
        metrics: list[MetricBase],
        filter_config: MetricGroupFilterByQuantileRanges,
    ) -> list[MetricBase]:
        """Filters the list of metrics based on quantile ranges represented as comma-delimited intervals

        Example of the filters:
          - 0,50,100 takes the minimum, median and maximum
          - 0-5,50,95-100, takes the bottom 5% of values (up to fifth percentile), median and top 5% of values (from 95th percentile up)

        :param metrics: list of metrics to be filtered by fields
        :param filter_config: configuration through which we filter
        """
        # Helper function that first groups the keys to buckets based on list of keys
        grouped_metrics = self._group_by_keys(metrics, filter_config.groupby)
        selected: list[MetricBase] = []
        for bucket in grouped_metrics.values():
            if bucket:
                ordered = sorted(bucket, key=self._metric_value_as_float)
                selected.extend(self._select_by_quantile_ranges(ordered, filter_config.ranges))
        return selected

    def _apply_discrete_sampler(
        self,
        metrics: list[MetricBase],
        sampler_config: MetricGroupDiscreteSampler,
    ) -> list[MetricBase]:
        """Samples the list of metrics in discrete domain

        :param metrics: list of metrics to be filtered by fields
        :param sampler_config: configuration through which we sample
        """
        grouped_metrics = self._group_by_keys(metrics, sampler_config.groupby)
        selected: list[MetricBase] = []
        rate = max(sampler_config.rate, 1)
        for bucket in grouped_metrics.values():
            # If we specified sampleby, we sort in that dimension and then apply sampling
            sampleby = sampler_config.sampleby
            if sampleby:
                bucket = sorted(bucket, key=lambda metric: (metric.get(sampleby) is None, metric.get(sampleby)))
            # Otherwise, we simply sample in the order the metrics were added
            selected.extend(bucket[::rate])
        return selected

    def _group_by_keys(
        self,
        metrics: list[MetricBase],
        groupby: Optional[list[str]],
    ) -> dict[tuple[Any, ...], list[MetricBase]]:
        """Groups the list of metrics based on tuple of keys.

        E.g. if we group by metric.name and metric.object, we have |metric.name| * |metric.object| groups

        For each group, we have a list of metrics.

        :param metrics: list of metrics
        :param groupby: list of keys by which we group
        """
        if not groupby:
            return {(): list(metrics)}
        grouped: dict[tuple[Any, ...], list[MetricBase]] = defaultdict(list)
        for metric in metrics:
            key = tuple(metric.get(field) for field in groupby)
            grouped[key].append(metric)
        return grouped

    def _select_by_quantile_ranges(self, ordered: list[MetricBase], ranges: str) -> list[MetricBase]:
        """Selects the quantile ranges from the list of metrics

        :param orderer: order list of metrics
        :param ranges: comma delimited percentile ranges
        """
        # Nothing to select
        if not ordered:
            return []

        parsed_ranges = self._parse_quantile_ranges(ranges)
        last_index = len(ordered) - 1
        selected_indices: set[int] = set()
        for low, high in parsed_ranges:
            if low == high:
                selected_indices.add(self._quantile_index(last_index, low, rounding="nearest"))
                continue
            start_idx = self._quantile_index(last_index, low, rounding="floor")
            end_idx = self._quantile_index(last_index, high, rounding="ceil")
            for idx in range(start_idx, end_idx + 1):
                selected_indices.add(idx)
        return [ordered[idx] for idx in sorted(selected_indices)]

    def _parse_quantile_ranges(self, ranges: str) -> list[tuple[int, int]]:
        """Parses the string into list of ranges

        Examples of comma-delimited ranges:
          - 50 -> just median
          - 0-10 -> up to tenth-percentile
          - 0-10,20-30,90-100 -> up to tenth percentile, between twelfth and thirtieth percentile and from ninetieth percentile up

        :param ranges: comma-separated list of quantile ranges
        """
        parsed = []
        for token in ranges.split(","):
            token = token.strip()
            if "-" in token:
                low, high = token.split("-", 1)
                parsed.append((int(low), int(high)))
            else:
                value = int(token)
                parsed.append((value, value))
        return parsed

    def _quantile_index(self, last_index: int, percentile: int, rounding: str) -> int:
        """Gets the index and rounds it according to different strategies

        :param last_index: last index in the list
        :param percentile: percentile rank
        :param rounding: rounding strategy
        """
        if last_index <= 0:
            return 0
        position = last_index * percentile / 100
        if rounding == "floor":
            idx = math.floor(position)
        elif rounding == "ceil":
            idx = math.ceil(position)
        else:
            idx = int(position + 0.5)
        return max(0, min(last_index, int(idx)))

    def _metric_value_as_float(self, metric: MetricBase) -> float:
        """Tries to convert the metric value to float"""
        value = metric.get("metric.value")
        if isinstance(value, (int, float)):
            return float(value)
        if value is None:
            raise ValueError(
                f"Quantile filtering requires numeric metric.value, got {value} for metric '{metric.get('metric.name')}'"
            )
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Quantile filtering requires numeric metric.value, got {value} for metric '{metric.get('metric.name')}'"
            ) from exc

    def _metric_identifier(self, metric: MetricBase) -> Any:
        """Gets the unique identifier of the metric"""
        return metric.get("metric.uuid", id(metric))

    def _filter_by_metric_ids(self, selected_metric_ids: set[Any]) -> MetricList:
        """Filters the metrics to contain only those selected from the previous filtering

        :param selected_metric_ids: set of selected metrics
        """
        filtered_metric_list = MetricList(self.load_generator)
        for metric in self._metrics:
            if self._metric_identifier(metric) in selected_metric_ids:
                filtered_metric_list._metrics.append(metric)
        return filtered_metric_list

    def serialize_to_dict(self, execution_ctx: ScenarioExecutionContext) -> list[dict[str, Any]]:
        """Serializes the metrics to list of dictionaries

        This is the final filter, that converts the metrics to dictionary and includes the scenario fields

        :param execution_ctx:
        :return: list of dictionaries
        """
        return [dict(metric.with_scenario(execution_ctx)) for metric in self.metrics]

    def __str__(self) -> str:
        metrics_repr = ", ".join(str(metric) for metric in self._metrics)
        return f"{self.__class__.__name__}(load_generator_type={self.load_generator_type}, metrics=[{metrics_repr}])"

    def __repr__(self) -> str:
        return str(self)
