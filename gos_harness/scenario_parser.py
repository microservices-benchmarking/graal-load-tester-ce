from __future__ import annotations

import pathlib
import uuid
import re
import toml
import json
import os
import sys
import math
from collections.abc import Mapping
from collections import OrderedDict
from itertools import chain
from enum import Enum
from typing import List, Union, Callable, Optional, Any, Type, NoReturn

from gos_harness import gos_logging as logging
from gos_harness.config import graalos_config as runtime_graalos_config

EXTRA_GRAALHOST_DIR_MOUNTS = "extra_graalhost_dir_mounts"
GRAALHOST_CONFIG_FILE = "graalhost_config_file"
FIRECRACKER_KERNEL_IMAGE = "firecracker_kernel_image"
FIRECRACKER_MEM_MIB = "firecracker_mem_mib"
FIRECRACKER_VCPU_COUNT = "firecracker_vcpu_count"
FIRECRACKER_ROOTFS_SIZE_MIB = "firecracker_rootfs_size_mib"

DEFAULT_SUCCESS_THRESHOLD = 0.99

RANGES_MATCHER: re.Pattern = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")

# Tuple of keys that are reserved and cannot be redefined by user
RESERVED_ENVIRONMENT_KEYS: tuple[str, ...] = ("IDX", "BATCH_IDX", "NUM_APPS", "ALL")
APP_TYPES: tuple[str, ...] = ("SERVER", "FUNCTION")
ALLOWED_MEMORY: tuple[int, ...] = (128, 256, 512, 1024, 2048)
ALLOWED_OCPUS: tuple[float, ...] = (0.0625, 0.125, 0.25, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4)
APP_DEPLOYMENT_TYPES: tuple[str, ...] = ("service", "dataplane", "nginx-tinyinit", "nginx-tinyinit-graalhost" , "nginx-tinyinit-gvisor", "nginx-tinyinit-firecracker", "predeployed", "graalos-stack")
LOCAL_APP_DEPLOYMENT_TYPES: tuple[str, ...] = ("nginx-tinyinit", "nginx-tinyinit-graalhost", "nginx-tinyinit-gvisor", "nginx-tinyinit-firecracker")
SCENARIO_DEPLOYMENT_TYPES: tuple[str, ...] = APP_DEPLOYMENT_TYPES + ("hybrid", )
ValidationCond = Union[Callable[[Any], bool], Type[Any], bool, None]

class NetworkType(Enum):
    """Specifies the type of the network for the app:

    Public = application can communicate with the internet
    Private = application can communicate only within the private subnet
    """
    PUBLIC = 1
    PRIVATE = 2

class UserPostprocessingConfig:
    def __init__(self, name: str, code: str):
        self._name = name
        self._code = code

    @property
    def name(self) -> str:
        return self._name

    @property
    def code(self) -> str:
        return self._code

    def __str__(self) -> str:
        return f"Metric({self.name}, {self.code.strip()[0:20] + '...'})"


class MetricGroupConfig:
    """Configuration of group of metrics

    :ivar _collect: list of metrics collected by the metric group
    :ivar _group: name of the group
    :ivar _enabled: bool flag indicating if the group is enabled
    :ivar _fail_on_no_metrics: bool flag indicating whether to fail when the group produces no metrics
    :ivar _postprocess: filter of sampler that is used to postprocess the metrics
    """
    def __init__(
        self,
        collect: list[str],
        group: Optional[str] = None,
        enabled: bool = True,
        fail_on_no_metrics: bool = True,
        postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig] = None,
    ):
        self._collect: list[str] = collect
        self._group: str = group or ""
        self._enabled: bool = enabled
        self._fail_on_no_metrics: bool = fail_on_no_metrics
        self._postprocess: Optional[MetricGroupSamplerConfig | MetricGroupFilterConfig] = postprocess

    def __str__(self) -> str:
        postprocess_str = "" if not self.postprocess else f", postprocess = {self.postprocess}"
        enabled_str = "" if self.enabled else ", enabled = false"
        fail_str = "" if self.fail_on_no_metrics else ", fail_on_no_metrics = false"
        return f"MetricGroup(group = '{self.group}', collect = {self.collect}{enabled_str}{fail_str}{postprocess_str})"

    @property
    def collect(self) -> list[str]:
        return self._collect

    @property
    def group(self) -> str:
        return self._group

    @property
    def postprocess(self) -> Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig]:
        return self._postprocess

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def fail_on_no_metrics(self) -> bool:
        return self._fail_on_no_metrics

class MetricGroupFilterConfig:
    """Configuration of filter of group of metrics

    :ivar _next_postprocess: next filter or sampler in the chain
    """
    def __init__(self, postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig] = None):
        self._next_postprocess: Optional[MetricGroupSamplerConfig | MetricGroupFilterConfig] = postprocess
        self._filterby: Optional[str] = None

    @property
    def filterby(self) -> str:
        assert self._filterby, "MetricGroupFilter has to set _filterby to a valid filter type"
        return self._filterby

    @property
    def next_postprocess(self) -> Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig]:
        return self._next_postprocess

class MetricGroupFilterByFieldsConfig(MetricGroupFilterConfig):
    """Configuration of filter of group of metrics based on fields

    :ivar _filterby: type of the filter
    :ivar _fields: dictionary of fields through which we filter
    """
    def __init__(self, fields: dict[str, list[str] | str], postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig] = None):
        super().__init__(postprocess=postprocess)
        self._filterby = "fields"
        self._fields = fields

    @property
    def fields(self) -> dict[str, list[str] | str]:
        return self._fields

    def __str__(self) -> str:
        postprocess_str = "" if not self.next_postprocess else f", postprocess = {self.next_postprocess}"
        return f"MetricGroupFilterByFields(fields = {self.fields}{postprocess_str})"

class MetricGroupFilterByQuantileRanges(MetricGroupFilterConfig):
    """Configuration of filter of group of metrics based on quantile ranges

    :ivar _filterby: type of the filter
    :ivar _groupby: list of keys, which are used to group the metrics first
    :ivar _ranges: comma delimited list of quantile ranges
    """
    def __init__(self, ranges: str, groupby: Optional[list[str]] = None, postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig] = None):
        super().__init__(postprocess=postprocess)
        self._filterby = "quantile-range"
        self._groupby = groupby
        self._ranges = ranges

    @property
    def groupby(self) -> Optional[list[str]]:
        return self._groupby

    @property
    def ranges(self) -> str:
        return self._ranges

    def __str__(self) -> str:
        postprocess_str = "" if not self.next_postprocess else f", postprocess = {self.next_postprocess}"
        return f"MetricGroupFilterByQuantileRanges(groupby = {self.groupby}, ranges = {self.ranges}{postprocess_str})"

class MetricGroupSamplerConfig:
    """Configuration of sampler of group of metrics

    :ivar _next_postprocess: next filter or sampler in the chain
    """
    def __init__(self, postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig] = None):
        self._next_postprocess: Optional[MetricGroupSamplerConfig | MetricGroupFilterConfig] = postprocess
        self._domain: Optional[str] = None

    @property
    def next_postprocess(self) -> Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig]:
        return self._next_postprocess

    @property
    def domain(self) -> str:
        assert self._domain, "MetricGroupSampler has to set _domain to a valid sampling domain"
        return self._domain

class MetricGroupDiscreteSampler(MetricGroupSamplerConfig):
    """Configuration of discrete-domain sampler of group of metrics

    :ivar _domain: domain of the sampling
    :ivar _sampleby: field by which we sample
    :ivar _rate: rate by which we sample
    """
    def __init__(self, rate: int, sampleby: Optional[str] = None, groupby: Optional[list[str]] = None, postprocess: Optional[MetricGroupFilterConfig | MetricGroupSamplerConfig] = None):
        super().__init__(postprocess=postprocess)
        self._domain = "discrete"
        self._sampleby = sampleby
        self._groupby = groupby
        self._rate = rate

    @property
    def sampleby(self) -> Optional[str]:
        return self._sampleby

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def groupby(self) -> Optional[list[str]]:
        return self._groupby

    def __str__(self) -> str:
        postprocess_str = "" if not self.next_postprocess else f", postprocess = {self.next_postprocess}"
        return f"MetricGroupDiscreteSampler(sampleby = {self.sampleby}, groupby = {self.groupby}, rate = {self.rate}{postprocess_str})"

class BinaryConfig:
    def __init__(self, environment: Optional[dict[str, Any]] = None, network_mode: NetworkType = NetworkType.PUBLIC, subnet_id: Optional[str] = None):
        """
        :ivar _network_mode: mode of the network:
          - public: application is exposed to the internet, cannot communicate with private subnets
          - private: application is attached to the configured private subnet
        :ivar _subnet_id: OCID of the subnet used for private applications
        :ivar _environment: environment that is passed to the app
        """
        self._artifact_id: Optional[str] = None
        self._network_mode = network_mode
        self._subnet_id = subnet_id
        self._environment = {} if environment is None else environment

    @property
    def artifact_id(self) -> Optional[str]:
        return self._artifact_id

    @property
    def network_mode(self) -> NetworkType:
        return self._network_mode

    @property
    def subnet_id(self) -> Optional[str]:
        return self._subnet_id

    @property
    def environment(self) -> dict[str, Any]:
        return self._environment


class BinaryFromArtifactId(BinaryConfig):
    def __init__(self, artifact_id: str, environment: dict[str, Any], network_mode: NetworkType, subnet_id: Optional[str]):
        super().__init__(environment, network_mode, subnet_id)
        self._artifact_id = artifact_id

    @property
    def artifact_id(self) -> Optional[str]:
        return self._artifact_id

    def __str__(self) -> str:
        return f"from-id({self.artifact_id})"


class BinaryFromArtifactIdFileConfig(BinaryConfig):
    def __init__(self, file_path: pathlib.Path, index: int, root_dir: pathlib.Path, environment: dict[str, Any], network_mode: NetworkType, subnet_id: Optional[str]):
        super().__init__(environment, network_mode, subnet_id)
        assert isinstance(file_path, pathlib.Path), f"Unexpected type for file_path: {type(file_path)} !"
        self._file_path = file_path
        self._index = index
        self._root_dir = root_dir

    @property
    def file_path(self) -> pathlib.Path:
        return self._file_path

    @property
    def index(self) -> int:
        return self._index

    @property
    def root_dir(self) -> pathlib.Path:
        return self._root_dir

    @property
    def artifact_id(self) -> Optional[str]:
        if self._artifact_id is None:
            file_path = self.file_path if self.file_path.is_absolute() else self.root_dir / self.file_path
            with file_path.open() as id_file:
                self._artifact_id = id_file.readlines()[self.index].strip()
        return self._artifact_id

    def __str__(self) -> str:
        return f"from-id-file({self.file_path}, {self.artifact_id})"


class RunningKegBinaryConfig(BinaryConfig):
    """
    :ivar _keg_domain: network domain, where the keg is running
    :ivar _keg_id: string id of the keg, the app then is running on _keg_id._keg_domain address
    :ivar _binary_path: path to the app that will be run inside the keg
    :ivar _launch_arguments: arguments that are passed to the app
    """
    def __init__(self, keg_id: str, binary_path: str, keg_domain: str, launch_arguments: str):
        super().__init__()
        self._keg_domain = keg_domain
        self._keg_id = keg_id
        self._binary_path = binary_path
        self._launch_arguments = launch_arguments

    @property
    def keg_domain(self) -> str:
        return self._keg_domain

    @property
    def keg_id(self) -> str:
        return self._keg_id

    @property
    def binary_path(self) -> str:
        return self._binary_path

    @property
    def launch_arguments(self) -> str:
        return self._launch_arguments

    @property
    def artifact_id(self) -> Optional[str]:
        return None

    @property
    def base_url(self) -> str:
        return f"{self.keg_id}.{self.keg_domain}"

    def __str__(self) -> str:
        return f"{self._binary_path} running-at({self.base_url})"

class GraalOSStackBinaryConfig(BinaryConfig):
    """Configuration for a binary running inside the GraalOS marketplace stack

    :ivar domain_prefix: prefix domain, which will be used as a target for the curls in form of <domain_prefix>.<base_domain>
    :ivar base_domain: base domain configured by the GraalOS marketplace stack
    :ivar container_image: a full pull url (obtainable from container registry) and SHA256 of the image that will be deployed
      - e.g. <registry>/<tenancy>/<repo>:<tag>@sha256:<digest>
    :ivar container_image_id: OCID of the pulled container image
    :ivar entrypoint: container entrypoint passed to the marketplace deployment payload
    :ivar environment: environment passed to the application on the marketplace stack
    :ivar guest_port: optional guest port passed to the marketplace deployment payload
    """
    def __init__(self, domain_prefix: str, base_domain: str, container_image: str, container_image_id: str, entrypoint: list[str], environment: dict[str, Any], guest_port: Optional[int] = None):
        super().__init__(environment=environment)
        self._domain_prefix = domain_prefix
        self._base_domain = base_domain
        self._container_image = container_image
        self._container_image_id = container_image_id
        self._entrypoint = entrypoint
        self._guest_port = guest_port

    @property
    def domain(self) -> str:
        """Final domain name, where the app is reachable"""
        return f"{self.domain_prefix}.{self.base_domain}"

    @property
    def base_url(self) -> str:
        """Full URL, where the app is reachable"""
        return f"https://{self.domain}"

    @property
    def domain_prefix(self) -> str:
        """Domain prefix is used to form the final domain name"""
        return self._domain_prefix

    @property
    def base_domain(self) -> str:
        """Base domain used with the domain prefix to form the full domain of the app"""
        return self._base_domain

    @property
    def container_image(self) -> str:
        """Container image that will be pulled in form of <PULL URL>@<SHA256>

        See OCI Container Registry for the configured region to find available container images.
        """
        return self._container_image

    @property
    def container_image_id(self) -> str:
        """Container image id that will be pulled in form of an OCID

        See OCI Container Registry for the configured region to find available container image ids.
        """
        return self._container_image_id

    @property
    def entrypoint(self) -> list[str]:
        """Container entrypoint passed to the marketplace stack deployment payload"""
        return self._entrypoint

    @property
    def guest_port(self) -> Optional[int]:
        """Guest port passed to the marketplace stack deployment payload"""
        return self._guest_port

    def __str__(self) -> str:
        return f"{self.container_image} ({self.container_image_id}, entrypoint = {self.entrypoint}, guest_port = {self.guest_port}) running-at {self.base_url}"



class LocalBinaryConfig(BinaryConfig):
    """
    :ivar _binary_command: command for executing the compiled native image binary that will be spawned by tinyinit
    :ivar _base_url: url, where the application will be listening for requests
    :ivar _nginx_keepalive: number of connections that are kept alive by nginx
    :ivar _nginx_keepalive_requests: number of requests kept on one nginx upstream connection
    :ivar _nginx_keepalive_timeout: timeout of keeping the connections alive by nginx; can be supplied as string such as "2s"
    :ivar _extra_graalhost_dir_mounts: list of directories to be mounted on graalhost
    :ivar _graalhost_config_file: the path of the config file to use for graalhost. Cannot be combined with a non-empty _extra_graalhost_dir_mounts
    :ivar _firecracker_kernel_image: path to Firecracker guest kernel image
    :ivar _firecracker_mem_mib: guest memory size in MiB
    :ivar _firecracker_vcpu_count: guest vCPU count
    :ivar _firecracker_rootfs_size_mib: size of the generated guest rootfs in MiB
    """
    def __init__(
        self,
        binary_command: str,
        base_url: str = "127.0.0.1",
        unique_copy: bool = False,
        nginx_keepalive: int = 5,
        nginx_keepalive_timeout: str = "5",
        extra_graalhost_dir_mounts: Optional[List[str]] = None,
        graalhost_config_file: str | None = None,
        firecracker_kernel_image: str | None = None,
        firecracker_mem_mib: int = 512,
        firecracker_vcpu_count: int = 1,
        firecracker_rootfs_size_mib: int | None = None,
        environment: Optional[dict[str, Any]] = None,
        nginx_keepalive_requests: int = 10_000_000,
    ):
        def both_graal_host_config_options_set(extra_graalhost_dir_mounts: Optional[List[str]], graalhost_config_file: str | None) -> bool:
            """
            Check if both of the mutually exclusive graalhost config options (see GRAALHOST_CONFIG_FILE and EXTRA_GRAALHOST_DIR_MOUNTS) are set.
            TODO extra_graalhost_dir_mounts seems to have 2 null values: None and [], this should be unifed to one
            """
            return graalhost_config_file is not None and extra_graalhost_dir_mounts is not None and len(
                extra_graalhost_dir_mounts) > 0

        super().__init__(environment=environment)
        self._base_url = base_url
        self._binary_command = binary_command
        self._unique_copy = unique_copy
        self._nginx_keepalive = nginx_keepalive
        self._nginx_keepalive_timeout = nginx_keepalive_timeout
        self._nginx_keepalive_requests = nginx_keepalive_requests
        if both_graal_host_config_options_set(extra_graalhost_dir_mounts, graalhost_config_file):
            error(f"Cannot use both {GRAALHOST_CONFIG_FILE} and {EXTRA_GRAALHOST_DIR_MOUNTS} for one binary.")
        self._extra_graalhost_dir_mounts = extra_graalhost_dir_mounts
        self._graalhost_config_file = graalhost_config_file
        self._firecracker_kernel_image = firecracker_kernel_image
        self._firecracker_mem_mib = firecracker_mem_mib
        self._firecracker_vcpu_count = firecracker_vcpu_count
        self._firecracker_rootfs_size_mib = firecracker_rootfs_size_mib

    @property
    def artifact_id(self) -> Optional[str]:
        return None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def binary_command(self) -> str:
        return self._binary_command

    @property
    def unique_copy(self) -> bool:
        return self._unique_copy

    @property
    def nginx_keepalive(self) -> int:
        return self._nginx_keepalive

    @property
    def nginx_keepalive_timeout(self) -> str:
        return self._nginx_keepalive_timeout

    @property
    def nginx_keepalive_requests(self) -> int:
        return self._nginx_keepalive_requests

    @property
    def extra_graalhost_dir_mounts(self) -> Optional[List[str]]:
        return self._extra_graalhost_dir_mounts

    @property
    def graalhost_config_file(self) -> str | None:
        return self._graalhost_config_file

    @property
    def firecracker_kernel_image(self) -> str | None:
        return self._firecracker_kernel_image

    @property
    def firecracker_mem_mib(self) -> int:
        return self._firecracker_mem_mib

    @property
    def firecracker_vcpu_count(self) -> int:
        return self._firecracker_vcpu_count

    @property
    def firecracker_rootfs_size_mib(self) -> int | None:
        return self._firecracker_rootfs_size_mib

    def __str__(self) -> str:
        return f"{self._binary_command} running-at({self.base_url})"

class RunningBinaryConfig(BinaryConfig):
    def __init__(self, base_url: str):
        super().__init__()
        self._base_url = base_url

    @property
    def artifact_id(self) -> Optional[str]:
        return None

    @property
    def base_url(self) -> str:
        return self._base_url

    def __str__(self) -> str:
        return f"running-at({self.base_url})"


class ExpectConfig:
    """Class that encapsulates the configuration for testing the responses of the load generators.

    We support checking the following:

    1. Checking the code of the response,
    2. Checking the bodies of the response using regular expressions.

    :ivar code: expected http code of the response or None (then the code will not be checked)
    :ivar body: expected body of the response, stated as regular expression or None (then the body will not be checked)
    :ivar body_regex: regular expression compiled from the body
    """
    def __init__(self, code: Optional[int], body: Optional[str]):
        self._code = code
        self._body_regex = re.compile(body) if body else None

    @property
    def is_empty(self) -> bool:
        return self._code is None and self._body_regex is None

    @property
    def body(self) -> Optional[str]:
        return self._body_regex.pattern if self._body_regex else None

    @property
    def code(self) -> Optional[int]:
        return self._code

    def check(self, code: int, response_file: str) -> tuple[bool, bool]:
        return self.check_code(code), self.check_body(response_file)

    def check_code(self, code: int) -> bool:
        """If http code was set, check that the given http code is the same"""
        return self._code is None or self._code == code

    def check_body(self, response_file: str) -> bool:
        """If body was set, check that the response was obtained and it contains given body"""
        if self._body_regex is not None:
            if not os.path.exists(response_file):
                return False
            with open(response_file, 'r') as response_handle:
                response = response_handle.read()
            if self._body_regex.search(response) is None:
                return False
        return True

class TargetConfig:
    def __init__(self, apps: Union[int, List[int]], endpoints: Union[int, List[int]]):
        super().__init__()
        self._app_indices = [apps] if isinstance(apps, int) else apps
        self._endpoint_indices = [endpoints] if isinstance(endpoints, int) else endpoints

    @property
    def app_indices(self) -> List[int]:
        return self._app_indices

    @property
    def endpoint_indices(self) -> List[int]:
        return self._endpoint_indices

    def __str__(self) -> str:
        if len(self.app_indices) == 1 and len(self.endpoint_indices) == 1:
            return f"{self.app_indices[0]}/{self.endpoint_indices[0]}"

        return f"{self.app_indices}/{self.endpoint_indices}"


class ParallelTargetConfig(TargetConfig):
    def __str__(self) -> str:
        return f"parallel(app_idx = {self.app_indices}, endpoint_idx = {self.endpoint_indices})"

class SpikeTargetConfig(TargetConfig):
    """Sends the requests in one spike not waiting for the reply

    :ivar _pre_begin_sleep: time slept in seconds before starting the round robin rounds
    :ivar _agent_emit_interval_during_sleep: rate in seconds for firing the agents while sleeping
    :ivar _post_end_sleep: time slept in seconds after finishing all round robin rounds
    """
    def __init__(self, apps: Union[int, List[int]], endpoints: Union[int, List[int]], pre_begin_sleep: int = 0, agent_emit_interval_during_sleep: float = 0.0, post_end_sleep: int = 0):
        super().__init__(apps, endpoints)
        self._pre_begin_sleep = pre_begin_sleep
        self._agent_emit_interval_during_sleep = agent_emit_interval_during_sleep
        self._post_end_sleep = post_end_sleep

    @property
    def pre_begin_sleep(self) -> int:
        return self._pre_begin_sleep

    @property
    def agent_emit_interval_during_sleep(self) -> float:
        return self._agent_emit_interval_during_sleep

    @property
    def post_end_sleep(self) -> int:
        return self._post_end_sleep

    def __str__(self) -> str:
        return f"spike(app_idx = {self.app_indices}, endpoint_idx = {self.endpoint_indices}, pre_begin_sleep = {self._pre_begin_sleep}, agent_emit_interval_during_sleep = {self._agent_emit_interval_during_sleep}, post_end_sleep = {self._post_end_sleep}))"

class RoundRobinTargetConfig(TargetConfig):
    """
    :ivar _pre_begin_sleep: time slept in seconds before starting the round robin rounds
    :ivar _between_rounds_sleep: time slept in seconds between two round robin rounds
    :ivar _request_sleep_interval: time slept in seconds between two request
    :ivar _agent_emit_interval_during_sleep: rate in seconds for firing the agents while sleeping
    :ivar _post_end_sleep: time slept in seconds after finishing all round robin rounds
    """
    def __init__(self, apps: Union[int, List[int]], endpoints: Union[int, List[int]], pre_begin_sleep: int = 0, between_rounds_sleep: int = 0, request_sleep_interval: float = 0, agent_emit_interval_during_sleep: float = 0.0, post_end_sleep: int = 0):
        super().__init__(apps, endpoints)
        self._pre_begin_sleep = pre_begin_sleep
        self._between_rounds_sleep = between_rounds_sleep
        self._request_sleep_interval = request_sleep_interval
        self._agent_emit_interval_during_sleep = agent_emit_interval_during_sleep
        self._post_end_sleep = post_end_sleep

    @property
    def pre_begin_sleep(self) -> int:
        return self._pre_begin_sleep

    @property
    def between_rounds_sleep(self) -> int:
        return self._between_rounds_sleep

    @property
    def request_sleep_interval(self) -> float:
        return self._request_sleep_interval

    @property
    def agent_emit_interval_during_sleep(self) -> float:
        return self._agent_emit_interval_during_sleep

    @property
    def post_end_sleep(self) -> int:
        return self._post_end_sleep

    def __str__(self) -> str:
        return f"round-robin(app_idx = {self.app_indices}, endpoint_idx = {self.endpoint_indices}, pre_begin_sleep = {self._pre_begin_sleep}, between_rounds_sleep = {self.between_rounds_sleep}, request_sleep_interval = {self._request_sleep_interval}, agent_emit_interval_during_sleep = {self._agent_emit_interval_during_sleep}, post_end_sleep = {self._post_end_sleep})"


class AppConfig:
    """
    :ivar _gidx: group index of the application
    :ivar _idx: index of the application
    :ivar _deployment: type of the deployment, one of following options:
      - nginx-tinyinit = local deployment, with native image running behind tinyinit and nginx
      - nginx-tinyinit-graalhost = local deployment, with native image running under GraalVisor behind tinyinit and nginx
      - nginx-tinyinit-firecracker = local deployment, with native image running inside Firecracker behind tinyinit and nginx
      - dataplane = deployment as a keg inside the running dataplane
      - service = deployment as graalos-application in running graalos service
    :ivar _app_name: name of the app (can be used to refer in queries)
    :ivar _type: type of the application, one of the following options:
      - function = the application does not have underlying server logic, tinyinit will take care of sockets
      - server = the applications has underlying server logic and will take care of socket handling itself
    :ivar _protocol: http or https protocol
    :ivar _port: port where the application is contacted
    :ivar _memory: restriction to memory in MB (usually 256 or 2048MB)
    :ivar _endpoints: endpoint that is contacted, specified as list of endpoints
    :ivar _binary: additional configuration dependent on the deployment
    :ivar _version: version of the application; preferably in form of <MAJOR_VERSION>.<MINOR_VERSION>.<PATCH_VERSION>; empty if the version is not tracked
    """
    # Map of unique group names to setup and teardown scripts
    setup_scripts: dict[str, str] = {}
    teardown_scripts: dict[str, str] = {}

    def __init__(self, gidx: int, idx: int, app_name: str, app_type: str, protocol: str, port: int, memory: int, ocpus: float, endpoints: List[str], binary: BinaryConfig, deployment: str, version: str, metric_groups: list[MetricGroupConfig]):
        self._gidx = gidx
        self._idx = idx
        self._deployment = deployment
        self._app_name = app_name
        self._type = app_type.lower()
        self._protocol = protocol
        self._port = port
        self._ocpus = ocpus
        self._memory = memory
        self._endpoints = endpoints
        self._binary = binary
        self._version = version
        self._metric_groups = metric_groups

    @property
    def group_idx(self) -> int:
        return self._gidx

    @property
    def idx(self) -> int:
        return self._idx

    @property
    def deployment(self) -> str:
        return self._deployment

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def unique_name(self) -> str:
        return f"{self.idx}-{self.app_name}"

    @property
    def unique_groupname(self) -> str:
        return f"{self.group_idx}-{self.app_name}"

    @property
    def runtime_properties_file(self) -> Optional[str]:
        if self.unique_groupname in AppConfig.setup_scripts:
            return os.path.join(os.path.dirname(AppConfig.setup_scripts[self.unique_groupname]), "runtime-properties.toml")
        return None

    @property
    def type(self) -> str:
        return self._type

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def port(self) -> int:
        return self._port

    @property
    def memory(self) -> int:
        return self._memory

    @property
    def ocpus(self) -> float:
        return self._ocpus

    @property
    def endpoints(self) -> List[str]:
        return self._endpoints

    @property
    def binary(self) -> BinaryConfig:
        return self._binary

    @property
    def version(self) -> str:
        return self._version

    @property
    def metric_groups(self) -> list[MetricGroupConfig]:
        return self._metric_groups

    def __str__(self) -> str:
        memory_str = f"{self.memory} MBs" if self.memory else "<unspecified>"
        ocpus_str = f"{self.ocpus} OCPUs" if self.ocpus else "<unspecified>"
        deployment_str = self.deployment if self.deployment else "<unspecified>"
        version_str = self.version if self.version else "<unknown>"
        return f"App({self.idx}, {self.unique_name}, group = {self.unique_groupname}, version = {version_str}, type = {self.type}, protocol = {self.protocol}, port = {self.port}, memory = {memory_str}, ocpus = {ocpus_str}, endpoints = {self.endpoints}, binary = {self.binary}, deployment = {deployment_str}, metric_groups = [{', '.join(str(m) for m in self.metric_groups if m.enabled)}])"


class LoadStrategy(Enum):
    THROUGHPUT = 1
    LATENCY = 2
    REQUESTS = 3


class GeneratorMethodConfig:
    @property
    def strategy(self) -> LoadStrategy:
        raise NotImplementedError("Method strategy not implemented !")

    @property
    def type(self) -> str:
        raise NotImplementedError("Method type not implemented !")

    @property
    def total_duration(self) -> int:
        raise ValueError("Total duration is undefined for the given load generator method !")


class FixedTimeMethodConfig(GeneratorMethodConfig):
    """
    `GeneratorMethodConfig` objects with a predictable duration.
    The 'total_duration' method will return the number of expected seconds.
    """
    @property
    def iterations(self) -> int:
        raise NotImplementedError("Iterations property not implemented !")

    @property
    def duration(self) -> int:
        raise NotImplementedError("Duration property not implemented !")

    @property
    def total_duration(self) -> int:
        return self.iterations * self.duration


class ThroughputMethodConfig(FixedTimeMethodConfig):
    def __init__(self, connections: int, threads: int, duration: int, iterations: int):
        super().__init__()
        self._connections = connections
        self._threads = threads
        self._duration = duration
        self._iterations = iterations

    @property
    def type(self) -> str:
        return "throughput"

    @property
    def connections(self) -> int:
        return self._connections

    @property
    def threads(self) -> int:
        return self._threads

    @property
    def duration(self) -> int:
        return self._duration

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def strategy(self) -> LoadStrategy:
        return LoadStrategy.THROUGHPUT

    def __str__(self) -> str:
        return f"{self.strategy.name.capitalize()}(c = {self.connections}, t = {self.threads}, duration = {self.duration}, iterations = {self.iterations})"


class LatencyMethodConfig(FixedTimeMethodConfig):
    def __init__(self, connections: int, threads: int, rate: int, duration: int, iterations: int):
        super().__init__()
        self._connections = connections
        self._threads = threads
        self._rate = rate
        self._duration = duration
        self._iterations = iterations

    @property
    def type(self) -> str:
        return "latency"

    @property
    def connections(self) -> int:
        return self._connections

    @property
    def threads(self) -> int:
        return self._threads

    @property
    def rate(self) -> int:
        return self._rate

    @property
    def duration(self) -> int:
        return self._duration

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def strategy(self) -> LoadStrategy:
        return LoadStrategy.LATENCY

    def __str__(self) -> str:
        return f"{self.strategy.name.capitalize()}(c = {self.connections}, t = {self.threads}, rate = {self.rate}, duration = {self.duration}, iterations = {self.iterations})"


class RequestsMethodConfig(GeneratorMethodConfig):
    def __init__(self, total_requests: int, timeout: int, request_timeout: int, signed_requests: bool = False, persistent_requests: bool = False):
        super().__init__()
        self._total_requests = total_requests
        self._request_timeout = request_timeout
        self._timeout = timeout
        self._signed_requests = signed_requests
        self._persistent_requests = persistent_requests

    @property
    def type(self) -> str:
        return "requests"

    @property
    def total_requests(self) -> int:
        return self._total_requests

    @property
    def request_timeout(self) -> int:
        return self._request_timeout

    @property
    def timeout(self) -> int:
        return self._timeout

    @property
    def signed_requests(self) -> bool:
        return self._signed_requests

    @property
    def persistent_requests(self) -> bool:
        return self._persistent_requests

    @property
    def strategy(self) -> LoadStrategy:
        return LoadStrategy.REQUESTS

    def __str__(self) -> str:
        return f"{self.strategy.name.capitalize()}(total_requests = {self.total_requests}, timeout = {self.timeout}, signed_requests = {self.signed_requests}, persistent_requests = {self.persistent_requests})"


class GeneratorConfig:
    def __init__(self, idx: int, name: str, delay: int, method: GeneratorMethodConfig, target: TargetConfig, success_threshold: float, expects: ExpectConfig, metric_groups: list[MetricGroupConfig], http_headers: Optional[list[str]] = None, attached_agents: Optional[list[int]] = None):
        self.idx = idx
        self._name = name
        self._delay = delay
        self._method = method
        self._target = target
        self._success_threshold = success_threshold
        self._expects = expects
        self._agents = attached_agents if attached_agents is not None else []
        self._http_headers = http_headers if http_headers is not None else []
        self._metric_groups = metric_groups

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_name(self) -> str:
        return f"{self.idx}{'-' if self.name else ''}{self.name}"

    @property
    def delay(self) -> int:
        return self._delay

    @property
    def method(self) -> GeneratorMethodConfig:
        return self._method

    @property
    def target(self) -> Union[TargetConfig]:
        return self._target

    @property
    def success_threshold(self) -> float:
        return self._success_threshold

    @property
    def expects(self) -> ExpectConfig:
        return self._expects

    @property
    def agents(self) -> list[int]:
        return self._agents

    @property
    def http_headers(self) -> list[str]:
        return self._http_headers

    @property
    def metric_groups(self) -> list[MetricGroupConfig]:
        return self._metric_groups

    def __str__(self) -> str:
        return f"Generator({self.idx}, name = '{self.name}', unique_name = '{self.unique_name}', delay = {self.delay}, method = {str(self.method)}, agents = {self.agents}, target = {self.target}, metric_groups = [{', '.join(str(m) for m in self.metric_groups if m.enabled)}])"

class AgentConfig:
    def __init__(self, idx: int, agent_type: str, attached_to_app: int, emitted_metrics: list[Any], metric_groups: list[MetricGroupConfig]):
        self.idx = idx
        self._type = agent_type
        self._attached_to_app = attached_to_app
        self._emitted_metrics = emitted_metrics
        self._metric_groups = metric_groups

    @property
    def type(self) -> str:
        return self._type

    @property
    def attached_to_app(self) -> int:
        return self._attached_to_app

    @property
    def emitted_metrics(self) -> list[Any]:
        return self._emitted_metrics

    @property
    def metric_groups(self) -> list[MetricGroupConfig]:
        return self._metric_groups

    def __str__(self) -> str:
        return f"Agent({self.idx}, type = {self.type}, attached_to_app = {self.attached_to_app}, emitted_metrics = {self._emitted_metrics}, metric_groups = [{', '.join(str(m) for m in self.metric_groups if m.enabled)}])"

class GraalOSStackConfig:
    """Configuration of the GraalOS stack corresponding to [graalos.stack] section in the TOML scenario
    
    :ivar lifecycle: lifecycle of the GraalOS stack; either 'persistent' (running and identifiable by IP), or
      'disposable' (freshly provisioned by given specification).
    :ivar stack_version: marketplace stack version used by both persistent and disposable stack lifecycle
    :ivar ip: IP of the running GraalOS stack
      - for 'persistent' stack this corresponds to the IP specified in the scenario;
      - for 'disposable' stack this is obtained automatically from the ORM job's output.
    :ivar marketplace_listing_id: OCID of the Marketplace listing used to resolve stack packages
    :ivar marketplace_region: Marketplace region used to resolve stack packages
    :ivar resource_manager_endpoint: OCI Resource Manager endpoint used to create, poll, and delete stacks
    :ivar domain: parent domain of the stack applications and stack API
    :ivar nameservers: nameservers passed to stack application deployment payloads
    :ivar specification: dictionary of specification of the GraalOS stack (how it will be provisioned)
    """
    def __init__(
        self,
        lifecycle: str,
        stack_version: str,
        marketplace_listing_id: str,
        marketplace_region: str,
        resource_manager_endpoint: str,
        domain: str,
        nameservers: list[str],
        ip: Optional[str],
        specification: dict[str, Any],
    ):
        self._lifecycle: str = lifecycle
        self._stack_version: str = stack_version
        self._marketplace_listing_id: str = marketplace_listing_id
        self._marketplace_region: str = marketplace_region
        self._resource_manager_endpoint: str = resource_manager_endpoint
        self._domain: str = domain
        self._nameservers: list[str] = nameservers
        self._ip: Optional[str] = ip
        self._specification: dict[str, Any] = specification

    @property
    def lifecycle(self) -> str:
        """Lifecycle of the GraalOS stack (persistent or disposable)"""
        return self._lifecycle

    @property
    def stack_version(self) -> str:
        """Marketplace stack version shared across stack lifecycles"""
        return self._stack_version

    @property
    def marketplace_listing_id(self) -> str:
        """Marketplace listing ID used to resolve stack packages"""
        return self._marketplace_listing_id

    @property
    def marketplace_region(self) -> str:
        """Marketplace region used to resolve stack packages"""
        return self._marketplace_region

    @property
    def resource_manager_endpoint(self) -> str:
        """OCI Resource Manager endpoint used for stack lifecycle operations"""
        return self._resource_manager_endpoint

    @property
    def domain(self) -> str:
        """Parent domain of the stack applications and stack API"""
        return self._domain

    @property
    def nameservers(self) -> list[str]:
        """Nameservers passed to stack application deployment payloads"""
        return self._nameservers

    @property
    def ip(self) -> Optional[str]:
        """IP of the running GraalOS stack"""
        return self._ip

    @property
    def specification(self) -> dict[str, Any]:
        """Specification of the GraalOS stack that will be freshly provisioned"""
        return self._specification

    def __str__(self) -> str:
        return f".stack(lifecycle = {self.lifecycle}, stack_version = {self.stack_version}, domain = {self.domain}, ip = {self.ip}, specification = {self.specification})"

class GraalOSServiceConfig:
    """Configuration of the GraalOS service corresponding to [graalos.service] section."""
    def __init__(
        self,
        tenancy: str,
        region: str,
        availability_domain: str,
        compartment_id: str,
        subnet_id: str,
        fault_domain: str,
        registry_domain: str,
        artifact_repository: str,
        network_attachment_id: str,
        log_group_id: str,
        cluster: runtime_graalos_config.Cluster,
    ):
        self._tenancy: str = tenancy
        self._region: str = region
        self._availability_domain: str = availability_domain
        self._compartment_id: str = compartment_id
        self._subnet_id: str = subnet_id
        self._fault_domain: str = fault_domain
        self._registry_domain: str = registry_domain
        self._artifact_repository: str = artifact_repository
        self._network_attachment_id: str = network_attachment_id
        self._log_group_id: str = log_group_id
        self._cluster: runtime_graalos_config.Cluster = cluster

    @property
    def tenancy(self) -> str:
        return self._tenancy

    @property
    def region(self) -> str:
        return self._region

    @property
    def availability_domain(self) -> str:
        return self._availability_domain

    @property
    def compartment_id(self) -> str:
        return self._compartment_id

    @property
    def subnet_id(self) -> str:
        return self._subnet_id

    @property
    def fault_domain(self) -> str:
        return self._fault_domain

    @property
    def registry_domain(self) -> str:
        return self._registry_domain

    @property
    def artifact_repository(self) -> str:
        return self._artifact_repository

    @property
    def network_attachment_id(self) -> str:
        return self._network_attachment_id

    @property
    def log_group_id(self) -> str:
        return self._log_group_id

    @property
    def cluster(self) -> runtime_graalos_config.Cluster:
        """Target GraalOS service cluster."""
        return self._cluster

    def __str__(self) -> str:
        return f".service(region = {self.region}, compartment_id = {self.compartment_id}, cluster = {self.cluster})"

class GraalOSConfig:
    """Configuration of the GraalOS corresponding to the [graalos] section"""
    def __init__(
        self,
        stack: Optional[GraalOSStackConfig],
        service: Optional[GraalOSServiceConfig],
        private_apps_per_attachment_limit: Optional[int],
        remote_oci_worker_host: Optional[str],
        default_auth: Optional[str],
        root_nameservers: Optional[list[str]],
        corporate_nameservers: Optional[list[str]],
        zone: Optional[str],
    ):
        self._stack: Optional[GraalOSStackConfig] = stack
        self._service: Optional[GraalOSServiceConfig] = service
        self._private_apps_per_attachment_limit: Optional[int] = private_apps_per_attachment_limit
        self._remote_oci_worker_host: Optional[str] = remote_oci_worker_host
        self._default_auth: Optional[str] = default_auth
        self._root_nameservers: Optional[list[str]] = root_nameservers
        self._corporate_nameservers: Optional[list[str]] = corporate_nameservers
        self._zone: Optional[str] = zone

    @property
    def stack(self) -> Optional[GraalOSStackConfig]:
        """GraalOS stack config; if it is specified"""
        return self._stack

    @property
    def service(self) -> Optional[GraalOSServiceConfig]:
        """GraalOS service config; if it is specified"""
        return self._service

    @property
    def private_apps_per_attachment_limit(self) -> Optional[int]:
        """Maximum number of private-networking service apps per network attachment."""
        return self._private_apps_per_attachment_limit

    @property
    def remote_oci_worker_host(self) -> Optional[str]:
        """Host used for remote OCI CLI execution."""
        return self._remote_oci_worker_host

    @property
    def default_auth(self) -> Optional[str]:
        """Default auth mode used when CLI does not override it."""
        return self._default_auth

    @property
    def root_nameservers(self) -> Optional[list[str]]:
        """Root nameservers used to discover GraalOS service zone nameservers."""
        return self._root_nameservers

    @property
    def corporate_nameservers(self) -> Optional[list[str]]:
        """Corporate nameservers used to discover GraalOS service zone nameservers."""
        return self._corporate_nameservers

    @property
    def zone(self) -> Optional[str]:
        """GraalOS DNS zone used for service app resolution."""
        return self._zone

    def __str__(self) -> str:
        configs = [str(c) for c in (self.stack, self.service) if c is not None]
        return f"GraalOSConfig({', '.join(configs)})"


class HarnessConfig:
    """
    :ivar _apps_creation_concurrency: number of concurrent oci graalos-application create requests (the higher number might lead to failures)
    :ivar _apps_creation_retries: how many times we will retry to create application, if it fails
    :ivar _apps_creation_timeout: how long (in seconds) should we wait before timeout on waiting for application to be created
    :ivar _apps_deletion_concurrency: number of concurrent oci graalos-application delete requests (higher number might lead to leaking unreaped applications)
    :ivar _app_creation_oci_cli_raw: retained for compatibility with the shared scenario schema
    :ivar _instances_creation_concurrency: retained for compatibility with the shared scenario schema
    :ivar _keg_deployment_timeout: time in seconds, how long we wait for all kegs to be deployed before failing with TimeoutError
    :ivar _redirect_nginx_logs: Redirect nginx logs from stdout to a separate file
    :ivar _redirect_tinyinit_logs: Redirect tinyinit logs from stdout to a separate file
    :ivar _autoscaling_policy: optional dictionary that sets the autoscaling options. It should be in one of the following formats:
      {"mode": "[CPU,MEMORY]", "scaleInThreshold": <INT>, "scaleOutThreshold": <INT> }
        - this autoscaling sets the scaling based on CPU or Memory exceeding certain threshold
      {"mode": "CUSTOM", "scaleInQuery": <MQL>, "scaleOutQuery": <MQL>}
        - this autoscaling allows more precisely tune the scaling based on the Metric Query Language
    :ivar _instance_configuration_shape: requested shape for the new configuration, used for creating new autoscaling policy
    :ivar _boat_profile: profile for boat user that is used for certain oci commands
    :ivar _scenario_timeout: timeout for the whole scenario
    """
    def __init__(self, apps_creation_concurrency: int, apps_creation_retries: int, apps_creation_timeout: int, apps_deletion_concurrency:int, app_creation_oci_cli_raw: bool, instances_creation_concurrency: int, keg_deployment_timeout: int, redirect_nginx_logs: bool, redirect_tinyinit_logs: bool, autoscaling_policy: Optional[dict[str, Any]], instance_configuration_shape: Optional[str], boat_profile: Optional[str], scenario_timeout: Optional[int]):
        self._apps_creation_concurrency = apps_creation_concurrency
        self._apps_creation_retries = apps_creation_retries
        self._apps_creation_timeout = apps_creation_timeout
        self._apps_deletion_concurrency = apps_deletion_concurrency
        self._app_creation_oci_cli_raw = app_creation_oci_cli_raw
        self._instances_creation_concurrency = instances_creation_concurrency
        self._keg_deployment_timeout = keg_deployment_timeout
        self._redirect_nginx_logs = redirect_nginx_logs
        self._redirect_tinyinit_logs = redirect_tinyinit_logs
        self._autoscaling_policy = autoscaling_policy
        self._instance_configuration_shape = instance_configuration_shape
        self._boat_profile = boat_profile
        self._scenario_timeout = int(scenario_timeout) if scenario_timeout else scenario_timeout

    @property
    def apps_creation_concurrency(self) -> int:
        return self._apps_creation_concurrency

    @property
    def apps_creation_retries(self) -> int:
        return self._apps_creation_retries

    @property
    def apps_creation_timeout(self) -> int:
        return self._apps_creation_timeout

    @property
    def apps_deletion_concurrency(self) -> int:
        return self._apps_deletion_concurrency

    @property
    def app_creation_oci_cli_raw(self) -> bool:
        return self._app_creation_oci_cli_raw

    @property
    def instances_creation_concurrency(self) -> int:
        return self._instances_creation_concurrency

    @property
    def keg_deployment_timeout(self) -> int:
        return self._keg_deployment_timeout

    @property
    def redirect_nginx_logs(self) -> bool:
        return self._redirect_nginx_logs

    @property
    def redirect_tinyinit_logs(self) -> bool:
        return self._redirect_tinyinit_logs

    @property
    def autoscaling_policy(self) -> Optional[dict[str, Any]]:
        return self._autoscaling_policy

    @property
    def instance_configuration_shape(self) -> Optional[str]:
        return self._instance_configuration_shape

    @property
    def boat_profile(self) -> Optional[str]:
        return self._boat_profile

    @property
    def scenario_timeout(self) -> Optional[int]:
        return self._scenario_timeout

    def __str__(self) -> str:
        extra = ""
        if self._autoscaling_policy != None:
            extra += f", autoscaling_policy = {str(self._autoscaling_policy)}"
        if self._boat_profile != None:
            extra += f", boat_profile = {self._boat_profile}"

        return (f"Harness(apps_creation_concurrency = {self.apps_creation_concurrency}, apps_creation_retries = {self.apps_creation_retries}, apps_creation_timeout = {self.apps_creation_timeout},"
                f" apps_deletion_concurrency = {self.apps_deletion_concurrency}, app_creation_oci_cli_raw = {self.app_creation_oci_cli_raw},"
                f" instances_creation_concurrency = {self.instances_creation_concurrency}, keg_deployment_timeout = {self.keg_deployment_timeout},"
                f" redirect_nginx_logs = {self.redirect_nginx_logs}, redirect_tinyinit_logs = {self.redirect_tinyinit_logs},"
                f" instance_configuration_shape = {self.instance_configuration_shape},"
                f" scenario_timeout = {self.scenario_timeout}"
                f"{extra}"
        )


class ScenarioConfig:
    """
    :ivar _id: id of the scenario stored as scenario.id in bench server
    :ivar _name: name of the scenario
    :ivar _group: group of the scenario
    :ivar _deployment: how the apps are deployed in the scenario
    :ivar _description: brief description of the scenario
    :ivar _metric_groups: metric groups for scenario-level metrics
    :ivar _results_processings: list of user defined postprocessing steps mapping name to executable python code
    :ivar _apps: list of application configuration
    :ivar _generators: list of generator configuration
    :ivar _parameters: list of user-modifiable environment parameters
    :ivar _agents: list of agent configuration
    :ivar _harness: configuration related to the harness itself
    :ivar _graalos: configuration of the GraalOS service (stack, clusters, etc.)
    """
    def __init__(self, 
            name: str, 
            description: str, 
            group: str, 
            deployment: str,
            metric_groups: List[MetricGroupConfig],
            results_processings: List[UserPostprocessingConfig], 
            apps: List[AppConfig],
            generators: List[GeneratorConfig], 
            parameters: dict, 
            agents: List[AgentConfig], 
            harness: HarnessConfig, 
            graalos: GraalOSConfig
    ):
        self._id = f"scenario-{str(uuid.uuid4())[:8]}"
        self._name = name
        self._group = group
        self._deployment = deployment
        self._description = description
        self._metric_groups = metric_groups
        self._results_processings = results_processings
        self._apps = apps
        self._generators = generators 
        self._parameters = parameters
        self._agents = agents
        self._harness = harness
        self._graalos = graalos

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def group(self) -> str:
        return self._group

    @property
    def deployment(self) -> str:
        return self._deployment

    @property
    def results_processings(self) -> List[UserPostprocessingConfig]:
        return self._results_processings

    @property
    def metric_groups(self) -> List[MetricGroupConfig]:
        return self._metric_groups

    def all_metric_groups(self, enabled_only: bool = False) -> set[MetricGroupConfig]:
        """Returns all metric groups in the scenario
        
        :param enabled_only: only groups that are enabled are returned
        """
        all_groups = (
            list(self.metric_groups)
            + [group for app in self.apps for group in app.metric_groups]
            + [group for generator in self.generators for group in generator.metric_groups]
            + [group for agent in self.agents for group in agent.metric_groups]
        )
        return set([group for group in all_groups if group.enabled]) if enabled_only else set(all_groups)

    @property
    def apps(self) -> List[AppConfig]:
        return self._apps

    @property
    def non_service_apps(self) -> List[AppConfig]:
        return [app for app in self._apps if app.deployment != "service"]

    @property
    def graalos_service_apps(self) -> List[AppConfig]:
        return [app for app in self._apps if app.deployment == "service"]

    @property
    def generators(self) -> List[GeneratorConfig]:
        return self._generators

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def agents(self) -> List[AgentConfig]:
        return self._agents

    @property
    def harness(self) -> HarnessConfig:
        return self._harness

    @property
    def graalos(self) -> GraalOSConfig:
        return self._graalos

    def __str__(self) -> str:
        newline = "\n"
        processings_str = f"## Custom Processing{newline}{newline.join(f'- {m}' for m in self.results_processings)}{newline}" if self.results_processings else ""
        sorted_generators = self.generators
        if sorted_generators:
            sorted_generators = sorted(sorted_generators, key=lambda t: t.delay)
        generators_str = f"## Generators{newline}{newline.join(f'- {generator}' for generator in sorted_generators)}{newline}" if sorted_generators else ""
        agents_str = f"## Agents{newline}{newline.join(f'- {agent}' for agent in self.agents)}{newline}" if self.agents else ""
        return f"""
::: Scenario {self.name} :::
Description: {self.description}
Group: {self.group if self.group else "<no group>"}
Parameters: {", ".join([f"{k}={v}" for k,v in self.parameters.items()])}
{processings_str}## Apps
{newline.join("- " + str(app) for app in self.apps)}
{generators_str}{agents_str}## Harness
{self.harness}
## GraalOS
{self.graalos}"""

def parse_cli_parameters(parameters: list[str]) -> dict[str, Any]:
    parameters_dict: dict[str, Any] = {}
    for param_str in parameters:
        if '=' not in param_str:
            raise ValueError(f"Parameter override must be in format PARAM=VALUE, got: {param_str}")

        parameter, value = param_str.split('=', 1)
        parsed_value: Any = value

        # Guess the type
        if value.lower() == 'true':
            parsed_value = True
        elif value.lower() == 'false':
            parsed_value = False
        else:
            try:
                parsed_value = int(value)
            except ValueError:
                try:
                    parsed_value = float(value)
                except ValueError:
                    # Keep as string if not a number
                    pass
        parameters_dict[parameter] = parsed_value
    return parameters_dict


def error(message: str) -> NoReturn:
    raise RuntimeError(message)


def check(value: Any, cond: ValidationCond, location: str) -> None:
    if cond is True:
        pass
    elif cond is int:
        if not isinstance(value, int):
            error("Expected int instead of '" + str(value) + "'. At " + location)
    elif cond is float:
        if not isinstance(value, float) and not isinstance(value, int):
            error("Expected float instead of '" + str(value) + "'. At " + location)
    elif cond is str:
        if not isinstance(value, str):
            error("Expected string instead of '" + str(value) + "'. At " + location)
    elif cond is list:
        if not isinstance(value, list):
            error("Expected list instead of '" + str(value) + "'. At " + location)
    elif cond is dict:
        if not isinstance(value, dict):
            error("Expected dictionary instead of '" + str(value) + "'. At " + location)
    elif cond is bool:
        if not isinstance(value, bool):
            error("Expected bool instead of '" + str(value) + "'. At " + location)
    elif callable(cond):
        if not cond(value):
            error("Invalid value '" + str(value) + "'. At " + location)
    elif cond is None:
        pass
    else:
        error(f"Unknown condition: {cond}")

def get_default_ocpus(memory: int) -> float:
    """Maps default OCPUs for given memory

    If the default cannot be established based on memory, we use the default of 0.5
    which is the service default
    """
    return {
        256: 0.125,
        512: 0.25,
        1024: 0.5,
        2048: 1
    }.get(memory, 0.5)


def try_eval(obj: object, env: Optional[dict[str, Any]]=None) -> Any:
    """Takes the object and tries to evaluate it as an expression in case it is explicitly requested.

    The explicit evaluation is requested as {'eval': "expression"}

    :param obj: object that will be evaluated
    :return: original object or evaluated expression
    """
    if isinstance(obj, Mapping) and 'eval' in obj:
        return eval_string_expr(obj['eval'], env=env)
    return obj

def get_field(obj: Any, field: str, env: Optional[dict[str, Any]]=None, default: Any = None) -> Any:
    """Retrieves the value of the field from the object

    If the field does not exist in the object, the default will be returned.
    If the object is in form of {eval: expr}, then the expression will be explicitly evaluated.

    :param obj: any object
    :param field: name of the extracted field
    :param env: optional key:value dictionary with environment that is passed for eval
    :param default: optional default value in case the field is missing in the object
    """
    value = try_eval(obj.get(field), env)
    if value is None:
        return default
    return value


def get_opt_enum(obj: dict, field: str, enum_type: Type[Enum], default: Enum, value_fn: Callable[[str], str] = lambda x: x, env: Optional[dict[str, Any]]=None) -> Enum:
    """Returns the enum of enum_type constructed from obj[field] or default if the field is not present in the obj

    :param obj: any object
    :param field: name of the extracted field
    :param value_fn: function that is applied to the value retried from the object, before being converted to enum
    :param env: optional key:value dictionary with environment that is passed for eval
    :param default: optional default value in case the field is missing in the object
    """
    value = get_field(obj, field, env=env)
    if value is None:
        return default
    choices = enum_type.__members__.keys()
    value = value_fn(value)
    if value not in choices:
        error(f"Invalid enum type. Got '{value}'. Must be one of {choices}")
    return enum_type[value]

def get_opt_dict(obj: object, field: str, env: Optional[dict[str, Any]]=None) -> dict[str, Any]:
    """Gets optional dict from the object or returns empty dict

    :param obj: any object
    :param field: name of the extracted field
    :param env: optional key:value dictionary with environment that is passed for eval
    """
    value = get_field(obj, field, default="{}", env=env)
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as err:
        error(f"Could not decode field '{field}' with value '{value}': {err}")

def get_opt(
    obj: object,
    field: str,
    cond: ValidationCond = True,
    default: Optional[Any] = None,
    env: Optional[dict[str, Any]] = None,
) -> Any:
    """Gets optional field from the object and returns default if missing

    :param obj: any object
    :param field: name of the extracted field
    :param cond: condition that the retrieved value must satisfy
    :param default: optional default value in case the field is missing in the object
    :param env: optional key:value dictionary with environment that is passed for eval
    """
    value = get_field(obj, field, env=env, default=default)
    check(value, cond, "field '" + field + "' in: " + str(obj))
    return value

def get(
    obj: object,
    field: str,
    cond: ValidationCond,
    env: Optional[dict[str, Any]] = None,
) -> Any:
    """Retrieves the field from the object and checks if it satisfies the passed condition

    :param obj: any object
    :param field: name of the extracted field
    :param cond: condition that the retrieved value must satisfy
    :param env: optional key:value dictionary with environment that is passed for eval
    """
    value = get_field(obj, field, env=env)
    if value is None:
        error(f"Missing field '{field}' in: {str(obj)}")
    check(value, cond, f"field '{field}' in: {str(obj)}")
    return value


def get_binary(binary: dict[str, Any], app_deployment: str, env: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if app_deployment == "graalos-stack":
        # Create apps on a GraalOS marketplace stack
        domain_prefix = get(binary, "domain_prefix", cond=str, env=env)
        container_image = get(binary, "container_image", cond=str, env=env)
        container_image_id = get(binary, "container_image_id", cond=str, env=env)
        entrypoint = get(binary, "entrypoint", cond=list, env=env)
        environment = get_opt_dict(binary, "environment", env=env)
        guest_port = get_opt(binary, "guest_port", cond=lambda value: value is None or isinstance(value, int), default=None, env=env)
        if not all(isinstance(arg, str) for arg in entrypoint):
            error(f"Field 'entrypoint' must be a list of strings in: {binary}")
        return {"domain_prefix": domain_prefix, "container_image": container_image, "container_image_id": container_image_id, "entrypoint": entrypoint, "environment": environment, "guest_port": guest_port}
    elif "artifact_id" in binary:
        # Create apps using a single artifact ID.
        artifact_id = get(binary, "artifact_id", cond=str, env=env)
        environment = get_opt_dict(binary, "environment", env=env)
        network_mode = get_opt_enum(binary, "network", NetworkType, default=NetworkType.PUBLIC, value_fn=str.upper, env=env)
        subnet_id = get_opt(binary, "subnet_id", cond=lambda value: value is None or isinstance(value, str), default=None, env=env)
        return {"artifact_id": artifact_id, "environment": environment, "network_mode": network_mode, "subnet_id": subnet_id}
    elif "artifact_id_file" in binary:
        # Create apps using a path to a file that contains artifact IDs.
        artifact_id_file = get(binary, "artifact_id_file", cond=str, env=env)
        environment = get_opt_dict(binary, "environment", env=env)
        network_mode = get_opt_enum(binary, "network", NetworkType, default=NetworkType.PUBLIC, value_fn=str.upper, env=env)
        subnet_id = get_opt(binary, "subnet_id", cond=lambda value: value is None or isinstance(value, str), default=None, env=env)
        return {"artifact_id_file": pathlib.Path(artifact_id_file), "environment": environment, "network_mode": network_mode, "subnet_id": subnet_id}
    elif "binary_command" in binary:
        if app_deployment not in LOCAL_APP_DEPLOYMENT_TYPES:
            raise ValueError(f"'binary_command' is supported only for the app.deployment in {LOCAL_APP_DEPLOYMENT_TYPES}; got: {app_deployment}")
        environment = get_opt_dict(binary, "environment", env=env)
        # keepalive is number of connections kept; keepalive_timeout is timeout in seconds/hours/minutes as string
        return {"binary_command": (get(binary, "binary_command", str, env=env)),
                       "base_url": (get_opt(binary, "base_url", str, default="127.0.0.1", env=env)),
                       "environment": environment,
                       "unique_copy": (get_opt(binary, "unique_copy", bool, default=False, env=env)),
                       "nginx_keepalive": (get_opt(binary, "nginx_keepalive", cond=int, default=5, env=env)),
                       "nginx_keepalive_timeout": (get_opt(binary, "nginx_keepalive_timeout", str, default="5s", env=env)),
                       "nginx_keepalive_requests": (get_opt(binary, "nginx_keepalive_requests", cond=int, default=10_000_000, env=env)),
                       EXTRA_GRAALHOST_DIR_MOUNTS: (extra_graalhost_dir_mounts(binary, env, app_deployment)),
                       GRAALHOST_CONFIG_FILE : (graalhost_config_file(binary, env, app_deployment)),
                       FIRECRACKER_KERNEL_IMAGE: (firecracker_kernel_image(binary, env, app_deployment)),
                       FIRECRACKER_MEM_MIB: (firecracker_mem_mib(binary, env, app_deployment)),
                       FIRECRACKER_VCPU_COUNT: (firecracker_vcpu_count(binary, env, app_deployment)),
                       FIRECRACKER_ROOTFS_SIZE_MIB: (firecracker_rootfs_size_mib(binary, env, app_deployment))}
    elif "keg_id" in binary:
        # Create apps using a preinstalled file that is loaded as a keg (lightweight virtualization)
        keg_id = get(binary, "keg_id", cond=str, env=env)
        keg_domain = get(binary, "keg_domain", cond=str, env=env)
        binary_path = get(binary, "binary_path", cond=str, env=env)
        launch_arguments = get_opt(binary, "launch_arguments", cond=str, default="", env=env)
        if app_deployment != "dataplane":
            raise ValueError(f"kegs are supported only in 'app.deployment = \"dataplane\"'; got {app_deployment} instead")
        return {"keg_id": keg_id, "binary_path": binary_path, "keg_domain": keg_domain, "launch_arguments": launch_arguments}
    elif "base_url" in binary:
        # Registers an already existing app accessible at the given base url
        base_url = get(binary, "base_url", cond=str, env=env)
        if base_url.startswith("http://") or base_url.startswith("https://"):
            raise ValueError("Protocol - http(s):// - must not be part of the binary config base url! Set 'app.protocol' instead.")
        if "/" in base_url:
            raise ValueError("Slashes (/) must not be part of the binary config base url! Set 'app.endpoints' instead.")
        if ":" in base_url:
            raise ValueError("Port number must not be part of the binary config base url! Set 'app.port' instead.")
        return {"base_url": base_url}
    else:
        error("Invalid 'binary' section.")


def graalhost_config_file(binary: dict[str, Any], env: Optional[dict[str, Any]], app_deployment: str) -> str | None:
    if GRAALHOST_CONFIG_FILE not in binary:
        return None
    value = get(binary, GRAALHOST_CONFIG_FILE, cond=str, env=env)
    if app_deployment != "nginx-tinyinit-graalhost" :
        error(f"Cannot use \"{GRAALHOST_CONFIG_FILE}\" when deployment is not \"nginx-tinyinit-graalhost\"")
    return value

def extra_graalhost_dir_mounts(binary: dict[str, Any], env: Optional[dict[str, Any]], _: str) -> list[str]:
    value = get(binary, "extra_graalhost_dir_mounts", cond=str, env=env) if "extra_graalhost_dir_mounts" in binary else None
    if value is not None:
        value = value.split(",")
    else:
        value = []
    value = list(filter(None, value))  # Filter out empty strings
    return value


def firecracker_kernel_image(binary: dict, env: Optional[dict[str, Any]], app_deployment: str) -> str|None:
    """Returns the Firecracker kernel image path for Firecracker local deployments."""
    if FIRECRACKER_KERNEL_IMAGE not in binary:
        if app_deployment == "nginx-tinyinit-firecracker":
            error(f"Missing required '{FIRECRACKER_KERNEL_IMAGE}' for deployment '{app_deployment}'")
        return None
    value = get(binary, FIRECRACKER_KERNEL_IMAGE, cond=str, env=env)
    return value


def firecracker_mem_mib(binary: dict, env: Optional[dict[str, Any]], app_deployment: str) -> int:
    """Returns the configured Firecracker guest memory size in MiB."""
    return get_opt(binary, FIRECRACKER_MEM_MIB, cond=int, default=512, env=env)


def firecracker_vcpu_count(binary: dict, env: Optional[dict[str, Any]], app_deployment: str) -> int:
    """Returns the configured Firecracker guest vCPU count."""
    return get_opt(binary, FIRECRACKER_VCPU_COUNT, cond=int, default=1, env=env)


def firecracker_rootfs_size_mib(binary: dict, env: Optional[dict[str, Any]], app_deployment: str) -> int|None:
    """Returns the optional size override for the generated Firecracker rootfs in MiB."""
    value = get_opt(binary, FIRECRACKER_ROOTFS_SIZE_MIB, cond=int, default=0, env=env)
    if value == 0:
        return None
    return value


def get_harness(harness_dict: dict[str, Any], env: Optional[dict[str, Any]] = None) -> HarnessConfig:
    for key in harness_dict.keys():
        if key not in ["apps_creation_concurrency", "apps_creation_retries", "apps_creation_timeout", "apps_deletion_concurrency", "app_creation_oci_cli_raw", "instances_creation_concurrency", "keg_deployment_timeout", "redirect_nginx_logs", "redirect_tinyinit_logs", "autoscaling_policy", "instance_configuration_shape", "boat_profile", "scenario_timeout"]:
            error(f"Invalid 'harness' configuration: {key}")
    apps_creation_concurrency = get_opt(harness_dict, "apps_creation_concurrency", cond=int, default=10, env=env)
    apps_creation_retries = get_opt(harness_dict, "apps_creation_retries", default=1, cond=int, env=env)
    if apps_creation_retries < 1:
        error("'apps_creation_retries' must be greater or equal than 1.")
    apps_creation_timeout = get_opt(harness_dict, "apps_creation_timeout", cond=int, default=1200)
    apps_deletion_concurrency = get_opt(harness_dict, "apps_deletion_concurrency", default=20, cond=int, env=env)
    app_creation_oci_cli_raw = get_opt(harness_dict, "app_creation_oci_cli_raw", default=False, cond=bool, env=env)
    instances_creation_concurrency = get_opt(harness_dict, "instances_creation_concurrency", default=10, cond=int, env=env)
    keg_deployment_timeout = get_opt(harness_dict, "keg_deployment_timeout", default=60, cond=int, env=env)
    redirect_nginx_logs = get_opt(harness_dict, "redirect_nginx_logs", default=False, cond=bool, env=env)
    redirect_tinyinit_logs = get_opt(harness_dict, "redirect_tinyinit_logs", default=False, cond=bool, env=env)
    autoscaling_policy = get_opt(harness_dict, "autoscaling_policy", default="", cond=str, env=env)
    boat_profile = get_opt(harness_dict, "boat_profile", env=env)
    scenario_timeout = get_opt(harness_dict, "scenario_timeout", env=env)
    if scenario_timeout:
        check(scenario_timeout, int, f"field '{scenario_timeout}' in: {harness_dict}")
    instance_configuration_shape = get_opt(harness_dict, "instance_configuration_shape", default="small-burstable_1_8", cond=str, env=env)
    try:
        autoscaling_policy = json.loads(autoscaling_policy) if autoscaling_policy != "" else None
    except json.decoder.JSONDecodeError as jde:
        logging.die(f"'autoscaling_policy' must be a valid JSON: {str(jde)}; got instead: {autoscaling_policy}")
    return HarnessConfig(apps_creation_concurrency, apps_creation_retries, apps_creation_timeout, apps_deletion_concurrency, app_creation_oci_cli_raw, instances_creation_concurrency, keg_deployment_timeout, redirect_nginx_logs, redirect_tinyinit_logs, autoscaling_policy, instance_configuration_shape, boat_profile, scenario_timeout)


def get_graalos(graalos_dict: dict[str, Any], env: Optional[dict[str, Any]] = None) -> GraalOSConfig:
    """Parses the [graalos] section of the configuration
    
    :param graalos_dict: [graalos] section from the parsed TOML
    """
    for key in graalos_dict.keys():
        if key not in [
            "stack",
            "service",
            "private_apps_per_attachment_limit",
            "remote_oci_worker_host",
            "default_auth",
            "root_nameservers",
            "corporate_nameservers",
            "zone",
        ]:
            error(f"Invalid 'graalos' configuration: {key}")
    stack = get_opt(graalos_dict, "stack")
    if stack is not None:
        for key in stack.keys():
            if key not in [
                "lifecycle",
                "stack_version",
                "marketplace_listing_id",
                "marketplace_region",
                "resource_manager_endpoint",
                "domain",
                "nameservers",
                "ip",
                "specification",
            ]:
                error(f"Invalid 'graalos.stack' configuration: {key}")
        stack_lifecycle = get(stack, "lifecycle", str, env=env)
        assert stack_lifecycle in ("persistent", "disposable")
        stack_version = get(stack, "stack_version", str, env=env)
        marketplace_listing_id = get(stack, "marketplace_listing_id", str, env=env)
        marketplace_region = get(stack, "marketplace_region", str, env=env)
        resource_manager_endpoint = get(stack, "resource_manager_endpoint", str, env=env)
        domain = get(stack, "domain", str, env=env)
        nameservers = get(stack, "nameservers", list, env=env)
        if not all(isinstance(nameserver, str) for nameserver in nameservers):
            error(f"Field 'nameservers' must be a list of strings in: {stack}")
        ip = get_opt(stack, "ip", env=env)
        spec = get_opt(stack, "specification", env=env)
        # Evaluate spec
        for key in (spec or {}).keys():
            spec[key] = get(spec, key, cond=True, env=env)
        if stack_lifecycle == "persistent" and ip is None:
            raise ValueError("'persistent' GraalOS stack requires 'ip' in its section")
        if stack_lifecycle == "disposable" and spec is None:
            raise ValueError("'disposable' GraalOS stack requires 'specification' in its section")
        if stack_lifecycle == "disposable":
            # Required marketplace fields for disposable stack source
            #  - also evaluates the field
            spec["stack_package_id"] = get(spec, "stack_package_id", str, env=env)
        stack = GraalOSStackConfig(
            stack_lifecycle,
            stack_version,
            marketplace_listing_id,
            marketplace_region,
            resource_manager_endpoint,
            domain,
            nameservers,
            ip,
            spec,
        )

    service = get_opt(graalos_dict, "service")
    if service is not None:
        for key in service.keys():
            if key not in [
                "tenancy",
                "region",
                "availability_domain",
                "compartment_id",
                "subnet_id",
                "fault_domain",
                "registry_domain",
                "artifact_repository",
                "network_attachment_id",
                "log_group_id",
                "cluster",
            ]:
                error(f"Invalid 'graalos.service' configuration: {key}")
        cluster_config = get(service, "cluster", dict, env=env)
        for key in cluster_config.keys():
            if key not in ["name", "endpoint", "environment", "cluster_id", "virtual_cluster_id", "instance_configuration_compartment_id"]:
                error(f"Invalid 'graalos.service.cluster' configuration: {key}")
        cluster = runtime_graalos_config.Cluster(
            get(cluster_config, "name", str, env=env),
            runtime_graalos_config.GraalOSEnvironment(get(cluster_config, "environment", str, env=env)),
            get(cluster_config, "endpoint", str, env=env),
            get(cluster_config, "cluster_id", str, env=env),
            get(cluster_config, "virtual_cluster_id", str, env=env),
            get_opt(cluster_config, "instance_configuration_compartment_id", str, env=env),
        )
        service = GraalOSServiceConfig(
            get(service, "tenancy", str, env=env),
            get(service, "region", str, env=env),
            get(service, "availability_domain", str, env=env),
            get(service, "compartment_id", str, env=env),
            get(service, "subnet_id", str, env=env),
            get(service, "fault_domain", str, env=env),
            get(service, "registry_domain", str, env=env),
            get(service, "artifact_repository", str, env=env),
            get(service, "network_attachment_id", str, env=env),
            get(service, "log_group_id", str, env=env),
            cluster,
        )
    default_auth = get(graalos_dict, "default_auth", str, env=env) if "default_auth" in graalos_dict else None
    if default_auth is not None and default_auth not in ("instance_principal", "security_token"):
        error(f"Invalid 'graalos.default_auth' configuration: {default_auth}")

    return GraalOSConfig(
        stack,
        service,
        get(graalos_dict, "private_apps_per_attachment_limit", int, env=env) if "private_apps_per_attachment_limit" in graalos_dict else None,
        get(graalos_dict, "remote_oci_worker_host", str, env=env) if "remote_oci_worker_host" in graalos_dict else None,
        default_auth,
        get(graalos_dict, "root_nameservers", list, env=env) if "root_nameservers" in graalos_dict else None,
        get(graalos_dict, "corporate_nameservers", list, env=env) if "corporate_nameservers" in graalos_dict else None,
        get(graalos_dict, "zone", str, env=env) if "zone" in graalos_dict else None,
    )

def flatten_dict(d: Mapping[Any, Any], sep: str = ".", prefix: str = "") -> dict[str, Any]:
    """
    Helper that flattens nested dictionaries by joining keys with `sep`. Non-dict values (lists, scalars, etc.) are kept as-is.

    Flattens {'a': {'b': [1, 2]}} to {'a.b': [1, 2]}

    :param d: input dictionary that will be flattened
    :param sep: separator of the keys (by default .)
    :param prefix: prefix for the keys (used for nested calls)
    :return: flattened dictionary
    """
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{sep}{k}" if prefix else str(k)
        if isinstance(v, Mapping):
            out.update(flatten_dict(v, sep=sep, prefix=key))
        else:
            out[key] = v
    return out

def get_metrics_group_filter(metrics_group: dict[str, Any], env: Optional[dict[str, Any]]) -> MetricGroupFilterConfig:
    """Parses the [*.metrics.filter] group

    :param metrics_group: parsed single metrics group
    :param env: optional key:value dictionary with environment that is passed for eval
    :return: parsed filter
    """
    metric_filter = get(metrics_group, "filter", cond=dict, env=env)
    filterby = get(metric_filter, "filterby", cond=str, env=env)
    postprocess = get_metrics_group_postprocess(metric_filter, env=env)
    if filterby == "fields":
        fields = flatten_dict({key: value for key, value in metric_filter.items() if key not in ("filterby", "sampler", "filter")})
        return MetricGroupFilterByFieldsConfig(fields=fields, postprocess=postprocess)
    elif filterby == "quantile-range":
        groupby = get_opt(metric_filter, "groupby", env=env, cond=lambda groupby: groupby is None or (isinstance(groupby, list) and all(isinstance(metric, str) for metric in groupby)))
        ranges = get(metric_filter, "ranges", env=env, cond=lambda ranges: isinstance(ranges, str) and bool(RANGES_MATCHER.match(ranges)))
        return MetricGroupFilterByQuantileRanges(ranges=ranges, groupby=groupby, postprocess=postprocess)
    else:
        raise ValueError(f"Unsupported filter '{filterby}' in metric group")

def get_metrics_group_sampler(metrics_group: dict[str, Any], env: Optional[dict[str, Any]]) -> MetricGroupSamplerConfig:
    """Parses the [.*.metrics.sampler] group
    
    :param metrics_group: parsed single metrics group
    :param env: optional key:value dictionary with environment that is passed for eval
    :return: parsed sampler
    """
    metric_sampler = get(metrics_group, "sampler", cond=dict, env=env)
    domain = get(metric_sampler, "domain", cond=str, env=env)
    postprocess = get_metrics_group_postprocess(metric_sampler, env=env)
    if domain == "discrete":
        sampleby = get_opt(metric_sampler, "sampleby", cond=lambda sampleby: sampleby is None or isinstance(sampleby, str), env=env)
        groupby = get_opt(metric_sampler, "groupby", env=env, cond=lambda groupby: groupby is None or (isinstance(groupby, list) and all(isinstance(metric, str) for metric in groupby)))
        rate = get(metric_sampler, "rate", cond=int, env=env)
        return MetricGroupDiscreteSampler(sampleby=sampleby, groupby=groupby, rate=rate, postprocess=postprocess)
    else:
        raise ValueError(f"Unsupported sampling domain '{domain}' in metric group")

def get_metrics_group_postprocess(metrics_group: dict[str, Any], env: Optional[dict[str, Any]]) -> MetricGroupFilterConfig | MetricGroupSamplerConfig | None:
    """Parses the [.*.metrics.(sampler|filter)] group

    Each group can have only one sampler or filter: you can chain the filters however:
      - this explicitly states the order of the application;
      - for multiple distinct filters, use different groups.

    :param metrics_group: parsed single metrics group
    :param env: optional key:value dictionary with environment that is passed for eval
    :return: parsed sampler/filter or None, if there is no next postprocessing group
    """
    has_filter = 'filter' in metrics_group
    has_sampler = 'sampler' in metrics_group

    if has_filter and has_sampler:
        raise ValueError(f"Cannot specify both distinct sampler and filter for: {metrics_group}")
    if has_filter:
        return get_metrics_group_filter(metrics_group, env)
    if has_sampler:
        return get_metrics_group_sampler(metrics_group, env)
    return None

def get_metrics_group(metrics_group: dict[str, Any], env: Optional[dict[str, Any]] = None) -> MetricGroupConfig:
    """Parses single group from [[*.metrics]] section

    :param metrics_group: parsed single metrics group
    :param env: optional key:value dictionary with environment that is passed for eval
    :return: parsed metrics group
    """
    group = get_opt(metrics_group, "group", env=env, cond=str, default="")
    collect = get(metrics_group, "collect", env=env, cond=lambda collect: isinstance(collect, list) and all(isinstance(metric, str) for metric in collect))
    enabled = get_opt(metrics_group, "enabled", env=env, cond=bool, default=True)
    fail_on_no_metrics = get_opt(metrics_group, "fail_on_no_metrics", env=env, cond=bool, default=True)
    postprocess = get_metrics_group_postprocess(metrics_group, env=env)
    return MetricGroupConfig(
        collect=collect,
        group=group,
        enabled=enabled,
        fail_on_no_metrics=fail_on_no_metrics,
        postprocess=postprocess,
    )

def get_metrics_group_list(specification: dict[str, Any], env: Optional[dict[str, Any]] = None) -> list[MetricGroupConfig]:
    """Parses the [[.*metrics]] sections

    :param specification: parsed scenario
    :param env: optional key:value dictionary with environment that is passed for eval
    :return: list of parsed metric groups
    """
    if 'metrics' in specification:
        metric_list = get(specification, "metrics", cond=list, env=env)
        return [get_metrics_group(metric_group, env) for metric_group in metric_list]
    return []

def get_generator_expects(
    generator: dict[str, Any], env: Optional[dict[str, Any]] = None
) -> ExpectConfig:
    """Parses the [generator.expects] section of the TOML file

    :param generator: parsed TOML dictionary for [generator.expects] section
    :param env: optional key:value dictionary with environment that is passed for eval
    """
    if "expect" in generator:
        expect = get(generator, "expect", dict, env=env)
        code = get_opt(expect, "code", cond=None, default=None, env=env)
        body = get_opt(expect, "body", cond=None, default=None, env=env)
        assert code is not None or body is not None, "Either 'code' or 'body' has to be set for the 'generator.expect'"
        return ExpectConfig(code, body)
    # We expect nothing
    return ExpectConfig(None, None)


def get_generator_method(generator: dict[str, Any], env: Optional[dict[str, Any]] = None) -> GeneratorMethodConfig:
    method = get(generator, "method", cond=dict, env=env)
    method_type = get(method, "type", cond=str, env=env)
    iterations = get_opt(method, "iterations", cond=int, env=env, default=1)
    assert iterations >= 0, f"Number of iterations must be positive. Got '{iterations}'"
    if method_type == "throughput":
        return ThroughputMethodConfig(
            get(method, "connections", cond=int, env=env),
            get(method, "threads", cond=int, env=env),
            get(method, "duration", cond=int, env=env),
            iterations
        )
    elif method_type == "latency":
        return LatencyMethodConfig(
            get(method, "connections", env=env, cond=int),
            get(method, "threads", env=env, cond=int),
            get(method, "rate", env=env, cond=int),
            get(method, "duration", env=env, cond=int),
            iterations
        )
    elif method_type == "requests":
        return RequestsMethodConfig(
            get(method, "total_requests", env=env, cond=int),
            get(method, "timeout", env=env, cond=int),
            get_opt(method, "request_timeout", env=env, cond=int, default=10),
            get_opt(method, "signed_requests", env=env, cond=bool, default=False),
            get_opt(method, "persistent_requests", env=env, cond=bool, default=False)
        )
    else:
        error(f"Unknown method type '{method_type}' in {generator}. Use one of {[m.name.lower() for m in LoadStrategy]}")


def split_url(url_expr: str) -> tuple[str, str]:
    parts = url_expr.split("}/{")
    if not len(parts) == 2:
        error("Invalid URL expression, need syntax '{expression}/{expression}'. Got: " + url_expr)
    if not parts[0][0] == "{":
        error("URL expression must start with '{', got: " + url_expr)
    if not len(parts[1]) > 0 or not parts[1][len(parts[1]) - 1] == "}":
        error("URL expression must end with '}', got: " + url_expr)
    parts[0] = parts[0][1:]
    parts[1] = parts[1][:-1]
    return parts[0], parts[1]


def eval_string_expr(expr: str, cond: ValidationCond = True, env: Optional[dict[str, Any]] = None) -> Any:
    """Evaluates the expression as a string

    :param expr: expression to evaluate
    :param cond: condition that the evaluated value must satisfy; if set to None, then it will be ignored
    :param env: environment used for evaluation
    """
    try:
        evaluated_value = eval(expr, env.copy() if env is not None else {}, {'math': math})
        check(evaluated_value, cond, f"expression '{expr}' evaluated as '{evaluated_value}' does not satisfy condition '{str(cond)}'")
        return evaluated_value
    except (SyntaxError, TypeError) as err:
        logging.die(f"Could not eval expression '{expr}': {str(err)}")



def as_number(obj: Any, env: Optional[dict[str, Any]] = None) -> Any:
    if isinstance(obj, int):
        return obj
    elif isinstance(obj, str):
        return eval_string_expr(obj, env=env, cond=lambda v: isinstance(v, int) or isinstance(v, range))
    else:
        error("Value is not a number '" + str(obj) + "'.")


def as_iterable(obj: Any, env: Optional[dict[str, Any]] = None) -> Any:
    if isinstance(obj, int):
        return obj
    elif isinstance(obj, str):
        return eval_string_expr(obj, env=env)
    elif isinstance(obj, list) and len(obj) == 2:
        return list(range(as_number(obj[0], env=env), as_number(obj[1], env=env)))
    elif isinstance(obj, range):
        return list(obj)
    else:
        error("Unknown iterable format: " + str(obj))


def validate_choice(val: Any, choices: tuple[Any, ...] | list[Any]) -> None:
    if val not in choices:
        error(f"Invalid app type. Got '{val}'. Must be one of {choices}")


def get_target(generator: dict[str, Any], app_count: int, env: Optional[dict[str, Any]] = None) -> TargetConfig:
    target = get(generator, "target", cond=True, env=env)
    round_robin_env = {**(env or {}), "ALL": range(0, app_count)}
    if isinstance(target, str):
        app_expr, endpoint_expr = split_url(target)
        app = eval_string_expr(app_expr, cond=int, env=env)
        endpoint = eval_string_expr(endpoint_expr, cond=int, env=env)
        return TargetConfig(app, endpoint)
    elif isinstance(target, dict) and "policy" in target:
        policy = target["policy"]
        if policy in ["parallel", "round-robin", "spike"]:
            apps = as_iterable(get(target, "apps", True, env=round_robin_env), env=round_robin_env)
            endpoints = as_iterable(get(target, "endpoints", True, env=round_robin_env), env=round_robin_env)
            if policy == "round-robin":
                pre_begin_sleep = get_opt(target, "pre_begin_sleep", cond=int, default=0, env=env)
                between_rounds_sleep = get_opt(target, "between_rounds_sleep", cond=int, default=0, env=env)
                request_sleep_interval = get_opt(target, "request_sleep_interval", cond=float, default=0.0, env=env)
                agent_emit_interval_during_sleep = get_opt(target, "agent_emit_interval_during_sleep", cond=float, default=0.0, env=env)
                post_end_sleep = get_opt(target, "post_end_sleep", cond=int, default=0, env=env)
                return RoundRobinTargetConfig(apps, endpoints, pre_begin_sleep, between_rounds_sleep, request_sleep_interval, agent_emit_interval_during_sleep, post_end_sleep)
            elif policy == "spike":
                pre_begin_sleep = get_opt(target, "pre_begin_sleep", cond=int, default=0, env=env)
                agent_emit_interval_during_sleep = get_opt(target, "agent_emit_interval_during_sleep", cond=float, default=0.0, env=env)
                post_end_sleep = get_opt(target, "post_end_sleep", cond=int, default=0, env=env)
                return SpikeTargetConfig(apps, endpoints, pre_begin_sleep, agent_emit_interval_during_sleep, post_end_sleep)
            elif policy == "parallel":
                return ParallelTargetConfig(apps, endpoints)
            else:
                error(f"Unimplemented target policy '{policy}' in {generator}")
        else:
            error(f"Unknown target policy '{policy}' in {generator}")
    else:
        error(f"Invalid target policy '{target}' in {generator}")



def load_toml_from_str(input_str: str) -> dict[str, Any]:
    """Parses the TOML out of the input string

    :param input_str: input string in json format
    """
    try:
        parsed_json = toml.loads(input_str, _dict=OrderedDict)
    except toml.TomlDecodeError as err:
        logging.log(f" - Error while parsing the scenario: {err}")
        json_lines = input_str.split("\n")
        error_context = "\n".join(
            json_lines[err.lineno-2:err.lineno] +
            [" " * (err.colno - 1) + "^"] +
            json_lines[err.lineno:err.lineno+2]
        )
        logging.log_file_content("", error_context)
        sys.exit(1)
    return parsed_json

def is_section_type(scenarios: list[dict[str, Any]], section_key: str, type: Any) -> bool:
    """Helper function that tests whether section_key corresponds to the given type

    This might look strange, but we need to differentiate between list (apps, targets) and
    dictionary sections (scenario, harness) when merging the scenarios and bases.

    This is an alternative to having these sections hardcoded

    :param scenarios: list of parsed TOML scenarios
    :param section_key: key corresponding to the section in TOML (apps, target, etc.)
    :param type: type (list, dict) we are checking
    """
    return all(section_key not in scenario or isinstance(scenario[section_key], type) for scenario in scenarios)

def find_value(bases: list[dict[str, Any]], section: str, key: str) -> Any:
    """Finds in a list of dictionaries first value corresponding to 'section.key'

    Note: there is no handling of multiple 'section.key' available in the list;
    any handling of multiple values must be solved outside this function.

    Used to get a value from list of multiple bases

    :param bases: list of dictionaries representing the bases of the scenario; each contains sections and individual keys
    :param section: name of the section (e.g. apps, generators)
    :param key: name of the individual key we are looking for (e.g. apps.binary)
    :return: first value of the 'section.key' in the list or None
    """
    for base in bases:
        if section in base and key in base[section]:
            return base[section][key]

def load_base_scenario_from_file(scenario_file: str, working_directory: pathlib.Path, processed_scenarios: set[pathlib.Path]) -> dict[str, Any]:
    """Loads the base scenario from the file and evaluates it

    :param scenario_file: path to the scenario
    :param working_directory: working directory, in order to evaluate path to the base scenario
    :param processed_scenarios: set of scenarios that were already parsed
    """
    path_to_scenario = (working_directory / scenario_file).resolve()
    with open(path_to_scenario) as scenario_handle:
        scenario = scenario_handle.read()
    new_working_directory = path_to_scenario.parent
    return load_scenario(scenario, new_working_directory, processed_scenarios)

def load_scenario(scenario_str: str, working_directory: pathlib.Path, processed_scenarios: set[pathlib.Path]) -> dict[str, Any]:
    """Loads the scenario from the string including all of its bases

    Loads the scenario from the string and for every scenario.extends it recursively loads the scenario

    :param scenario_str: TOML representation of the scenario
    :param working_directory: working directory of the scenario
    """
    master_scenario = load_toml_from_str(scenario_str)
    base_scenarios_files = master_scenario['scenario']['extends'] if 'scenario' in master_scenario and 'extends' in master_scenario['scenario'] else []

    # Recursively parses the base scenarios
    base_scenarios = []
    for base_scenario_file in base_scenarios_files:
        path = pathlib.Path(base_scenario_file)
        # Check if there is a cycle in the extensions
        if path in processed_scenarios:
            logging.die("Trying to process already processed scenarios. This means potential cycle in the 'scenario.extends'. Offending file: {str(path)}")
        # We add to processed to detect cycles
        #  - note we allow diamonds, the clash of the multiple fields specified in multiple bases is solved below while merging
        new_processed_scenarios = processed_scenarios.union({path})
        base_scenarios.append(load_base_scenario_from_file(base_scenario_file, working_directory, new_processed_scenarios))

    all_scenarios = base_scenarios + [master_scenario]

    # No need to merge anything
    if len(base_scenarios) == 0:
        return master_scenario

    # Merging bases into the master
    else:
        final_scenario: dict[str, Any] = {}
        # To preserve the order of the section keys
        section_keys = list(OrderedDict.fromkeys(k for s in all_scenarios[::-1] for k in s.keys()))

        # Iterating through every section
        for section_key in section_keys:
            # List sections are merged together to a single list starting from oldest base to the master
            if is_section_type(all_scenarios, section_key, list):
                final_list = []
                for scenario in all_scenarios:
                    final_list.extend(scenario.get(section_key, []))
                final_scenario[section_key] = final_list
            # Dictionary sections are merged into a single dictionary
            elif is_section_type(all_scenarios, section_key, dict):
                final_dict: dict[str, Any] = {}
                # We need to keep the order of the keys, hence this ugly slop
                keys = OrderedDict.fromkeys(chain.from_iterable(
                    dict.keys(scenario.get(section_key, {})) for scenario in all_scenarios[::-1]
                )).keys()
                # For every key in the section
                for key in keys:
                    # We skip this pair as it is no longer needed
                    if section_key == 'scenario' and key == 'extends':
                        continue
                    defined_in_master = key in master_scenario.get(section_key, {})
                    defined_in_bases = len(list(filter(lambda x: key in x.get(section_key, {}), base_scenarios)))

                    # If the key is defined in the master, and not in any base -> use master
                    if defined_in_master and defined_in_bases == 0:
                        final_dict[key] = master_scenario[section_key][key]
                    # If the key is defined in the master and in at least one base -> use explicit override
                    elif defined_in_master and defined_in_bases >= 1:
                        if not isinstance(master_scenario[section_key][key], Mapping) or 'override' not in master_scenario[section_key][key]:
                            logging.die(f"'{section_key}.{key}' defined both in master and base scenario. You need to explicitly override the value with '{key} = {{override = val}}'")
                        final_dict[key] = master_scenario[section_key][key]['override']
                    # If the key is not defined in the master and it is defined in the base -> use base
                    elif not defined_in_master and defined_in_bases == 1:
                        final_dict[key] = find_value(base_scenarios, section_key, key)
                    # If the key is not defined in the master and is defined in the multiple bases -> fail
                    elif not defined_in_master and defined_in_bases > 1:
                        logging.die(f"'{section_key}.{key}' defined in multiple base scenarios and not overridden in master")
                    else:
                        assert False, "How did we get here?"
                final_scenario[section_key] = final_dict
            else:
                assert False, "Impossible has happened, there is non-list, non-dictionary master section"
        return final_scenario


def parse_scenario_file(scenario_file: pathlib.Path, parameters: Optional[dict[str, None]] = None) -> ScenarioConfig:
    """Parses the scenario from the input file

    Wrapper for the main function: 'parse_scenario' (one is used for parsing from string, the other from the file)

    :param scenario_file: path to existing input file
    :param parameters: optional dictionary of parameters obtained from the CLI
    """
    with open(scenario_file, 'r') as scenario_handle:
        scenario_str= scenario_handle.read()
    # file paths given in the config will be resolved relative to the location of the config file
    working_directory = scenario_file.parent
    return parse_scenario(scenario_str, working_directory, parameters, scenario_file)


def load_processed_scenario_toml_from_file(scenario_file: pathlib.Path) -> str:
    """Loads a scenario file and returns TOML with scenario.extends merged in."""
    with open(scenario_file, 'r') as scenario_handle:
        scenario_str = scenario_handle.read()
    processed_scenarios = {scenario_file}
    config_json = load_scenario(scenario_str, scenario_file.parent, processed_scenarios)
    return toml.dumps(config_json)


def _evaluate_scenario_environment(config_json: dict[str, Any], parameters: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    if parameters is None:
        parameters = {}

    parameters_dict = get_opt(config_json, "parameters", cond=dict, default={})
    for external_param in parameters.keys():
        if external_param not in parameters_dict:
            raise ValueError(f"User-provided parameter '{external_param}' doesn't exist in the scenario! Parameters are: {parameters_dict.keys()}")
    for external_param, value in parameters.items():
        parameters_dict[external_param] = value

    env_dict = get_opt(config_json, "environment", cond=dict, default={})
    duplicate_env_param = set(parameters_dict.keys()) & set(env_dict.keys())
    if duplicate_env_param:
        keys_str = "', '".join(sorted(duplicate_env_param))
        raise ValueError(f"Duplicate entries found in both 'parameters' and 'environment': '{keys_str}' !")

    # We evaluate the variables in order, so user-defined variables can contain previously defined variables
    #  - for example as follows:
    #     apps = 10
    #     duration = 1000
    #     period = durations / apps
    # Note that the parameters are evaluated before environment entries.
    scenario_environment: dict[str, Any] = {}
    for d in [parameters_dict, env_dict]:
        for key in d.keys():
            if key in RESERVED_ENVIRONMENT_KEYS:
                raise ValueError(f"{key} is reserved keyword and cannot be overwritten in 'environment' or 'parameters'.")
            scenario_environment[key] = get_field(d, key, env=scenario_environment)

    return parameters_dict, scenario_environment


def parse_graalos_config_file(config_file: pathlib.Path) -> GraalOSConfig:
    """Parses only the [graalos] section from a TOML scenario/config file."""
    config_file = config_file.resolve()
    with open(config_file, 'r') as config_handle:
        config_str = config_handle.read()
    config_json = load_scenario(config_str, config_file.parent, {config_file})
    _, scenario_environment = _evaluate_scenario_environment(config_json)
    graalos_config_dict = get_opt(config_json, "graalos", cond=dict, default={}, env=scenario_environment)
    return get_graalos(graalos_config_dict, env=scenario_environment)


def parse_scenario(
    scenario_str: str,
    working_directory: pathlib.Path = pathlib.Path.cwd(),
    parameters: Optional[dict[str, Any]] = None,
    scenario_file: Optional[pathlib.Path] = None,
) -> ScenarioConfig:
    """Parses the scenario

    :param scenario_str: string in json format containing the definition of the scenario
    :param working_dir: working directory of the scenario (either current directory or the parent of the input scenario file)
    :param parameters: optional dictionary of parameters obtained from the CLI
    :param scenario_file: path to the scenario file, if the scenario_str is coming from a file
    """
    logging.header("Parsing the scenario")
    if parameters is None:
        parameters = {}

    processed_scenarios = set()
    if scenario_file:
        processed_scenarios.add(scenario_file)
    config_json = load_scenario(scenario_str, working_directory, processed_scenarios)

    logging.log_file_content(f"{scenario_file}", toml.dumps(config_json))

    #  - these are shared across all apps per scenario
    AppConfig.setup_scripts.clear()
    AppConfig.teardown_scripts.clear()

    parameters_dict, scenario_environment = _evaluate_scenario_environment(config_json, parameters)

    graalos_config_dict = get_opt(config_json, "graalos", cond=dict, default={}, env=scenario_environment)
    parsed_graalos_config = get_graalos(graalos_config_dict, env=scenario_environment)

    apps: list[AppConfig] = []
    app_index = 0
    group_index = 0
    for app in get(config_json, "apps", cond=list, env=scenario_environment):
        protocol = get_opt(app, "protocol", cond=str, default="http", env=scenario_environment)
        if protocol not in ["http", "https"]:
            raise ValueError(f"Protocol must be either 'http' or 'https'! Got '{protocol}'")
        endpoints = get(app, "endpoints", cond=list, env=scenario_environment)
        name = get(app, "name", cond=str, env=scenario_environment)
        app_type = get(app, "type", cond=str, env=scenario_environment)
        version = get_opt(app, "version", cond=str, default="", env=scenario_environment)
        app_deployment = get(app, "deployment", cond=str, env=scenario_environment)
        validate_choice(app_type.upper(), APP_TYPES)
        validate_choice(app_deployment, APP_DEPLOYMENT_TYPES)
        app_binary = get(app, "binary", cond=dict, env=scenario_environment)
        count = get_opt(app_binary, "count", cond=int, default=1, env=scenario_environment)
        setup = get_opt(app, "setup", cond=str, default="", env=scenario_environment)
        app_metric_groups = get_metrics_group_list(app, env=scenario_environment)
        if setup:
            AppConfig.setup_scripts[f"{group_index}-{name}"] = setup
        teardown = get_opt(app, "teardown", cond=str, default="", env=scenario_environment)
        if teardown:
            AppConfig.teardown_scripts[f"{group_index}-{name}"] = teardown
        for batch_index in range(0, count):
            app_env = {**scenario_environment, "IDX": app_index, "BATCH_IDX": batch_index}
            binary_method = get_binary(app_binary, app_deployment, env=app_env)
            port = get_opt(app, "port", cond=int, env=app_env, default=443)
            memory = get_opt(app, "memory", cond=int, default=256, env=app_env)
            validate_choice(memory, ALLOWED_MEMORY)
            ocpus = get_opt(app, "ocpus", cond=float, default=get_default_ocpus(memory), env=app_env)
            validate_choice(ocpus, ALLOWED_OCPUS)
            binary: Optional[BinaryConfig] = None
            if "network_mode" in binary_method and binary_method["network_mode"] == NetworkType.PRIVATE:
                if app_deployment == "service" and not binary_method["subnet_id"]:
                    error(f"Private service apps require 'subnet_id' in [apps.binary]: {app_binary}")
            if app_deployment == "graalos-stack":
                if parsed_graalos_config.stack is None:
                    raise ValueError("Cannot parse GraalOS stack app without '[graalos.stack]' configuration")
                binary = GraalOSStackBinaryConfig(
                    binary_method["domain_prefix"],
                    parsed_graalos_config.stack.domain,
                    binary_method["container_image"],
                    binary_method["container_image_id"],
                    binary_method["entrypoint"],
                    binary_method["environment"],
                    binary_method["guest_port"]
                )
            elif "artifact_id" in binary_method:
                binary = BinaryFromArtifactId(
                    binary_method["artifact_id"],
                    binary_method["environment"],
                    binary_method["network_mode"],
                    binary_method["subnet_id"]
                )
            elif "artifact_id_file" in binary_method:
                binary = BinaryFromArtifactIdFileConfig(
                    binary_method["artifact_id_file"],
                    batch_index,
                    working_directory,
                    binary_method["environment"],
                    binary_method["network_mode"],
                    binary_method["subnet_id"]
                )
            elif "binary_command" in binary_method:
                binary = LocalBinaryConfig(
                    binary_method["binary_command"],
                    binary_method["base_url"],
                    binary_method["unique_copy"],
                    binary_method["nginx_keepalive"],
                    binary_method["nginx_keepalive_timeout"],
                    binary_method["extra_graalhost_dir_mounts"],
                    binary_method[GRAALHOST_CONFIG_FILE],
                    binary_method[FIRECRACKER_KERNEL_IMAGE],
                    binary_method[FIRECRACKER_MEM_MIB],
                    binary_method[FIRECRACKER_VCPU_COUNT],
                    binary_method[FIRECRACKER_ROOTFS_SIZE_MIB],
                    binary_method["environment"],
                    binary_method["nginx_keepalive_requests"],
                )
            elif "keg_id" in binary_method:
                binary = RunningKegBinaryConfig(
                    binary_method["keg_id"],
                    binary_method["binary_path"],
                    binary_method["keg_domain"],
                    binary_method["launch_arguments"]
                )
            elif "base_url" in binary_method:
                binary = RunningBinaryConfig(binary_method["base_url"])
            assert binary is not None
            apps.append(AppConfig(group_index, app_index, name, app_type, protocol, port, memory, ocpus, endpoints, binary, app_deployment, version, app_metric_groups))
            app_index += 1
        group_index += 1

    agents = []
    agent_index = 0
    agent_list = get_opt(config_json, "agent", list, [])
    for agent in agent_list:
        agent_env= {**scenario_environment, "NUM_APPS": len(apps), "IDX": agent_index}
        agent_type = get(agent, "type", cond=str, env=agent_env)
        agent_emit_metrics = get_opt(agent, "emit", cond=list, default=[], env=agent_env)
        agent_metric_groups = get_metrics_group_list(agent, env=agent_env)
        count = get(agent, "count", cond=int, env=agent_env)
        for batch_index in range(0, count):
            batch_env =  {**agent_env, "BATCH_IDX": batch_index, "IDX": agent_index}
            attached_to_app = get(agent, "app", env=batch_env, cond=int)
            assert attached_to_app < app_index, f"index out of bounds: cannot attach agent to app {attached_to_app}; choose from <0, {app_index})"
            assert apps[attached_to_app].deployment in ('nginx-tinyinit', 'nginx-tinyinit-graalhost', 'nginx-tinyinit-gvisor', 'nginx-tinyinit-firecracker', 'dataplane'), "Agents are not supported outside of app.deployment = ('nginx-tinyinit', 'nginx-tinyinit-graalhost', 'nginx-tinyinit-gvisor', 'nginx-tinyinit-firecracker', 'dataplane')"
            agents.append(AgentConfig(agent_index, agent_type, attached_to_app, agent_emit_metrics, agent_metric_groups))
            agent_index += 1

    generators = []
    generator_index = 0
    generators_list = get(config_json, "generators", cond=list, env=scenario_environment)
    for generator in generators_list:
        generator_env = {**scenario_environment, "IDX": generator_index, "NUM_APPS": len(apps)}
        count = get_opt(generator, "count", env=generator_env, cond=int, default=1)
        attached_agents = get_opt(generator, "agents", env={**generator_env, "ALL": list(range(0, len(agents)))}, cond=list, default=[])
        http_headers = get_opt(generator, "http_headers", cond=list, default=[], env=generator_env)
        metric_groups = get_metrics_group_list(generator, env=generator_env)
        assert all(a < len(agents) for a in attached_agents), f"Invalid specification of attached agents '{attached_agents}': indexes must be < {len(agents)}"
        for batch_index in range(0, count):
            batch_env = {**generator_env, "BATCH_IDX": batch_index, "IDX": generator_index}
            name = get_opt(generator, "name", cond=str, default="", env=batch_env)
            method = get_generator_method(generator, env=batch_env)
            delay = get_opt(generator, "delay", cond=int, env=batch_env, default=0)
            success_threshold = get_opt(generator, "success_threshold", lambda v: isinstance(v, (float, int)) and 0 <= v <= 1, default=DEFAULT_SUCCESS_THRESHOLD, env=batch_env)
            expects = get_generator_expects(generator, env=batch_env)
            target = get_target(generator, len(apps), env=batch_env)
            if (expects.body is not None or expects.code is not None) and not isinstance(method, RequestsMethodConfig):
                raise ValueError("[generators.expect] is supported for 'requests' only")
            generators.append(GeneratorConfig(generator_index, name, delay, method, target, success_threshold, expects, metric_groups, http_headers, attached_agents))
            generator_index += 1

    scenario = get(config_json, "scenario", cond=dict, env=scenario_environment)
    harness_config = get_opt(config_json, "harness", cond=dict, default={}, env=scenario_environment)
    results_processings = get_opt(scenario, "results_processing", cond=list, default=[])
    scenario_deployment = get(scenario, "deployment", cond=str, env=scenario_environment)
    scenario_metric_groups = get_metrics_group_list(scenario, env=scenario_environment)
    if not scenario_metric_groups:
        scenario_metric_groups = [MetricGroupConfig(collect=["scenario-success", "scenario-resource-breakdown"], group="default")]
    validate_choice(scenario_deployment, SCENARIO_DEPLOYMENT_TYPES)
    # If the scenario.deployment is not set to mixed, then we iterate over all apps and tests that they are deployed in the same way
    if scenario_deployment != "hybrid":
        for app in apps:
            if app.deployment != scenario_deployment:
                raise ValueError(f"'scenario.deployment' is set to '{scenario_deployment}', but {app.unique_name} is deployed as '{app.deployment}'")
    return ScenarioConfig(
        get(scenario, "name", cond=str, env=scenario_environment),
        get(scenario, "description", cond=str, env=scenario_environment),
        get_opt(scenario, "group", cond=str, default="", env=scenario_environment),
        scenario_deployment,
        scenario_metric_groups,
        [UserPostprocessingConfig(p["name"], p["code"]) for p in results_processings],
        apps,
        generators,
        parameters_dict,
        agents,
        get_harness(harness_config, env=scenario_environment),
        parsed_graalos_config
    )


if __name__ == "__main__":
    logging.setup_logger('stdout')
    if len(sys.argv) != 2:
        print(f"Usage: {pathlib.Path(sys.argv[0]).name} <scenario-file>", file=sys.stderr)
        sys.exit(1)

    logging.raw_log(parse_scenario_file(pathlib.Path(sys.argv[1])))
