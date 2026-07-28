"""Metric stubs backing scenario metric definitions."""

from __future__ import annotations

import os
import platform
import uuid

from dataclasses import dataclass, field
from gos_harness.gos_oci import BaseApp, DeployedGraalApp, KegBinaryApp
from gos_harness.graalos_marketplace import GraalOSStackApp
from gos_harness.scenario_execution_context import ScenarioExecutionContext
from typing import Any, Iterator, Optional, TYPE_CHECKING, ClassVar, MutableMapping

if TYPE_CHECKING:
    from gos_harness.load_generator import LoadGenerator
    from gos_harness.scenario_parser import MetricGroupConfig




def list_supported_metric_names() -> list[str]:
    """Returns all registered metric.name values."""
    return sorted(MetricBase._metric_name_registry)


def get_machine_name(deployment: str) -> str:
    """Returns the machine name

    For dataplane image, we return tag identifying the shape (e4, e3, s3) with number of CPUs and memory,
    otherwise we try to obtain the machine name from the environment. If everything fails, we fallback to platform name.

    :param deployment: deployment of the applications
    :return machine.name for the bench points
    """
    # Otherwise, we try to obtain machine name from environment
    machine_name = os.environ.get('MACHINE_NAME')
    if machine_name:
        return machine_name

    # Fallback to the name of the node
    return platform.node()


# --- Base class for Metric ---
@dataclass
class MetricBase(MutableMapping[str, Any]):
    """Base metric object."""
    METRIC_NAME: ClassVar[Optional[str]] = None
    _metric_name_registry: ClassVar[set[str]] = set()

    _fields: dict[str, Any] = field(default_factory=dict)
    _metric_groups: list["MetricGroupConfig"] = field(default_factory=list, init=False, repr=False)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        metric_name = getattr(cls, "METRIC_NAME", None)
        if metric_name:
            MetricBase._metric_name_registry.add(metric_name)

    def __post_init__(self) -> None:
        self._fields.setdefault("metric.uuid", str(uuid.uuid4()))

    def __getitem__(self, key: str) -> Any:
        return self._fields[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._fields[key] = value
        if key == "metric.name" and isinstance(value, str):
            MetricBase._metric_name_registry.add(value)

    def __delitem__(self, key: str) -> None:
        del self._fields[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._fields)

    def __len__(self) -> int:
        return len(self._fields)

    @property
    def metric_groups(self) -> list["MetricGroupConfig"]:
        return self._metric_groups

    def with_metric_groups(self, metric_groups: list["MetricGroupConfig"]) -> MetricBase:
        self._metric_groups = list(metric_groups)
        return self

    def with_scenario(self, execution_ctx: ScenarioExecutionContext) -> MetricBase:
        """Appends scenario related fields to the metric

        :param execution_ctx: execution context of the scenario
        """
        self["benchmarking.start-ts"] = execution_ctx.scenario_start_ts
        self["benchmarking.end-ts"] = execution_ctx.scenario_end_ts
        self["bench-suite"] = execution_ctx.suite_name
        if execution_ctx.suite_version:
            self["bench-suite-version"] = execution_ctx.suite_version
        self["benchmark"] = execution_ctx.scenario_config.name
        self["scenario.name"] = execution_ctx.scenario_config.name
        self["scenario.id"] = execution_ctx.scenario_config.id
        self["scenario.group"] = execution_ctx.scenario_config.group
        self["scenario.deployment"] = execution_ctx.scenario_config.deployment
        self["scenario.status"] = execution_ctx.scenario_status
        self["scenario.app-count"] = len(execution_ctx.scenario_config.apps)
        self["machine.name"] = get_machine_name(execution_ctx.scenario_config.deployment)
        return self

    def with_app(self, *, app: Optional[BaseApp] = None, app_process: Optional[str] = None, app_endpoint: Optional[str] = None) -> MetricBase:
        """Appends application specific fields to the app

        :param app: application
        :param app_process: concrete process that is running
        """
        if app:
            self.update(app.metric_fields())

        if app_process:
            self['app.process-name'] = app_process
        if app_endpoint:
            self['extra.app-url'] = app_endpoint
        return self

    def with_load_generator(
        self,
        *,
        load_generator: Optional[LoadGenerator] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> MetricBase:
        """Appends load generator related fields to the metric

        :param load_generator: load generator
        :param start_ts: start of the load generation
        :param end_ts: end of the load_generation
        """
        # For legacy purposes, we keep the original names of the data model to use the `load-tester*`` prefix
        #   - this was deliberately not changed to `load-generator`, as this would break the queries
        if load_generator is not None:
            self["load-tester.id"] = load_generator.id
            self["load-tester.name"] = load_generator.name
            self["load-tester.method-type"] = load_generator.configuration.method.type
            # Add Load Generator Extras
            self.with_extras(**load_generator.extras())
        if start_ts is not None:
            self["load-tester.start-ts"] = start_ts
        if end_ts is not None:
            self["load-tester.end-ts"] = end_ts
        return self

    def with_request(
        self,
        *,
        request_ts: float,
        http_code: Optional[int] = None,
        local_ip: Optional[str] = None,
        local_port: Optional[int] = None,
    ) -> MetricBase:
        """Appends HTTP related fields to the metric

        :param request_ts: timestamp, when the request happened
        :param http_code: HTTP code of the request
        :param local_ip: IP of the client from which the request was sent
        :param local_port: port of the client from which the request was sent
        """
        if request_ts is not None:
            self['http.request-ts'] = request_ts
        if http_code is not None:
            self['http.code'] = http_code
        if local_ip is not None:
            self['http.local-ip'] = local_ip
        if local_port is not None:
            self['http.local-port'] = local_port
        return self

    def with_extras(self, **kwargs: Any) -> MetricBase:
        """Appends extra arguments to the metric

        :param kwargs: key, value extra arguments
        """
        for key, value in kwargs.items():
            self[f"extra.{key}"] = value
        return self

    def with_wrk(self, *, wrk_config: dict[str, Any]) -> MetricBase:
        """Appends the wrk config fields to the metric

        :param wrk_config: configuration of the wrk
        """
        self._move_key('load-tester.options', wrk_config, "options")
        self._move_key('load-tester.rate', wrk_config, "rate")
        self._move_key('load-tester.threads', wrk_config, "threads")
        self._move_key('load-tester.connections', wrk_config, "connections")
        self._move_key('load-tester.duration', wrk_config, "duration")
        return self

    def _move_key(self, dstkey: str, src: dict[str, Any], srckey: str) -> None:
        """Moves the src[srckey] to self[dstkey], if srckey is in the src

        :param dstkey: destination key
        :param src: source dictionary
        :param srckey: source key
        """
        if srckey in src:
            self[dstkey] = src[srckey]


@dataclass
class CreationTimeMetric(MetricBase):
    """How long it takes for an app to be created, i.e. registered on the service; time between following timestamps:
      - time_finished: request for creation was finished;
      - time_accepted: request for creation was accepted as reported by the oci request payload.
    
    Part of the [[apps.metrics]] groups.
      - should be created with app fields.
    """
    METRIC_NAME = "creation-time"

    def __init__(self, *, metric_value: float) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "creation-time"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "s"


@dataclass
class StrictCreationTimeMetric(MetricBase):
    """How long it takes for an app to be created, i.e. registered on the service; time between following timestamps:
      - time_finished: request for creation was finished;
      - time_started: request for creation was started to be processed as seen by the user.
    
    Part of the [[apps.metrics]] groups.
      - should be created with app fields.
    """
    METRIC_NAME = "strict-creation-time"

    def __init__(self, *, metric_value: float) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "strict-creation-time"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "s"


@dataclass
class ScenarioSuccessMetric(MetricBase):
    """Was the load testing scenario successful or not?
    
    Part of the [[scenario.metrics]] groups.
      - always generated, unless explicitly overwritten with:

      [[scenario.metrics]]
      collect = []
    """
    METRIC_NAME = "scenario-success"

    def __init__(self, *, metric_value: int | float) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "higher"
        self["metric.name"] = "scenario-success"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = ""


@dataclass
class ScenarioResourceBreakdown(MetricBase):
    """Number of scenario resources grouped by resource kind.

    Part of the [[scenario.metrics]] groups.
    """
    METRIC_NAME = "scenario-resource-breakdown"

    def __init__(self, *, metric_value: int | float, metric_object: str) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "higher"
        self["metric.name"] = "scenario-resource-breakdown"
        self["metric.value"] = metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = ""
        self["metric.object"] = metric_object


@dataclass
class RequestTimeMetric(MetricBase):
    """How long it takes for request to be processed (from sending the request to receiving the response)
    
    Part of the [[generators.metrics]] groups.
      - should be created with app fields, request fields and load generator fields.
    """
    METRIC_NAME = "request-time"

    def __init__(self, *, metric_value: float, metric_iteration: int) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "request-time"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "ms"
        self["metric.iteration"] = metric_iteration


@dataclass
class RequestTimeBreakdownMetric(MetricBase):
    """Breakdown of how long it takes for request to be processed (from sending the request to receiving the response)
    
    Part of the [[generators.metrics]] groups.
      - should be created with app fields, request fields and load generator fields.
    """
    METRIC_NAME = "request-time-breakdown"

    def __init__(self, *, metric_value: float, metric_object: str, metric_iteration: int) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "request-time-breakdown"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "ms"
        self["metric.object"] = metric_object
        self["metric.iteration"] = metric_iteration


@dataclass
class LatencyDistributionMetric(MetricBase):
    """Distribution of the latencies of sending requests and receiving response
    
    Part of the [[generators.metrics]] groups:
      - should be created with load generator fields;
      - optionally should be created with wrk fields and app fields (when load generating using wrk/wrk2).
    """
    METRIC_NAME = "latency-distribution"

    def __init__(self, *, metric_value: float, metric_percentile: float, metric_iteration: int) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "latency-distribution"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "ms"
        self["metric.percentile"] = metric_percentile
        self["metric.iteration"] = metric_iteration


@dataclass
class LatencyDistributionBreakdownMetric(MetricBase):
    """Breakdown of distribution of the latencies of sending requests and receiving response

    Note there is a difference between latency distribution breakdown and latency breakdown distribution:
      - latency distribution breakdown means, we do latency distribution and then break down the percentile requests;
      - latency breakdown distribution means, we break down the requests to individual components and then compute the percentiles.
    
    Part of the [[generators.metrics]] groups:
      - should be created with load generator fields;
      - optionally should be created with wrk fields and app fields (when load generating using wrk/wrk2).
    """
    METRIC_NAME = "latency-distribution-breakdown"

    def __init__(self, *, metric_object: str, metric_value: float, metric_percentile: float, metric_iteration: int) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "latency-distribution-breakdown"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "ms"
        self["metric.object"] = metric_object
        self["metric.percentile"] = metric_percentile
        self["metric.iteration"] = metric_iteration


@dataclass
class LoadTesterSuccessMetric(MetricBase):
    """Was the load generator successful or not?
    
    Part of the [[generators.metrics]] groups:
      - should be created with load generator fields;
      - optionally should be created with wrk fields and app fields (when load generating using wrk/wrk2).
    """
    METRIC_NAME = "load-tester-success"

    def __init__(self, *, metric_value: float) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "higher"
        self["metric.name"] = "load-tester-success"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = ""


@dataclass
class RequestCodeCountMetric(MetricBase):
    """What is the distribution of the HTTP codes in the load generator?
    
    Part of the [[generators.metrics]] groups:
      - should be created with load generator fields;
      - optionally should be created with wrk fields and app fields (when load generating using wrk/wrk2).
    """
    METRIC_NAME = "request-code-count"

    def __init__(self, *, http_code: int, metric_value: float) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "higher"
        self["metric.name"] = "request-code-count"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "#"
        self["http.code"] = http_code


@dataclass
class ThroughputMetric(MetricBase):
    """Throughput of the app: i.e. how many requests could be processed in time.
    
    Part of the [[generators.metrics]] groups:
      - should be created with load generator fields;
      - optionally should be created with wrk fields and app fields (when load generating using wrk/wrk2).
    """
    METRIC_NAME = "throughput"

    def __init__(self, *, metric_value: float, metric_iteration: int) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "higher"
        self["metric.name"] = "throughput"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "op/s"
        self["metric.iteration"] = metric_iteration


@dataclass
class MemoryMetric(MetricBase):
    """How much memory has system consumed during the load generation?
    
    Part of the [[agent.metrics]] groups:
      - should be created with app fields.
    """
    METRIC_NAME = "memory"

    def __init__(self, *, agent: str, agent_event: str, metric_iteration: int, metric_value: float) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "memory"
        self["metric.object"] = "total"
        self["metric.iteration"] = metric_iteration
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "MB"
        self["extra.agent"] = agent
        self["extra.event"] = agent_event


@dataclass
class MemoryBreakdownMetric(MetricBase):
    """What is the breakdown of how much memory has system consumed during the load generation?
    
    Part of the [[agent.metrics]] groups:
      - should be created with app fields.
    """
    METRIC_NAME = "memory-breakdown"

    def __init__(self, *, agent: str, agent_event: str, metric_object: str, metric_iteration: int, metric_value: float) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "memory-breakdown"
        self["metric.object"] = metric_object
        self["metric.iteration"] = metric_iteration
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "MB"
        self["extra.agent"] = agent
        self["extra.event"] = agent_event


@dataclass
class PssSampleMetric(MetricBase):
    """How much memory has individual app has consumed during the experiment?
    
    Part of the [[agent.metrics]] groups:
      - should be created with app fields.
    """
    METRIC_NAME = "pss-sample"

    def __init__(self, *, agent: str, agent_event: str, metric_iteration: int, metric_value: str) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "pss-sample"
        self["metric.object"] = "pss"
        self["metric.iteration"] = metric_iteration
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "MB"
        self["extra.agent"] = agent
        self["extra.event"] = agent_event


@dataclass
class PssSampleBreakdownMetric(MetricBase):
    """What is the breakdown of how much memory has individual app has consumed during the experiment?
    
    Part of the [[agent.metrics]] groups:
      - should be created with app fields.
    """
    METRIC_NAME = "pss-sample-breakdown"

    def __init__(self, *, agent: str, agent_event: str, metric_object: str, metric_iteration: int, metric_value: str) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "pss-sample-breakdown"
        self["metric.object"] = metric_object
        self["metric.iteration"] = metric_iteration
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "MB"
        self["extra.agent"] = agent
        self["extra.event"] = agent_event


@dataclass
class MaxPssMetric(MetricBase):
    """How much memory in maximum has individual app has consumed during the experiment?
    
    Part of the [[agent.metrics]] groups:
      - should be created with app fields.
    """
    METRIC_NAME = "max-pss"

    def __init__(self, *, agent: str, metric_value: str) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "max-pss"
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "MB"
        self["extra.agent"] = agent


@dataclass
class MaxPssBreakdownMetric(MetricBase):
    """What is the breakdown of how much memory in maximum has individual app has consumed during the experiment?
    
    Part of the [[agent.metrics]] groups:
      - should be created with app fields.
    """
    METRIC_NAME = "max-pss-breakdown"

    def __init__(self, *, agent: str, metric_object: str, metric_value: str) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "max-pss-breakdown"
        self["metric.object"] = metric_object
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = "MB"
        self["extra.agent"] = agent


@dataclass
class ProcessCountMetric(MetricBase):
    """How many processes were spawned during the given point during the experiment?
    
    Part of the [[agent.metrics]] groups:
      - should be created with app fields.
    """
    METRIC_NAME = "process-count"

    def __init__(self, *, agent: str, agent_event: str, metric_iteration: int, metric_value: int) -> None:
        """Metric fields to be populated elsewhere."""
        super().__init__()
        self["metric.better"] = "lower"
        self["metric.name"] = "process-count"
        self["metric.iteration"] = metric_iteration
        self["metric.value"] =  metric_value
        self["metric.type"] = "numeric"
        self["metric.unit"] = ""
        self["extra.agent"] = agent
        self["extra.event"] = agent_event
