from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import io
import os
import pathlib
import re
import sys
import tempfile
import time
import toml

from enum import Enum
from datetime import datetime
from contextlib import redirect_stdout, redirect_stderr
from typing import Optional, Callable, Any, TypedDict, Literal, TextIO
from dataclasses import dataclass, field

from gos_harness import async_cmd_runner, scenario_parser
from gos_harness.config import dataplane_config, graalos_config
from gos_harness.load_generator import LoadGenerator, RemoteLoadGenerator 
from gos_harness import gos_oci, setup_check, bench_server, local_runner, manage_apps, gos_logging as logging
from gos_harness.agents import Agent
from gos_harness.gos_oci import DeployedGraalApp, RunningApp, LocalBinaryApp, KegBinaryApp, BaseApp
from gos_harness.metrics.metrics import CreationTimeMetric, StrictCreationTimeMetric, ScenarioResourceBreakdown, ScenarioSuccessMetric
from gos_harness.metrics.metrics import list_supported_metric_names
from gos_harness.metrics.metric_list import MetricList
from gos_harness.utils.module_loader import LazyLoader
from gos_harness.graalos_marketplace import GraalOSStack, GraalOSStackApp
from gos_harness.scenario_parser import ScenarioConfig, RunningBinaryConfig, LocalBinaryConfig, RunningKegBinaryConfig, AppConfig, NetworkType, GeneratorConfig, GraalOSStackBinaryConfig, LOCAL_APP_DEPLOYMENT_TYPES
from gos_harness.scenario_execution_context import (
    SCENARIO_STATUS_FAILED_EXCEPTION,
    SCENARIO_STATUS_FAILED_POSTPROCESSING,
    SCENARIO_STATUS_FAILED_SUCCESS_RATE,
    SCENARIO_STATUS_FAILED_TIMEOUT,
    SCENARIO_STATUS_SUCCESS,
    ScenarioExecutionContext,
)
from gos_harness.result_processors import remove_cold_start_outliers

# Lazy Loaded packages used for creating the visualization of the scenario
#  - lazy loaded packages are loaded only when they are used
scenario_visualizer = LazyLoader("scenario_visualizer", globals(), "gos_harness.visualization.scenario_visualizer")
report_visualizer = LazyLoader("report_visualizer", globals(), "gos_harness.visualization.report_visualizer")

class UserProvidedCodeError(Exception):
    pass


class _MetricGroupOwners(TypedDict):
    """Type hint for the per-owner buckets in metric-group aggregation."""
    scenario: list[str]
    apps: list[str]
    generators: list[str]
    agents: list[str]


class _DeduplicatedMetricGroup(TypedDict):
    """Type hint for a deduplicated metric-group aggregation entry."""
    group: scenario_parser.MetricGroupConfig
    owners: _MetricGroupOwners

class LoadGeneration(Enum):
    """Enumeration for the different ways how to perform the load generation:

    1. Local: the load generation is performed from the same instance as the Graal Load Tester harness;
      - requires wrk, wrk2 and curl executables.

    2. Remote: the load generation is performed from freshly provisioned compute instances;
      - graal-load-tester, wrk, wrk2 and curl is migrated to the instance;
      - the load generation is executed as remote procedure.

    """
    LOCAL = "local"
    REMOTE = "remote"

@dataclass
class ScenarioResources:
    """Resources created and used during the benchmarking and the experiment

    - GraalOS Marketplace Stack Resources
    :ivar graalos_marketplace_stack: created disposable or running GraalOS stack
    :ivar deployed_stack_apps: stack app handles used for deployment and cleanup
    :ivar successful_stack_apps: apps successfully deployed to the stack
    :ivar failed_stack_apps: apps that failed to deploy to the stack

    - GraalOS Service Resources
    :ivar failed_graalos_apps: list of apps that were failed to created

    - GraalOS Dataplane Resources
    :ivar deployed_kegs: list of kegs deployed to dataplane

    - General
    :ivar graal_apps: apps created and used during the scenario
    :ivar agents: list of agents
    :ivar load_generators: list of load generators
    :ivar nginx_log_file: log file, where output of nginx will be saved
    :ivar tinyinit_log_file: log file, where output of tinyinit will be saved
    """
    graalos_marketplace_stack: Optional[GraalOSStack] = None
    deployed_stack_apps: list[GraalOSStackApp] = field(default_factory=list)
    successful_stack_apps: list[GraalOSStackApp] = field(default_factory=list)
    failed_stack_apps: set[str] = field(default_factory=set)
    graalos_apps: list[DeployedGraalApp] = field(default_factory=list)
    failed_graalos_apps: set[str] = field(default_factory=set)
    deployed_kegs: list[KegBinaryApp] = field(default_factory=list)
    local_apps: list[BaseApp] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    load_generators: list[LoadGenerator] = field(default_factory=list)
    nginx_log_file: Optional[TextIO] = None
    tinyinit_log_file: Optional[TextIO] = None

    @property
    def deployed_stack_apps_count(self) -> int:
        return len(self.deployed_stack_apps)

    @property
    def successful_stack_apps_count(self) -> int:
        return len(self.successful_stack_apps)

    @property
    def deployed_kegs_count(self) -> int:
        return len(self.deployed_kegs)

    @property
    def apps(self) -> list[BaseApp]:
        """Returns all apps: local, graalos service apps, deployed kegs, deployed stack apps"""
        apps: list[BaseApp] = list(self.local_apps)
        apps.extend(self.graalos_apps)
        apps.extend(self.deployed_kegs)
        apps.extend(self.successful_stack_apps)
        return apps

graalos = graalos_config.get_config()
dataplane = dataplane_config.get_config()
version: Optional[str] = None


def describe_deployment(scenario: ScenarioConfig, cli_args: argparse.Namespace) -> None:
    """Prints the information about the machine based on deployment"""
    logging.header("Running on following specification")
    logging.log(f"Running Graal Load Tester version '{get_version()}'")
    logging.log(f"  - Load generators: {cli_args.load_generators.value}")
    if any(app.deployment == 'graalos-stack' for app in scenario.apps):
        graalos_stack = scenario.graalos.stack
        if graalos_stack:
            logging.log(f" - Graalos Stack: {graalos_stack.lifecycle}")
            logging.log(f"   - IP: {graalos_stack.ip}")
            logging.log(f"   - Specification : {graalos_stack.specification}")
            logging.log(f"   - Authentication Method: {graalos.auth}")
    if any(app.deployment == 'service' for app in scenario.apps):
        logging.log("Running 'service' deployment:")
        logging.log(f" - Cluster: {graalos.cluster}")
        logging.log(f" - Region: {graalos.region}")
        logging.log(f" - Availability Domain: {graalos.availability_domain}")
        logging.log(f" - Compartment Id: {graalos.compartment_id}")
        logging.log(f" - Subnet Id: {graalos.subnet_id}")
        logging.log(f" - Fault Domain: {graalos.fault_domain}")
        logging.log(f" - Artifact Repository: {graalos.artifact_repository}")
        logging.log(f" - Authentication Method: {graalos.auth}")
        logging.log(f" - Log Group Id: {graalos.log_group_id}")
        logging.log(f" - OCI calls: {'remote' if graalos.remote_oci else 'local'}")
    if any(app.deployment == 'dataplane' for app in scenario.apps):
        logging.log("Running 'dataplane' deployment:")
        logging.log(f" - Proxy: {dataplane.proxy if dataplane else 'none'}")
        logging.log(f" - Cluster Version: {dataplane.cluster_version}")
        logging.log(f" - Instance IP: {dataplane.instance_ip}")
        logging.log(f" - Image ID: {dataplane.image_id}")
        logging.log(f" - Machine Name: {dataplane.machine_name}")
    if any(app.deployment == 'nginx-tinyinit' for app in scenario.apps):
        logging.log(f"Running 'nginx-tinyinit' deployment")
    if any(app.deployment == 'nginx-tinyinit-graalhost' for app in scenario.apps):
        logging.log(f"Running 'nginx-tinyinit-graalhost' deployment")
    if any(app.deployment == 'nginx-tinyinit-firecracker' for app in scenario.apps):
        logging.log(f"Running 'nginx-tinyinit-firecracker' deployment")


def get_version() -> str:
    """Dynamically gets the version of the Graal Load Tester from the pyproject.toml file."""
    global version
    if version is None:
        pyproject_file_path = pathlib.Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_file_path, mode="rt") as pyproject:
            version_from_pyproject = toml.loads(pyproject.read())["project"]["version"]
            version = f"v{version_from_pyproject}"
    assert version is not None
    return version


def log_metric_groups_summary(scenario_config: ScenarioConfig) -> None:
    """Logs enabled metric groups deduplicated across scenario/apps/generators/agents.

    The output is in the following format:

        ---------------------------------------------------------------------------------------------------------------------
        ===                                    Summary of the Metrics and Metric Groups                                   ===
        ---------------------------------------------------------------------------------------------------------------------
        2026-03-27 12:54:23.168 (+00.001s) Metrics collected in the scenario (7 metrics): latency-distribution, latency-distribution-breakdown, load-tester-success, request-code-count, request-time, request-time-breakdown, scenario-success
        2026-03-27 12:54:23.168 (+00.000s) Metrics groups enabled in the scenario:
        2026-03-27 12:54:23.168 (+00.000s)  [1] group='default' | collect=['scenario-success'] | fail_on_no_metrics=True | owners: scenario
        2026-03-27 12:54:23.168 (+00.000s)  [2] group='<unnamed>' | collect=['request-time', 'request-time-breakdown', 'latency-distribution', 'latency-distribution-breakdown', 'load-tester-success', 'request-code-count'] | fail_on_no_metrics=True | owners: generators=[0 (type=requests)]
    
    :param scenario_config: configuration of the scenario.
    """
    logging.header("Summary of the Metrics and Metric Groups")

    deduplicated_groups: dict[tuple[Any, ...], _DeduplicatedMetricGroup] = {}

    def format_metric_group(group: scenario_parser.MetricGroupConfig) -> str:
        """Formats a metric group for log output."""
        postprocess = f" | postprocess={group.postprocess}" if group.postprocess else ""
        return (f"group='{group.group or '<unnamed>'}' | collect={group.collect} | fail_on_no_metrics={group.fail_on_no_metrics}{postprocess}")

    def register_group(group: scenario_parser.MetricGroupConfig, owner_type: Literal["scenario", "apps", "generators", "agents"], owner_name: str) -> None:
        if not group.enabled:
            return
        key = (group.group, tuple(group.collect), group.enabled, group.fail_on_no_metrics, str(group.postprocess) if group.postprocess else None)
        if key not in deduplicated_groups:
            deduplicated_groups[key] = {"group": group, "owners": {"scenario": [], "apps": [], "generators": [], "agents": []}}
        deduplicated_groups[key]["owners"][owner_type].append(owner_name)

    for group in scenario_config.metric_groups:
        register_group(group, "scenario", "scenario")

    for app in scenario_config.apps:
        app_owner = f"{app.app_name} (deployment={app.deployment})"
        for group in app.metric_groups:
            register_group(group, "apps", app_owner)

    for generator in scenario_config.generators:
        generator_name = generator.name if generator.name else generator.unique_name
        generator_owner = f"{generator_name} (type={generator.method.type})"
        for group in generator.metric_groups:
            register_group(group, "generators", generator_owner)

    for agent in scenario_config.agents:
        agent_owner = f"agent-{agent.idx} (name={agent.type})"
        for group in agent.metric_groups:
            register_group(group, "agents", agent_owner)

    if not deduplicated_groups:
        logging.log("No enabled metric groups configured.")
        return

    metric_names = sorted({metric_name for item in deduplicated_groups.values() for metric_name in item["group"].collect})
    logging.log(f"Metrics collected in the scenario ({len(metric_names)} metrics): {', '.join(metric_names)}")
    logging.log(f"Metrics groups enabled in the scenario:")
    for idx, item in enumerate(deduplicated_groups.values(), start=1):
        group = item["group"]
        owners = item["owners"]
        owner_parts = []
        if owners["scenario"]:
            owner_parts.append("scenario")
        if owners["apps"]:
            owner_parts.append(f"apps=[{', '.join(sorted(set(owners['apps'])))}]")
        if owners["generators"]:
            owner_parts.append(f"generators=[{', '.join(sorted(set(owners['generators'])))}]")
        if owners["agents"]:
            owner_parts.append(f"agents=[{', '.join(sorted(set(owners['agents'])))}]")

        logging.log(f" [{idx}] {format_metric_group(group)} | owners: {'; '.join(owner_parts)}")

def create_load_generator(load_generation: LoadGeneration, load_generator_config: GeneratorConfig, graal_apps: list[BaseApp], agents: list[Agent]) -> RemoteLoadGenerator | LoadGenerator:
    """Based on the type of the load generation constructs the generator

    :param load_generation: type of the load generation (remote or local)
    :param load_generator_config: parsed configuration of the load generator
    :param graal_apps: list of apps to which the load generator sends the requests
    :param agent: list of attached agents
    """
    if load_generation == LoadGeneration.LOCAL:
        return LoadGenerator(apps=graal_apps, configuration=load_generator_config, agents=agents)
    elif load_generation == LoadGeneration.REMOTE:
        return RemoteLoadGenerator(apps=graal_apps, configuration=load_generator_config, agents=agents)
    else:
        assert False, f"Unsupported load generation type: {load_generation.value}"


def mark_scenario_as_failed(
    execution_ctx: ScenarioExecutionContext,
    message: str,
    exception: BaseException,
    log_non_debug: bool = True,
    status: str = SCENARIO_STATUS_FAILED_EXCEPTION,
) -> None:
    """Logs a scenario failure and records the failed scenario status.

    :param execution_ctx: scenario execution context to mark as failed
    :param message: failure message to log before exception details
    :param exception: exception that caused the failure
    :param log_non_debug: whether to log a concise exception message below DEBUG level
    :param status: structured scenario status to record
    """
    logging.log(message)
    logging.log_exception(exception, log_non_debug=log_non_debug)
    execution_ctx.set_scenario_status(status)


def finalize_scenario_results(
    scenario_config: ScenarioConfig,
    execution_ctx: ScenarioExecutionContext,
    resources: ScenarioResources,
    benchmarking_results: list[MetricList],
    orchestration_succeeded: bool,
) -> list[dict[str, Any]]:
    """Creates scenario-level results and applies final metric filtering.

    :param scenario_config: parsed scenario configuration with metric groups
    :param execution_ctx: scenario execution context used for serialization
    :param resources: scenario resources tracked during orchestration
    :param benchmarking_results: raw metric lists produced by orchestration
    :param orchestration_succeeded: whether orchestration completed without failure
    :return: serialized and amended datapoints ready for upload or local output
    """
    logging.log("Checking success of the overall scenario by checking success of all load generators...")
    success_count = 0
    total_count = 0
    for metric_list in benchmarking_results:
        for metric in metric_list.iterate(**{"metric.name": "load-tester-success"}):
            if bool(metric["metric.value"]):
                success_count += 1
            total_count += 1
    scenario_success = orchestration_succeeded and (success_count == total_count)
    if orchestration_succeeded:
        execution_ctx.set_scenario_status(
            SCENARIO_STATUS_SUCCESS if scenario_success else SCENARIO_STATUS_FAILED_SUCCESS_RATE
        )
    scenario_result = MetricList()
    scenario_result.add_metric(ScenarioSuccessMetric(metric_value=int(scenario_success)), scenario_config.metric_groups)
    resource_counts = {
        "stack-app-count": resources.successful_stack_apps_count,
        "failed-stack-app-count": len(resources.failed_stack_apps),
        "local-app-count": len(resources.local_apps),
        "dataplane-keg-count": resources.deployed_kegs_count,
        "service-app-count": len(resources.graalos_apps),
        "failed-service-app-count": len(resources.failed_graalos_apps),
        "compute-instance-count": gos_oci.num_disposable_instances(),
    }
    for metric_object, metric_value in resource_counts.items():
        scenario_result.add_metric(
            ScenarioResourceBreakdown(metric_object=metric_object, metric_value=metric_value),
            scenario_config.metric_groups,
        )
    logging.log(f"{success_count}/{total_count} load generators performed requests successfully {':-D' if scenario_success else ':,('}")

    logging.log("Applying metric group filtering and sampling")
    joined_results = MetricList.join_all(benchmarking_results + [scenario_result])
    metric_groups = (
        scenario_config.all_metric_groups(enabled_only=True)
        if orchestration_succeeded
        else scenario_config.metric_groups
    )
    filtered_results = joined_results.apply_metric_groups(all_metric_groups=metric_groups)
    data_points = filtered_results.serialize_to_dict(execution_ctx)
    logging.log(" - finished applying metric group filtering.")

    new_field = "extra.load-test-ts"
    logging.log(f"Amending data points to populate '{new_field}'...")
    modified_data_points = bench_server.duplicate_start_ts_results(data_points, new_field)
    logging.log(" - finished amending the data points.")

    logging.log(f"Printing the summary of the results")
    bench_server.print_summary(modified_data_points)
    logging.log(f"Printing individual HTTP requests statistics")
    bench_server.print_http_requests_stats(modified_data_points)

    return modified_data_points


async def _orchestrate_scenario(
    scenario_config: ScenarioConfig,
    execution_ctx: ScenarioExecutionContext,
    resources: ScenarioResources,
    timeout: Optional[int]
) -> list[MetricList]:
    """Orchestrates the scenario wrt given execution context

    This corresponds to creation of the resources (without the final cleanup) and running the load generation

    :param scenario_config: parsed scenario
    :param execution_ctx: context of the execution of the scenario; contains flags, options, etc.
    :param resources: resource created during the benchmarking (this includes apps, kegs, etc.)
    :param timeout: scenario timeout: the timeout will be tested at safe points; meaning that it is not fully enforced.
    """
    orchestration_start = time.time()

    def raise_if_timeout_exceeded() -> None:
        """Checkpoint for checking if the scenario has exceeded the timeout"""
        if timeout is None:
            return
        elapsed = time.time() - orchestration_start
        if elapsed >= timeout:
            raise TimeoutError(f"Scenario orchestration exceeded timeout of {timeout} seconds (elapsed {elapsed:.3f}s)")

    if timeout:
        logging.log(f"Scenario will gracefully timeout if it exceeds {timeout} seconds")
        logging.log(f" - the timeout is checked at safe points that assures no resource leakage.")
        logging.log(f" - the actual scenario runtime might be higher than {timeout} seconds.")

    # Create marketplace stack before we deploy the applications
    # [Resources Ownership] - disposable stack is created identified by the OCID
    #  - in case the timeout or some external cancellation happens, there can be some loss.
    if scenario_config.graalos.stack is not None:
        logging.log(" - Provisioning GraalOS Stack")
        resources.graalos_marketplace_stack = await GraalOSStack.create_from_config(scenario_config.graalos.stack)
    raise_if_timeout_exceeded()

    # Running the setup phase, if it is specified
    #  - this is used to start additional services or to generate runtime properties
    for group, setup in AppConfig.setup_scripts.items():
        logging.log(f" - Running setup script for '{group}' group: {setup}")
        res, _, _ = await async_cmd_runner.run(['bash', setup], timeout=360)
        if res.returncode != 0:
            raise RuntimeError(f"{setup} for group {group} has failed")
    raise_if_timeout_exceeded()

    # Initializing agents
    if scenario_config.agents:
        logging.log(f"Initializing {len(resources.agents)} agents")
        agent_start = time.time()
        for agent_config in scenario_config.agents:
            agent = Agent.create_agent(agent_config, scenario_config.apps, execution_ctx)
            agent.initialize()
            resources.agents.append(agent)
            logging.log(f" - Initialized agent {agent.idx} ({agent.type})")
        logging.log(f"Agent creation successfully completed in {time.time() - agent_start:.3f} sec")
    raise_if_timeout_exceeded()

    # Deploying the apps
    max_async_graal_app_creations = scenario_config.harness.apps_creation_concurrency
    apps_to_deploy_on_service = scenario_config.graalos_service_apps
    apps_to_deploy_on_service_count = len(apps_to_deploy_on_service)
    non_service_apps = scenario_config.non_service_apps
    non_service_apps_count = len(non_service_apps)
    logging.header(f"Creating {non_service_apps_count + apps_to_deploy_on_service_count} Graal apps")
    deploying_start = time.time()
    if len(non_service_apps) > 0:
        logging.log(f"Creating {len(non_service_apps)} Graal apps locally or in OCI instances")
        for app_config in non_service_apps:
            app: BaseApp
            if isinstance(app_config.binary, RunningBinaryConfig):
                app = RunningApp(app_config)
                resources.local_apps.append(app)
            elif isinstance(app_config.binary, RunningKegBinaryConfig):
                # Note: nothing is run here so no need for async
                local_runner.run_local_keg_on_dataplane(app_config, execution_ctx)
                app = KegBinaryApp(app_config)
                resources.deployed_kegs.append(app)
            elif isinstance(app_config.binary, GraalOSStackBinaryConfig):
                if resources.graalos_marketplace_stack is None:
                    raise ValueError("Cannot create GraalOS Stack apps without specification of the GraalOS stack")
                app = GraalOSStackApp(app_config, resources.graalos_marketplace_stack)
                resources.deployed_stack_apps.append(app)
            else:
                assert isinstance(app_config.binary, LocalBinaryConfig), f"Cannot handle the creation of app from {app_config.binary.__class__}"
                # Note: this is not run async, because, we spawn processes that are long running and their cleanup is assured by atexit
                local_runner.run_local_binary_on_nginx_tinyinit(app_config, execution_ctx, resources.nginx_log_file, resources.tinyinit_log_file)
                app = LocalBinaryApp(app_config)
                resources.local_apps.append(app)
            logging.log(f"Created app: {app}")
            for endpoint in app_config.endpoints:
                logging.log(f" - Endpoint: {app.endpoint_url(endpoint)}")
    raise_if_timeout_exceeded()

    # We deploy the apps to the marketplace stack
    # [Resources Ownership] GraalOS Marketplace Stack Apps are created; the list is kept in the resources
    #  - in case of cancellation, the list should contain the ID of the successfully created resources.
    if resources.deployed_stack_apps_count > 0:
        logging.log(f"Deploying {resources.deployed_stack_apps_count} apps to the stack")
        batch_result = await async_cmd_runner.batch_run(
            batch_items=resources.deployed_stack_apps,
            batch_fun=lambda app: app.deploy(
                max_creation_retries=execution_ctx.scenario_config.harness.apps_creation_retries
            ),
            batch_step=max_async_graal_app_creations,
            abort_early=True,
            timeout_handler=raise_if_timeout_exceeded,
            progress_label="Deploying stack apps"
        )
        for i, res in enumerate(batch_result.all_results):
            app = resources.deployed_stack_apps[i]
            if isinstance(res, BaseException):
                resources.failed_stack_apps.add(app.unique_name)
            else:
                resources.successful_stack_apps.append(app)
        batch_result.assert_run_was_successful(f"Couldn't deploy {resources.deployed_stack_apps_count} stack apps: ", raise_on_assertion_failure=True)
    raise_if_timeout_exceeded()

    # We wait for kegs being deployed: for 60s unless the harness.keg_deployment_timeout is set
    # [Resources Ownership] GraalOS Dataplane Kegs are safely stored in the resources;
    #  - in case of cancellation, the list should contain the IDs of the kegs for the cleanup.
    if resources.deployed_kegs_count > 0:
        logging.log(f"Waiting for {resources.deployed_kegs_count} to be deployed")
        await local_runner.wait_for_kegs(resources.deployed_kegs, waiting_cond = lambda keg_list: len(keg_list) == resources.deployed_kegs_count, timeout = scenario_config.harness.keg_deployment_timeout)
        await local_runner.collect_pids_for_kegs(execution_ctx)
    raise_if_timeout_exceeded()

    # [Resources Ownership] GraalOS Service Apps are kept in the resources from start;
    #   - we opt-out of the list of failed apps, in case there is cancellation or error;
    #   - no resource is hence lost.
    if apps_to_deploy_on_service_count > 0:
        logging.log(f"Creating {apps_to_deploy_on_service_count} Graal apps on service:")
        # Pre-register all graal-application to ensure cleanup can find these apps even if creation is interrupted
        for app_cfg in apps_to_deploy_on_service:
            resources.failed_graalos_apps.add(f"{scenario_config.name}_{app_cfg.unique_name}")
        batch_result = await async_cmd_runner.batch_run(
            batch_items=apps_to_deploy_on_service,
            batch_fun=lambda app_config: gos_oci.deploy_graal_app(
                f"{execution_ctx.scenario_config.name}_{app_config.unique_name}",
                app_config,
                creation_timeout=execution_ctx.scenario_config.harness.apps_creation_timeout,
                max_creation_retries=execution_ctx.scenario_config.harness.apps_creation_retries,
                oci_cli_raw=execution_ctx.scenario_config.harness.app_creation_oci_cli_raw
            ),
            batch_step=max_async_graal_app_creations,
            abort_early=True,
            timeout_handler=raise_if_timeout_exceeded,
            progress_label="Creating service apps"
        )
        # This is a workaround, as sometimes the apps are in CREATING state during failure and then they would be kept ACTIVE
        #  - during cleanup, we will lookup these applications and try to kill them.
        for (i, res) in enumerate(batch_result.all_results):
            name = f"{scenario_config.name}_{apps_to_deploy_on_service[i].unique_name}"
            if isinstance(res, BaseException):
                # Since, the creation ended with exception we leave its name in resources.failed_graalos_apps so cleanup can find and delete it
                pass
            else:
                assert isinstance(res, DeployedGraalApp)
                resources.failed_graalos_apps.discard(name)
                resources.graalos_apps.append(res)
        batch_result.assert_run_was_successful(f"{len(batch_result.failures)} apps couldn't be created: ", raise_on_assertion_failure=True)

        for graal_app in resources.graalos_apps:
            await graal_app.resolve_ip()
            logging.log(f"App's ({graal_app.ocid}) IP resolved as {graal_app.ip})")

        if execution_ctx.log_service_apps:
            await create_logs_for_graal_apps(resources.graalos_apps, max_async_graal_app_creations, timeout_handler=raise_if_timeout_exceeded)
    raise_if_timeout_exceeded()

    logging.log(f"{non_service_apps_count + apps_to_deploy_on_service_count} Graal apps successfully created in {time.time() - deploying_start:.3f} sec")

    max_async_load_generators_init = scenario_config.harness.instances_creation_concurrency
    benchmarking_results = []

    logging.header(f"Initializing {len(scenario_config.generators)} load generators")
    load_generator_init_start = time.time()
    resources.load_generators = [create_load_generator(execution_ctx.load_generation, generator_config, resources.apps, resources.agents) for generator_config in scenario_config.generators]
    load_generator_init_result = await async_cmd_runner.batch_run(batch_items=resources.load_generators, batch_fun=lambda lt: lt.initialize(), batch_step=max_async_load_generators_init, timeout_handler=raise_if_timeout_exceeded)
    load_generator_init_result.assert_run_was_successful("Load generators couldn't be initialized: ", raise_on_assertion_failure=True)
    logging.log(f"Load generators all initialized in {time.time() - load_generator_init_start:.3f}!")
    raise_if_timeout_exceeded()

    logging.header(f"Running {len(resources.load_generators)} load generators")
    load_generation_start = time.time()
    # here we must really trigger all coroutines concurrently because load generator tasks are time-sensitive
    remaining = None if timeout is None else timeout - (time.time() - orchestration_start)

    load_generator_tasks = [asyncio.create_task(lt.run()) for lt in resources.load_generators]
    gather = asyncio.gather(*load_generator_tasks, return_exceptions=True)

    try:
        load_generator_results = await (asyncio.wait_for(gather, remaining) if remaining is not None else gather)
    except asyncio.TimeoutError as exc:
        logging.log("[!!!] Load generation timed out; cancelling load generators")
        for task in load_generator_tasks:
            task.cancel()
        await asyncio.gather(*load_generator_tasks, return_exceptions=True)
        raise TimeoutError(f"Load generation exceeded timeout of {timeout} seconds (elapsed {time.time() - orchestration_start:.3f}s)") from exc
    logging.log(f"Load generators all completed their tasks in {time.time() - load_generation_start:.3f}!")

    failed_runs = [res for res in load_generator_results if isinstance(res, BaseException)]
    benchmarking_results = [res for res in load_generator_results if not isinstance(res, BaseException)]
    if len(failed_runs) > 0:
        logging.log_exceptions(failed_runs)
        raise RuntimeError(f"{len(failed_runs)}/{len(load_generator_results)} load generators failed !")

    logging.header(f"Crunching and uploading performance numbers")
    for graal_app in resources.graalos_apps:
        timing_result = MetricList()
        timing_result.add_metric(CreationTimeMetric(metric_value=graal_app.creation_time).with_app(app=graal_app).with_load_generator(start_ts=graal_app.time_accepted.timestamp(), end_ts=graal_app.time_finished.timestamp()), graal_app.app_config.metric_groups)
        timing_result.add_metric(StrictCreationTimeMetric(metric_value=graal_app.strict_creation_time).with_app(app=graal_app).with_load_generator(start_ts=graal_app.time_accepted.timestamp(), end_ts=graal_app.time_finished.timestamp()), graal_app.app_config.metric_groups)
        benchmarking_results.append(timing_result)

    for results_processing_step in scenario_config.results_processings:
        logging.log(f"Running user-defined processing code '{results_processing_step.name}'...")
        # - pass a copy to user code such that the original copy is intact when exception happens
        # - filter `None` so that user-code can be simplified
        locals: dict[str, Any] = {"metric_list": benchmarking_results[::], "remove_cold_start_outliers": remove_cold_start_outliers}
        logger = io.StringIO()
        try:
            with redirect_stdout(logger):
                with redirect_stderr(logger):
                    exec(results_processing_step.code, {}, locals)
                    metric_list = locals["metric_list"]
                    if not isinstance(metric_list, list):
                        raise TypeError("User-defined processing must leave 'metric_list' as a list.")
                    benchmarking_results = metric_list
        except Exception as e:
            logging.log(f"Error running user-defined processing code {results_processing_step.name}:" + str(e))

        # print output from user code
        out = logger.getvalue()
        logging.log("Printing results of the user defined code:")
        if out:
            logging.raw_log("".join(f"[{results_processing_step.name}] {line}" for line in out.splitlines(True)))

    # Filter out empty results (if there are any)
    benchmarking_results = list(filter(lambda res: isinstance(res, MetricList), benchmarking_results))

    return benchmarking_results


async def run_scenario(
    scenario_config: ScenarioConfig,
    load_generation: LoadGeneration = LoadGeneration.LOCAL,
    upload_results: bool = True,
    general_log_file: Optional[str] = None,
    log_service_apps: bool = False,
    configure_graalos: bool = True,
) -> tuple[int, list[dict[str, Any]]]:
    service_load_testing_enabled = any(app.deployment == "service" for app in scenario_config.apps)
    if service_load_testing_enabled:
        # main() configures GraalOS before calling this function, but tests and
        # other direct run_scenario() callers still rely on this default setup.
        if scenario_config.graalos.service is None:
            logging.die("Missing '[graalos.service]' configuration needed for service load testing")
        if configure_graalos:
            graalos.set_scenario_config(scenario_config.graalos)

    # Create ScenarioExecutionContext and populate it with scenario run parameters
    #  - execution context contains options and additional information of the scenario related to runtime
    execution_ctx = ScenarioExecutionContext(
        scenario_id=scenario_config.id,
        scenario_config=scenario_config,
        load_generation=load_generation,
        upload_results=upload_results,
        log_service_apps=log_service_apps,
        suite_name="graalos",
        suite_version=get_version(),
        scenario_start_ts=time.time(),
    )

    for results_processing_step in scenario_config.results_processings:
        logging.log(f"Parsing user-defined processing code {results_processing_step.name}...")
        try:
            ast.parse(results_processing_step.code)
        except SyntaxError as e:
            raise UserProvidedCodeError(f"[error] Syntax error in the code: {str(e.text)}")

    # Check that the private apps fit into the limit for number of private apps per attachment
    private_apps_count = len([app for app in scenario_config.apps if app.binary.artifact_id is not None and app.binary.network_mode == NetworkType.PRIVATE])
    private_apps_limit = scenario_config.graalos.private_apps_per_attachment_limit
    if private_apps_count > 0 and private_apps_limit is None:
        raise RuntimeError("Missing 'graalos.private_apps_per_attachment_limit' configuration needed for private-networking service applications")
    if private_apps_limit is not None and private_apps_count > private_apps_limit:
        raise RuntimeError(f"Max {private_apps_limit} private-networking applications are allowed per single network attachment: got {private_apps_count}")

    nginx_log_file = None
    if general_log_file is not None and scenario_config.harness.redirect_nginx_logs:
        nginx_log_file_name = f"{general_log_file}.nginx.log"
        nginx_log_file = open(nginx_log_file_name, "w")
        logging.log(f"Dumping all nginx logs into '{nginx_log_file_name}' file.")
    tinyinit_log_file = None
    if general_log_file is not None and scenario_config.harness.redirect_tinyinit_logs:
        tinyinit_log_file_name = f"{general_log_file}.tinyinit.log"
        tinyinit_log_file = open(tinyinit_log_file_name, "w")
        logging.log(f"Dumping all tinyinit logs into '{tinyinit_log_file_name}' file.")

    # Setting the new autoscaling policy before the actual benchmarking takes place
    cluster_metadata = {}
    original_autoscaling: Optional[dict[Any, Any]] = None
    if scenario_config.harness.autoscaling_policy != None:
        autoscaling_policy = scenario_config.harness.autoscaling_policy
        assert autoscaling_policy is not None
        boat_profile = scenario_config.harness.boat_profile
        instance_configuration_shape = scenario_config.harness.instance_configuration_shape
        assert boat_profile is not None
        assert instance_configuration_shape is not None
        logging.log("Setting the new autoscaling policy:")
        logging.raw_log(str(autoscaling_policy))

        # 1. We retrieve the previous autoscaling policy and save it to cluster_metadata
        cluster_metadata = await gos_oci.get_cluster_metadata(boat_profile)
        original_autoscaling = cluster_metadata["data"]["autoScalingOptions"]
        logging.log(" - the original autoscaling policy was set to:")
        logging.raw_log(str(original_autoscaling))

        # 2. We retrieve the configuration id that will serve as a base for new autoscaling policy
        configuration_id = await gos_oci.get_instance_configuration_id(instance_configuration_shape, boat_profile)
        logging.log(f" - obtained template id for the new autoscaling policy based on shape '{instance_configuration_shape}': {configuration_id}")

        # 3. We set the new autoscaling policy based on the obtained configuration id (this takes some time)
        await gos_oci.set_autoscaling_for_cluster(configuration_id, autoscaling_policy, boat_profile)

        # 4. We check what was really set
        cluster_metadata = await gos_oci.get_cluster_metadata(boat_profile)
        logging.log(" - the new autoscaling policy was successfully set to:")
        logging.raw_log(str(cluster_metadata["data"]["autoScalingOptions"]))

    logging.header(f"Running the '{scenario_config.name}' scenario")
    logging.raw_log(scenario_config)
    logging.raw_log("")
    log_metric_groups_summary(scenario_config)

    # Group all resources used during scenario run and cleanup
    resources = ScenarioResources(nginx_log_file=nginx_log_file, tinyinit_log_file=tinyinit_log_file)

    timeout = scenario_config.harness.scenario_timeout
    benchmarking_results: list[MetricList] = []
    modified_data_points = []
    orchestration_succeeded = False
    try:
        benchmarking_results = await _orchestrate_scenario(scenario_config, execution_ctx, resources, timeout)
        orchestration_succeeded = True
    except TimeoutError as e:
        mark_scenario_as_failed(execution_ctx, f"[!!!] Scenario timed out after (at least {timeout}) seconds [!!!]", e, log_non_debug=False, status=SCENARIO_STATUS_FAILED_TIMEOUT)
    except Exception as e:
        mark_scenario_as_failed(execution_ctx, "[!!!] Scenario failed with an unexpected exception [!!!]", e)
    finally:
        execution_ctx.mark_benchmarking_as_finished()
        try:
            modified_data_points = finalize_scenario_results(scenario_config, execution_ctx, resources, benchmarking_results, orchestration_succeeded)
        except Exception as e:
            mark_scenario_as_failed(execution_ctx, "[!!!] Scenario results postprocessing failed [!!!]", e, status=SCENARIO_STATUS_FAILED_POSTPROCESSING)

        # Note, we need to use [::] to copy the list and not create reference
        apps_to_delete: list[DeployedGraalApp | manage_apps.ListedApp] = list(resources.graalos_apps)
        # Sometimes, we get a timeout, even though the application was correctly created;
        #  - This is a workaround, that tries to fetch those apps and properly clean them up
        #  - We only get their display names, so we fetch all active apps and filter out based on unique names
        #  - Note, that this might delete some apps that were not launched by this process, but we have to ask for forgiveness then.
        failed_apps_to_delete = await manage_apps.fetch_apps(states=["ACTIVE","FAILED","CREATING","UPDATING"], filter_display_names=resources.failed_graalos_apps) if resources.failed_graalos_apps else []
        apps_to_delete.extend(failed_apps_to_delete)
        logging.header("Cleaning up")
        logging.log("Cleaning up following resources:")
        logging.log(f" - {gos_oci.num_disposable_instances()} disposable instances")
        logging.log(f" - {len(apps_to_delete)} graal apps ({len(failed_apps_to_delete)} failed, but active)")
        stack_apps_to_delete = [app for app in resources.deployed_stack_apps if app.application_id is not None]
        logging.log(f" - {len(stack_apps_to_delete)} apps on GraalOS marketplace stack")
        logging.log(f" - {resources.deployed_kegs_count} kegs")

        logging.log("Cleaning compute instances")
        instances_cleanup_coroutines = [gos_oci.terminate_disposable_instances()] if gos_oci.num_disposable_instances() > 0 else []
        instances_cleanup_result = await asyncio.gather(*instances_cleanup_coroutines, return_exceptions=True)
        failed_instances_cleanup = [res for res in instances_cleanup_result if isinstance(res, BaseException)]
        if len(failed_instances_cleanup) > 0:
            logging.log(f"!!! ERROR: Cleaning up {len(failed_instances_cleanup)} compute instances failed ! Please manually terminate those instances.")
            logging.log_exceptions(failed_instances_cleanup)
        logging.log(f" - terminated {len(instances_cleanup_result)-len(failed_instances_cleanup)} instances.")
        logging.log(" - cleaning done!")

        max_async_graal_app_deletions = scenario_config.harness.apps_deletion_concurrency
        logging.log("Cleaning apps on service")
        graal_apps_cleanup_result = await async_cmd_runner.batch_run(
            batch_items=apps_to_delete,
            batch_fun=lambda app: gos_oci.delete_graal_app(app.ocid),
            batch_step=max_async_graal_app_deletions,
            progress_label="Deleting service apps",
        )
        graal_apps_cleanup_result.assert_run_was_successful(f"Cleanup of {len(apps_to_delete)} graal applications failed; consider manually deleting them: ")
        logging.log("  - cleaning done!")

        logging.log("Cleaning apps on marketplace stack")
        deployed_stack_apps_result = await async_cmd_runner.batch_run(
            batch_items=stack_apps_to_delete,
            batch_fun=lambda app: app.delete(),
            batch_step=max_async_graal_app_deletions,
            progress_label="Deleting stack apps",
        )
        deployed_stack_apps_result.assert_run_was_successful(f"Cleanup of {len(stack_apps_to_delete)} graalos stack applications failed; consider manually deleting them: ")
        logging.log("  - cleaning done!")

        logging.log("Cleaning load generators")
        for load_generator in resources.load_generators:
            load_generator.cleanup()
        logging.log("  - cleaning done!")

        logging.log(f"Cleaning {resources.deployed_kegs_count} kegs")
        if resources.deployed_kegs_count > 0:
            await local_runner.clean_kegs_from_dataplane(resources.deployed_kegs, execution_ctx)
        logging.log("  - cleaning done!")

        logging.log("Closing log files")
        if resources.nginx_log_file is not None:
            resources.nginx_log_file.close()
        if resources.tinyinit_log_file is not None:
            resources.tinyinit_log_file.close()

        # Restoring to the previous autoscaling policy after the actual benchmarking
        if scenario_config.harness.autoscaling_policy != None:
            boat_profile = scenario_config.harness.boat_profile
            assert boat_profile is not None
            logging.log("Restoring to the previous autoscaling policy.")
            configuration_id = cluster_metadata['data']['instanceConfigurationId']
            assert original_autoscaling is not None
            await gos_oci.set_autoscaling_for_cluster(configuration_id, original_autoscaling, boat_profile)
            cluster_metadata = await gos_oci.get_cluster_metadata(boat_profile)
            logging.log(" - the original autoscaling policy was restored to:")
            logging.raw_log(str(cluster_metadata["data"]["autoScalingOptions"]))

        # Running the teardown phase, if it is specified
        #  - this is used to stop the services, and remove temporary or runtime user artifacts.
        for group, teardown in AppConfig.teardown_scripts.items():
            logging.log(f" - Running teardown script for '{group}' group: {teardown}")
            res, _, _ = await async_cmd_runner.run(['bash', teardown], timeout=360)
            if res.returncode != 0:
                raise RuntimeError(f"{teardown} for group {group} has failed")

        # Finally disposing the marketplace stack, if the lifecycle is set to 'disposable'
        if resources.graalos_marketplace_stack is not None and resources.graalos_marketplace_stack.lifecycle == "disposable":
            try:
                await resources.graalos_marketplace_stack.delete()
            except Exception as e:
                stack_id = resources.graalos_marketplace_stack.stack_id
                logging.log("!!! ERROR: Cleanup of disposable GraalOS marketplace stack failed.")
                logging.log(f"!!! Please manually clean the marketplace resources for stack '{stack_id}'.")
                logging.log("!!! Suggested manual cleanup: rerun stack destroy/delete or remove failed ORM jobs in OCI Resource Manager.")
                logging.log_exception(e)

    # Ensures that one point is always generated, even if postprocessing did not generate any points.
    if execution_ctx.scenario_status != SCENARIO_STATUS_SUCCESS and not modified_data_points:
        modified_data_points = bench_server.duplicate_start_ts_results(MetricList().add_metric(ScenarioSuccessMetric(metric_value=0), scenario_config.metric_groups).serialize_to_dict(execution_ctx), "extra.load-test-ts")

    if execution_ctx.upload_results:
        bench_server.bench_server_upload(modified_data_points)
        logging.log(f"Bench server uploaded completed for scenario. Name: '{scenario_config.name}'. ID: {scenario_config.id}")
    else:
        bench_server.save_results_json(modified_data_points)
        logging.log(f"Skipping bench server upload...")

    logging.log(f"Scenario completed in {time.time() - execution_ctx.scenario_start_ts:.3f} sec")
    logging.log(f" - Status: {execution_ctx.scenario_status}")
    logging.log(f" - Name: '{scenario_config.name}'")
    logging.log(f" - ID: {scenario_config.id}")

    return 0 if execution_ctx.scenario_status == SCENARIO_STATUS_SUCCESS else 1, modified_data_points

async def create_logs_for_graal_apps(graal_apps: list[DeployedGraalApp], max_async_logs_creations: int, timeout_handler: Optional[Callable[[], None]] = None) -> None:
    """For list of graal apps create a logs in several batches.

    :param graal_apps: list of DeployedGraalApps containing OCIDs of the applications
    :param max_async_logs_creations: maximum number of logs that are created asynchronously
    """
    graal_apps_count = len(graal_apps)
    logging.log(f"Creating logs for {graal_apps_count} apps")
    batch_result = await async_cmd_runner.batch_run(
        batch_items=graal_apps,
        batch_fun=lambda app: gos_oci.create_graal_app_log(app),
        batch_step=max_async_logs_creations,
        abort_early=True,
        timeout_handler=timeout_handler
    )
    batch_result.assert_run_was_successful(f"Couldn't create a logs for the {graal_apps_count}", raise_on_assertion_failure=True)


class VersionAction(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any, option_string: Optional[str] = None) -> None:
        print(f"{get_version()}")
        sys.exit(0)


def generate_scenario_visualization_data_uri(scenario_config: ScenarioConfig) -> Optional[str]:
    """Renders scenario_visualizer output and returns it as a PNG data URI."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        image_path = os.path.join(tmp_dir, "scenario-visualization.png")
        scenario_visualizer.render(scenario_config, image_path)
        if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
            return None
        with open(image_path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("ascii")
        return f"data:image/png;base64,{encoded}"


def main(argv: list[str] = sys.argv) -> int:
    supported_metrics = ", ".join(list_supported_metric_names())
    parser = argparse.ArgumentParser(description='Load Tester for Graal project components.', epilog=f"Supported metric.name values: {supported_metrics}")
    parser.add_argument("scenario", help="path to the scenario toml file", type=pathlib.Path)
    parser.add_argument("-p", "--parameter", action='append', dest='parameters',
                        metavar='PARAM=VALUE', default=[],
                        help='Override existing parameter from the "parameters" section of the scenario file (can be used multiple times)')
    parser.add_argument("--describe", action='store_true', help='Dumps the parsed scenario config file and exits.')
    parser.add_argument("--visualize", action='store_true', help='Dumps an image of the scenario load testing timeline.')
    parser.add_argument("--generate-offline-report", action='store_true', help="Generates a static HTML report from the benchmark results.")
    upload_group = parser.add_mutually_exclusive_group()
    upload_group.add_argument("--skip-upload", dest="upload_results", action='store_false', help='Skips upload of results json to bench server. Dumps it to \'bench-results.json\'. [default]')
    upload_group.add_argument("--upload-results", dest="upload_results", action='store_true', help='Upload the results json to bench server.')
    load_generator_group = parser.add_mutually_exclusive_group()
    load_generator_group.add_argument("--local-load-generators", dest="load_generators", action='store_const', const=LoadGeneration.LOCAL, help='Spawn all load generators locally (requires wrk and wrk2 installed). [default]')
    load_generator_group.add_argument("--remote-load-generators", dest="load_generators", action='store_const', const=LoadGeneration.REMOTE, help='Spawn all load generators remotely using dedicated compute instances for each.')
    parser.add_argument("--log-to", type=str, default="stdout", help='Logs the output to different streams. Each stream is delimited by comma, supported logs are: stdout, file:<PATH>')
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="INFO", help='Sets the log level')
    parser.add_argument("--enable-service-logs", action='store_true', help='Enables logs for all applications created in the GraalOS service')
    parser.add_argument("--version", action=VersionAction, help="Print version information and exit.")
    graalos.add_auth_arguments(parser)
    parser.set_defaults(load_generators=LoadGeneration.LOCAL, upload_results=False)
    args = parser.parse_args(argv[1:])
    logging.setup_logger(args.log_to, args.log_level)
    log_capture_handler = logging.attach_memory_log_handler() if args.generate_offline_report else None

    general_log_file = None
    for file_match in re.finditer(r"file:([^,]+)", args.log_to):
        general_log_file = file_match.group(1)
        logging.log(f"Dumping all graalos-load-tester logs into '{general_log_file}' file.")

    parameters = scenario_parser.parse_cli_parameters(args.parameters)

    if not os.path.exists(args.scenario):
        logging.die(f"Scenario file '{args.scenario}' does not exist. Make sure you provide proper path to the scenario")
    scenario_config = scenario_parser.parse_scenario_file(args.scenario, parameters=parameters)
    if args.describe:
        logging.log(scenario_config)
        log_metric_groups_summary(scenario_config)
        sys.exit(0)

    if args.visualize:
        setup_check.require_package("matplotlib")
        time_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        image_name = f"{time_str}_{args.scenario.stem}.png"
        scenario_visualizer.render(scenario_config, image_name)
        sys.exit(0)

    original_scenario_toml = None
    parsed_scenario_toml = None
    scenario_visualization_data_uri = None
    if args.generate_offline_report:
        setup_check.require_package("matplotlib")
        original_scenario_toml = args.scenario.read_text()
        parsed_scenario_toml = scenario_parser.load_processed_scenario_toml_from_file(args.scenario)
        scenario_visualization_data_uri = generate_scenario_visualization_data_uri(scenario_config)

    service_load_testing_enabled = any(app.deployment == "service" for app in scenario_config.apps)
    if service_load_testing_enabled:
        if scenario_config.graalos.service is None:
            logging.die("Missing '[graalos.service]' configuration needed for service load testing")
    graalos.configure_from_scenario_config(args, scenario_config.graalos)

    describe_deployment(scenario_config, args)

    setup_check.verify_requirements(
        check_oci_cli=service_load_testing_enabled and not args.remote_oci,
        check_load_generation=True,
        check_uploader=args.upload_results,
        check_remote_load_generators=args.load_generators == LoadGeneration.REMOTE,
        check_local_runners=any([app for app in scenario_config.apps if app.deployment in LOCAL_APP_DEPLOYMENT_TYPES]),
        check_local_gvisor=any([app for app in scenario_config.apps if app.deployment == 'nginx-tinyinit-gvisor']),
        check_local_firecracker=any([app for app in scenario_config.apps if app.deployment == 'nginx-tinyinit-firecracker']),
        check_dataplane=any([app for app in scenario_config.apps if app.deployment == 'dataplane']),
        check_signed_request_support=any([generator for generator in scenario_config.generators if hasattr(generator.method, "signed_requests") and generator.method.signed_requests == True])
    )

    loop = asyncio.get_event_loop()

    # If we are using security token for authentication:
    #   - we create a periodic task that refreshes the token every gos_oci.AUTH_TOKEN_REFRESH_INTERVAL
    #   - this is to ensure that the authentication stays fresh during long benchmarking scenarios
    #   - the security tokens have limited validity (about one hour)
    if graalos.auth == 'security_token':
        profile = graalos.profile
        assert profile is not None
        loop.create_task(gos_oci.refresh_token(profile))
    if scenario_config.harness.boat_profile:
        loop.create_task(gos_oci.refresh_token(scenario_config.harness.boat_profile))
    rc, data_points = loop.run_until_complete(
        run_scenario(
            scenario_config,
            load_generation=args.load_generators,
            upload_results=args.upload_results,
            general_log_file=general_log_file,
            log_service_apps=args.enable_service_logs,
            # main() already applied scenario config plus CLI auth/profile overrides.
            configure_graalos=False,
        )
    )
    if args.generate_offline_report:
        report_visualizer.render(
            data_points,
            original_scenario_toml=original_scenario_toml,
            parsed_scenario_toml=parsed_scenario_toml,
            scenario_visualization_data_uri=scenario_visualization_data_uri,
            log_output=log_capture_handler.content() if log_capture_handler else None,
            scenario_config=scenario_config,
            load_generation=args.load_generators.value,
        )
    return rc


if __name__ == "__main__":
    logging.setup_logger('stdout')
    sys.exit(main(sys.argv))
