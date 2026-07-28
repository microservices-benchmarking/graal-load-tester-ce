"""
The module has a list of supported agents: agents are standalone units, scripts or tools that
are notified based on events to emit an observability information.

Each agent should be represented by a command that takes a string event as a parameter and emits
its output to stdout.
"""
from __future__ import annotations

from typing import Any, Collection, Iterator, Optional
from collections import defaultdict

from gos_harness.metrics.metrics import (
    MetricBase,
    MemoryMetric, MemoryBreakdownMetric,
    MaxPssMetric, MaxPssBreakdownMetric,
    PssSampleMetric, PssSampleBreakdownMetric,
    ProcessCountMetric
)
from gos_harness.scenario_parser import AgentConfig, AppConfig, MetricGroupConfig
from gos_harness.scenario_execution_context import ScenarioExecutionContext
from gos_harness import gos_logging as logging

import subprocess
import os
import re

from datetime import datetime

AGENT_DIR: str = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "scripts", "agents"))
MONITORED_COMPONENT_DELIMITER: str = "#,#"
METRIC_DELIMITER: str = "#"
KEY_VALUE_DELIMITER: str = ":"


def parse_compact(results: str) -> list[dict[str, Any]]:
    """Helper function that parses the results in compact form:

    For KEY_VALUE_DELIMITER = KVD, METRIC_DELIMITER = MD, MONITORED_COMPONENT_DELIMITER = MCD:

    key KVD value [MD key KVD value]* [MCD key KVD value [MD key KVD value]*]*

    E.g.

    Pss:1555#Pid:9431#,#Pss:1255#Pid:9351
       ^-- KEY_VALUE_DELIMITER
            ^-- METRIC_DELIMITER
                     ^^^-- MONITORED_COMPONENT_DELIMITER

    :param results: string with the results
    :return: list of parsed dictionaries of form {key: value}
    """
    metric_list = []
    for result in results.split(MONITORED_COMPONENT_DELIMITER):
        parsed_pairs = result.split(METRIC_DELIMITER)
        metrics = {}
        for metric_pair in parsed_pairs:
            if metric_pair.strip():
                metric_key, metric_value = metric_pair.split(KEY_VALUE_DELIMITER)
                metrics[metric_key.strip()] = metric_value.strip()
        metric_list.append(metrics)
    return metric_list

class Agent:
    """
    :ivar idx: index or unique identification of the agent
    :ivar _command: command that executes the notification of the agent
    :ivar _type: type of the agent for identification in the results
    :ivar _attached_to_app: index of the app to which the agent is attached
    """
    def __init__(self, idx: int, command: str, atype: str, app: int, emitted_metrics: Collection[str], metric_groups: list[MetricGroupConfig]):
        self._idx = idx
        self._command = command
        self._type = atype
        self._attached_to_app = app
        self._metric_groups = metric_groups
        # For simplicity we support the following in agents: max, samples, breakdown
        #  - If no metrics were specified, we emit everything
        self._should_emit_max = 'max' in emitted_metrics or not emitted_metrics
        self._should_emit_breakdown = 'breakdown' in emitted_metrics or not emitted_metrics
        self._should_emit_samples = 'samples' in emitted_metrics or not emitted_metrics

    @property
    def idx(self) -> int:
        return self._idx

    @property
    def command(self) -> str:
        return self._command

    @property
    def type(self) -> str:
        return self._type

    @property
    def attached_to_app(self) -> int:
        return self._attached_to_app

    @property
    def metric_groups(self) -> list[MetricGroupConfig]:
        return self._metric_groups

    @property
    def should_emit_breakdown(self) -> bool:
        return self._should_emit_breakdown

    @property
    def should_emit_max(self) -> bool:
        return self._should_emit_max

    @property
    def should_emit_samples(self) -> bool:
        return self._should_emit_samples

    def callback(self, event: str) -> str:
        """
        :param event: event that has happened
        """
        raise NotImplementedError("Each agent has to have a callback")

    def initialize(self) -> None:
        """Initialization of the agent if it is needed"""
        pass

    def __str__(self) -> str:
        return f"Agent (command = {self._command}, type = {self._type}, attached_to_app = {self._attached_to_app})"

    @staticmethod
    def create_agent(agent_config: AgentConfig, apps: list[AppConfig], execution_ctx: ScenarioExecutionContext) -> Agent:
        """Factory method for creating agents out of agent_config"""
        if agent_config.type == "meminfo":
            return MemInfoAgent(agent_config.idx, agent_config.attached_to_app, agent_config.emitted_metrics, agent_config.metric_groups)
        elif agent_config.type == "smaps":
            return ProcSmapsAgent(agent_config.idx, agent_config.attached_to_app, agent_config.emitted_metrics, agent_config.metric_groups, apps[agent_config.attached_to_app], execution_ctx)
        else:
            assert False, f"Unregistered agent: {agent_config.type}"

    def parse_agent_output(self, output: str) -> Iterator[MetricBase]:
        """Parses the stream of results as a (event, metric, value) pairs"""
        raise NotImplementedError("Each agent has to have a callback")

class MemInfoAgent(Agent):
    """Meminfo agent is implemented as a shell script: /scripts/agents/global_agent_meminfo.sh

    The agent mines the information about the memory from /proc/meminfo and outputs it in compact form
    (pairs of keys and values delimited by `:`, and further delimited by `#`)

    The output is for example:
    MemTotal:32587712#SwapTotal:8388608#Committed_AS:11390136#Percpu:11904#Timestamp;2024-09-25-09-59-51

    :ivar _initial_snapshot: initial snapshot of MemInfo agent taken before the deployment of apps
      - includes all keys from /proc/meminfo (such as MemAvailable, Buffers, Cached, etc.);
        - run `cat /proc/meminfo` for full list (this can change for different kernels)
      - does not include timestamp or event.
    """
    def __init__(self, idx: int, app_idx: int, emitted_metrics: Collection[str], metric_groups: list[MetricGroupConfig]):
        self._initial_snapshot: dict[str, int] = {}
        super().__init__(idx, "global_agent_meminfo.sh", "meminfo", app_idx, emitted_metrics, metric_groups)

    def initialize(self) -> None:
        super().initialize()
        result = subprocess.run([self.callback("initial")], capture_output=True, text=True, shell=True)
        if result.returncode != 0:
            raise RuntimeError(f"Error while initializing MemInfoAgent: {result.stderr}")
        metrics = parse_compact(result.stdout)
        assert len(metrics) == 1, f"Invalid number of parsed metrics, got: {len(metrics)}"
        self._initial_snapshot = {
            key: int(metrics[0][key]) for key in metrics[0].keys() if key.lower() not in ("timestamp", "event")
        }

    def callback(self, event: str) -> str:
        """Calls the global_agent_meminfo.sh script out of the agent directory and adds the event key and event value to the call

        The output is prepended with 'meminfo' for easier lookup
        """
        verbose = " -v" if self.should_emit_breakdown else ""
        return f"{os.path.join(AGENT_DIR, self.command)}{verbose} event {event}"

    def parse_agent_output(self, output: str) -> Iterator[MetricBase]:
        """Parses the results of the meminfo from the output.

        Each line is in the following format:

        meminfo: event:pre-robin#MemTotal:32587712#SwapTotal:8388608#Committed_AS:11390136#Percpu:11904#Timestamp;2024-09-25-09-59-51

        The output is broken into individual pairs, with first pair (event: <STR>) being parsed first. Then, we generate
        pairs of (event, metric key, metric value), such as (pre-robin, MemTotal, 32587712).
        """
        iteration = 0
        for line in output.split("\n"):
            if line.startswith('meminfo'):
                meminfo_result = line.split(': ', maxsplit=1)[1]
                # Compute the difference between total memory and memory available and convert from kB to MB
                metrics = parse_compact(meminfo_result)
                assert len(metrics) == 1, f"Incorrect number of metrics parsed (got {len(metrics)}): {meminfo_result}"
                metric = metrics[0]
                assert self._initial_snapshot, "MemInfoAgent was not properly initialized"
                mem_used = (self._initial_snapshot["MemAvailable"] - int(metric["MemAvailable"])) / 1024
                event_ts = int(datetime.strptime(metric["Timestamp"], "%Y-%m-%d-%H-%M-%S").timestamp())
                # metric.name, metric.object, metric.value, metric.unit. metric.better, extra.agent, app.process-name, extra.event, extra.request_ts, metric.iteration
                yield MemoryMetric(agent="meminfo", agent_event=metric["event"], metric_iteration=iteration, metric_value=mem_used)
                if self.should_emit_breakdown:
                    for metric_key in [k for k in metric.keys() if k.lower() not in ("event", "timestamp")]:
                        metric_name = "-".join(k.lower() for k in re.split(r'(?<=[a-z])(?=[A-Z])|_', metric_key))
                        metric_diff = (self._initial_snapshot[metric_key] - int(metric[metric_key])) / 1024
                        # Note: for some breakdown metrics, it could be better to use "higher" (e.g. for Buffers, Cached), for simplicity everything will be lower for now, though
                        yield MemoryBreakdownMetric(agent="meminfo", agent_event=metric["event"], metric_object=metric_name, metric_iteration=iteration, metric_value=metric_diff)
                iteration += 1

    def __str__(self) -> str:
        return f"MemInfoAgent(command = {self._command}, attached_to_app = {self._attached_to_app})"

class ProcSmapsAgent(Agent):
    """ProcSmapsAget is implemented as a shell script: /scripts/agents/pid_agent_smaps.sh

    The agent mines the information about the memory from the /proc/PID/smaps_rollup and ouputs it in compact form
    (pairs of keys and values delimited by `:`, and further delimited by `#` and each data corresponding to a pid
    is delimited by #,#)
    """
    def __init__(self, idx: int, app_idx: int, emitted_metrics: Collection[str], metric_groups: list[MetricGroupConfig], app: AppConfig, execution_ctx: ScenarioExecutionContext):
        self._pid: Optional[int] = None
        self._app = app
        self._execution_ctx = execution_ctx
        super().__init__(idx, "pid_agent_smaps.sh", "smaps", app_idx, emitted_metrics, metric_groups)

    @property
    def pid(self) -> int:
        """On demand obtaining of PID

        Note: this is need as we initialize agents first, and apps are not deployed yet, so we do not
        have the PIDs. hence, this is constructed on demand.
        """
        if self._pid is None:
            self._pid = self._execution_ctx.get_pid(self._app)
        return self._pid

    def callback(self, event: str) -> str:
        """Calls the pid_agent_smaps.sh script out of the agent directory and adds the event key and event value to the call

        The output is prepended with 'smaps' for easier lookup
        """
        return f"{os.path.join(AGENT_DIR, self.command)} {self.pid} event {event}"

    def parse_agent_output(self, output: str) -> Iterator[MetricBase]:
        """Parses the results of the smaps from the output

        smaps: Name:nginx#Pid:328#Rss:2324#Pss:237#Swap:0#PidGroup:328#event:hello#Timestamp:2024-10-09-13-56-42#,#Name:nginx#Pid:329#Rss:5600#Pss:631#Swap:0#PidGroup:328#event:hello#Timestamp:2024-10-09-13-56-42
        """
        iteration = 0
        maxima_per_process: dict[str, dict[str, Any]] = {}
        for line in output.split("\n"):
            if line.startswith(f'smaps{self.idx}: '):
                meminfo_result = line.split(': ', maxsplit=1)[1]
                metrics = parse_compact(meminfo_result)
                # We group metrics per process name:
                #  - We sum numeric values (for different pids)
                #  - We keep the rest
                metrics_per_process: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(int))
                for metric in metrics:
                    if "Name" not in metric:
                        logging.log(f"Warning: 'Name' not found in metric record: {metric}")
                        continue
                    process_name = metric["Name"]
                    for key, value in metric.items():
                        if key.lower() not in {"name", "timestamp", "pid", "event"}:
                            metrics_per_process[process_name][key] += int(value)
                        # We store PIDs in a list
                        elif key.lower() == "pid":
                            if key not in metrics_per_process[process_name]:
                                metrics_per_process[process_name][key] = []
                            metrics_per_process[process_name][key].append(value)
                        else:
                            metrics_per_process[process_name][key] = value
                for process_name, metric in metrics_per_process.items():
                    # Units are in kB, hence, we convert to MB
                    event_ts = int(datetime.strptime(metric["Timestamp"], "%Y-%m-%d-%H-%M-%S").timestamp())
                    pss_value = metric["Pss"] / 1024
                    if process_name not in maxima_per_process or pss_value > maxima_per_process[process_name]["Pss"]:
                        maxima_per_process[process_name] = metric
                    # metric.name, metric.object, metric.value, metric.unit. metric.better, extra.agent, app.process, extra.event, extra.request_ts, metric.iteration
                    if self.should_emit_samples:
                        yield PssSampleMetric(agent="smaps", agent_event=metric["event"], metric_iteration=iteration, metric_value=pss_value).with_app(app_process=process_name)
                        # process-count has no unit and no metric.object
                        yield ProcessCountMetric(agent="smaps", agent_event=metric["event"], metric_iteration=iteration, metric_value=len(metric["Pid"])).with_app(app_process=process_name)
                        if self.should_emit_breakdown:
                            for metric_key in [k for k in metric.keys() if k not in ("Pid", "Name", "Timestamp", "Event", "PidGroup", "event")]:
                                metric_name = "-".join(k.lower().strip() for k in re.split(r'(?<=[a-z])(?=[A-Z])|_', metric_key))
                                yield PssSampleBreakdownMetric(agent="smaps", agent_event=metric["event"], metric_object=metric_name, metric_iteration=iteration, metric_value=metric[metric_key] / 1024).with_app(app_process=process_name)
                iteration += 1
        if self.should_emit_max:
            for process_name, max_metric in maxima_per_process.items():
                # metric.name, metric.object, metric.value, metric.unit. metric.better, extra.agent, app.process, extra.event, extra.request_ts, metric.iteration
                yield MaxPssMetric(agent="smaps", metric_value=max_metric['Pss'] / 1024).with_app(app_process=process_name)
                if self.should_emit_breakdown:
                    for metric_key in [k for k in max_metric.keys() if k not in ("Pid", "Name", "Timestamp", "Event", "PidGroup", "event")]:
                        metric_name = "-".join(k.lower().strip() for k in re.split(r'(?<=[a-z])(?=[A-Z])|_', metric_key))
                        metric_value= max_metric[metric_key] / 1024
                        yield MaxPssBreakdownMetric(agent="smaps", metric_object=metric_name, metric_value=metric_value).with_app(app_process=process_name)


    def __str__(self) -> str:
        return f"ProcSmapsAgent (command = {self._command}, attached_to_app = {self.attached_to_app})"
