from __future__ import annotations

import math
import re
import os
import pickle
import subprocess
import shutil
import asyncio
import tempfile
import uuid
import shlex
import time
import io

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Optional, List, Tuple, Callable, Sequence, TYPE_CHECKING
from gos_harness import async_cmd_runner, scenario_parser, gos_logging as logging
from gos_harness.utils.module_loader import LazyLoader
from gos_harness.agents import Agent
from gos_harness.async_cmd_runner import append_list_to_env_var
from gos_harness.gos_oci import BaseApp
from gos_harness.metrics.metrics import RequestTimeMetric, RequestTimeBreakdownMetric, LatencyDistributionMetric, LatencyDistributionBreakdownMetric, ThroughputMetric, RequestCodeCountMetric, LoadTesterSuccessMetric
from gos_harness.metrics.metric_list import MetricList
from gos_harness.scenario_parser import (
    LoadStrategy, ThroughputMethodConfig, LatencyMethodConfig, RequestsMethodConfig, FixedTimeMethodConfig,
    TargetConfig, ParallelTargetConfig, RoundRobinTargetConfig, SpikeTargetConfig
)
from gos_harness.rpc import StoredProcedure

# Lazy Loaded packages used for sending requests over persistent connections
#  - 'pycurl' is used to manually control the libcurl options
pycurl = LazyLoader("pycurl", globals(), "pycurl")

if TYPE_CHECKING:
    import pycurl as pycurl_module
    from requests import PreparedRequest

_current_script_directory = os.path.dirname(os.path.realpath(__file__))
CURL_FORMAT_FILE_LOCATION = os.path.join(_current_script_directory, "curl-format.txt")
LOAD_GENERATOR_DIR= os.path.dirname(_current_script_directory)
SCRIPTS_DIR = os.path.join(LOAD_GENERATOR_DIR, "scripts")
CURL_MATCHER = re.compile(r"^(?P<request_ts>\d*\.?\d+) curl: (?P<curl>\d*\.*\d+)s code: (?P<http_code>\d+) url: (?P<url>\S+) local_ip: (?P<local_ip>\S*) local_port: (?P<local_port>\d*) \(namelookup: (?P<namelookup>\d*\.*\d*)s?, connect: (?P<connect>\d*\.*\d*)s?, appconnect: (?P<appconnect>\d*\.*\d*)s?, pretransfer: (?P<pretransfer>\d*\.*\d*)s?, redirect: (?P<redirect>\d*\.*\d*)s?, starttransfer: (?P<starttransfer>\d*\.*\d*)s?, x_request_start: (?P<x_request_start>\S*), tinyinit_activation: (?P<tinyinit_activation>\S*), tinyinit_fork: (?P<tinyinit_fork>\S*), tinyinit_exec: (?P<tinyinit_exec>\S*), x_app_start: (?P<x_app_start>\S*), x_response_start: (?P<x_response_start>\S*)\)", re.MULTILINE)

LATENCY_DISTRIBUTION_METRIC_OBJECTS: tuple[str, ...] = (
    "namelookup", "pretransfer-time", "connect-time", "appconnect-time",
    "node-time", "response-time", "network-time"
)

@dataclass
class Request:
    """Represents a full request with its breakdown used for further analysis.

    :ivar start_ts: start of the request
    :ivar end_ts: end of the request (receiving the response)
    :ivar iteration: metric iteration, i.e. in which order the request was sent to the app by the same load generator
    :ivar request_time: total time for the request
    :ivar breakdown: map of the individual components
    """
    start_ts: float
    end_ts: float
    iteration: int
    request_time: float
    breakdown: dict[str, float]

    @classmethod
    def percentile(cls, k_th: int, sorted_values: list[Request]) -> Request:
        """Computes the k_th percentile of the list of requests.

        In case there are two ranks for the given quantile, we do the interpolation of the values.

        :param k_th: quantile rank in range <0, 100>
        :param sorted_values: list of requests sorted by request_time
        """
        x = (len(sorted_values) - 1) * k_th / 100
        fr = int(x)
        cl = int(x + 0.5)
        if fr == cl:
            return sorted_values[fr]
        # interpolate between the two closest ranks
        request_time =  sorted_values[fr].request_time * (cl - x) + sorted_values[cl].request_time * (x - fr)
        breakdown = {}
        # We use interpolation to compute the hypothetical request's breakdown (note that no such request exists)
        # We limit the breakdown only in the metric.objects that are common for both cold and non-cold starts
        for key in sorted_values[fr].breakdown.keys():
            if key in LATENCY_DISTRIBUTION_METRIC_OBJECTS:
                breakdown[key] = sorted_values[fr].breakdown.get(key, 0) * (cl - x) + sorted_values[cl].breakdown.get(key, 0) * (x - fr)
        return Request(sorted_values[fr].start_ts, sorted_values[fr].end_ts, sorted_values[fr].iteration, request_time, breakdown)


def _parse_curl_outputs(output: str) -> list[dict[str, str]]:
    return [match.groupdict() for match in CURL_MATCHER.finditer(output)]

def parse_http_counts(output: str, success_threshold: float) -> tuple[bool, dict[int, int]]:
    """
    Extract request counts per HTTP code and return overall success/failure along with the counts.
    The returned tuple contains first the boolean success value followed by the counts' dictionary.
    :param output: String containing wrk output with HTTP status lines
    :param success_threshold: Minimum ratio of 2xx codes for success
    :rtype tuple:
    """
    def _map_status(code: str) -> int:
        """Typical output looks like the one below. Since we store HTTP status codes as numbers and it's
        convenient to keep this. We take the risk to approximate corner cases by mapping them to generic codes.

        HTTP status 2xx count: 300
        HTTP status 3xx count: 0
        HTTP status 400 count: 0
        HTTP status 404 count: 0
        HTTP status 429 count: 0
        HTTP status 4xx count: 0
        HTTP status 500 count: 0
        HTTP status 502 count: 0
        HTTP status 503 count: 0
        HTTP status 504 count: 0
        HTTP status 5xx count: 0
        HTTP status xxx count: 0
        """
        if code == '2xx':
            return 200
        elif code == '3xx':
            return 300
        elif code == 'xxx':
            return 0  # typically when the connection can't be established with the server
        elif code == '4xx':
            return 499  # '4xx' being a placeholder for uncommon 400 errors, let's map them all to 499
        elif code == '5xx':
            return 599  # same reasoning as '4xx' above
        else:
            try:
                return int(code)
            except ValueError as e:
                logging.log(f"ERROR: no rule to convert the following HTTP code to integer: '{code}'")
                raise e
    # the regex must match the format that is produced by the corresponding Lua script in scripts/
    matches = re.findall( r'HTTP status (\w+) count: (\d+)', output)
    counts = {_map_status(status): int(count) for status, count in matches if int(count) != 0}
    success = counts.get(200, 0) >= success_threshold * sum(counts.values())
    return success, counts

def extract_latency_breakdown_from_wrk2_output(wrk2_output: str) -> list[tuple[str, float, float]]:
    """Extracts the breakdown of the latency distribution from the wrk2 output.

    The latency distribution is represented by percentiles (P50, P75, ..., P99, P100);
    this distribution is then broken down into individual components. So, for example,
    we break P50 request (median) into two components: network time (how much this request
    spent in the request and response) and node time (how much this request spent in the node,
    i.e. the graalos service, graalhost, and/or native image).

    The following sequence diagram illustrates the parts of the breakdown

                     TIMESTAMP | metric.object

                               * (wrk2 requests starts)
                               |
           X-WRK-Request-Start *
                               | network-time
     X-OCI-GRAAL-Request-Start *
                               | node-time
              X-Response-Start *
                               | network-time
                               * (response function in script)
                               |
                               * (wrk2 request ends)


    The wrk2_output consists of regular output of the wrk2 as well as the output of the
    http_codes_and_breakdown.lua script. The output is as follows and contains a short
    table that contains the percentiles and network, node time and total time measured in the
    script. Note that the total is slightly lower than the times reported by the wrk2.
    This is most likely due to different way the total is computed and the wrk2 numbers
    contain small overhead. The times are in [ms].

    ------------------------------

    Latency breakdown (node and network time)
                  Total  Network     Node
    -  50.000:     1.68     1.68     0.00
    -  75.000:     2.26     2.26     0.00
    -  90.000:     2.91     1.91     1.00
    -  99.000:     7.03     7.03     0.00
    -  99.900:    21.01    20.99     0.02
    -  99.990:    28.53     2.33    26.20
    -  99.999:    29.56     2.64    26.92
    - 100.000:    29.68     2.68    27.00

    From the output, we record only P50, P75, P90, P99 and P100 and report two metric.objects:
      1. 'node-time' is the time between X-OCI-GRAAL-Request-Start and X-Response-Start timestamps,
        obtained from the native image binary (the binary needs to set these values).
      2. 'network-time' is the time between the start of parsing of the response in the wrk2 and
        start of the sending of the request in the wrk2 (set in X-WRK-Request-Start header; the binary
        needs to set handle this value) with 'node-time' subtracted from this value.

    The function returns the breakdown of the latency distribution as list of tuples of metric.object,
    percentile and metric.value representing the
    value of the breakdown.

    :param wrk2_output: output of the wrk2
    :return: list of tuples of (metric.object, percentile, metric.value)
    """
    matches = re.finditer(r"\s*-\s*(?P<percentile>\d+)\.000:\s*(?P<total>\d+\.\d+)\s*(?P<network>\d+\.\d+)\s*(?P<node>\d+\.\d+)", wrk2_output)
    breakdown = []
    for match in matches:
        percentile = float(match.group('percentile').strip())
        network_latency = float(match.group('network').strip())
        node_latency = float(match.group('node').strip())
        breakdown.append(("node-time", percentile, node_latency))
        breakdown.append(("network-time", percentile, network_latency))
    return breakdown

def parse_wrk2(output: str) -> list[tuple[float, float]]:
    matches = re.finditer(r"(?P<percentile>.*)%\s*(?P<latency>\d+\.\d+)(?P<unit>[a-zA-Z]+)", output)
    latencies = []
    for match in matches:
        percentile = float(match.group('percentile').strip())
        if not percentile.is_integer():
            # currently dropping 99.X for X != 0 due to charting issues: GR-57394
            continue
        latency = float(match.group('latency').strip())
        unit = match.group('unit').strip()
        if unit == "s":
            latency *= 1000
        elif unit == "m":
            latency *= 60 * 1000
        elif unit == "us":
            latency /= 1000
        elif unit != "ms":
            raise ValueError(f"Unknown unit while parsing latency results! Got '{unit}'")
        latencies.append((percentile, latency))
    if len(latencies) == 0:
        raise ValueError("No latency result could be parsed !")
    return latencies


def parse_wrk(output: str) -> Optional[float]:
    match = re.search(r"Requests/sec:\s+(?P<throughput>\d+(\.\d+)?)", output)
    ops_per_sec = float(match.group('throughput')) if match else None
    return ops_per_sec


def build_wrk_cmd(wrk_path: str, wrk_params: list[str], http_headers: list[str], app_url: str, app: BaseApp) -> list[str]:
    """Builds commmand for wrk or wrk2 load generation

    :param wrk_path: path to wrk or wrk2
    :param wrk_params: list of params for wrk or wrk2, e.g. -d <DURATION> or -R <RATE>
    :param http_headers: list of additional http headers
    :param app_url: endpoint url of the app we are generating load for
    """
    additional_http_headers = []
    for header in http_headers:
        additional_http_headers.append("-H")
        additional_http_headers.append(header)

    # If there is available preresolve, we replace the domain with the IP and set the Host header instead
    dns_resolve = app.curl_dns_resolve()
    if dns_resolve:
        # Resolution is in form of DOMAIN:PORT:IP
        domain, _, ip = dns_resolve.split(":")
        app_url = app_url.replace(domain, ip)
        additional_http_headers.append("-H")
        additional_http_headers.append(f"Host: {domain}")

    return [wrk_path] + wrk_params + additional_http_headers + [app_url]

def identity(x: object) -> object:
    """Identity returns its parameter as it is; used as a default for functions accepting functions.

    This is extracted out of anonymous lambda default, since we want to pickle the MetricLists,
    that contains the load generator as its members. And anonymous lambdas cannot be pickled.
    """
    return x


class LoadGenerator:
    def __init__(
        self,
        configuration: scenario_parser.GeneratorConfig,
        apps: List[BaseApp],
        agents: list[Agent],
        id: Optional[str] = None,
        fn_map: Callable[[Any], Any] = identity,
        env_vars: Optional[dict[str, str]] = None,
    ) -> None:
        self._id = id if id is not None else str(uuid.uuid4())[:8]
        self._apps = apps
        self._run_under_proxy = False
        self._agents = [agents[attached_agent] for attached_agent in configuration.agents]
        self._env_vars = env_vars
        self._fn_map = fn_map

        self._initialized = True
        self._configuration = configuration

        # Maps for storing persistent connection and its buffer
        #  - first map stores the pycurl.Curl object that represents the setup of a session of sending requests
        #  - the second map stores the buffers for obtaining the response (the headers)
        self._persistent_connection_map: dict[str, "pycurl_module.Curl"] = {}
        self._persistent_connection_buffers_map: dict[str, io.BytesIO] = {}

        # Map of reusable authorization headers for signed requests
        #  - the keys are in form of '{method},{url}', e.g. 'GET,service.example.invalid'
        self._authorization_headers: dict[str, "PreparedRequest"] = {}

        self._script_temp_dir: Optional[str] = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self.configuration.name if self.configuration else ""

    @property
    def apps(self) -> List[BaseApp]:
        return self._apps

    @property
    def agents(self) -> list[Agent]:
        return self._agents

    @property
    def signed_requests(self) -> bool:
        return isinstance(self.configuration.method, RequestsMethodConfig) and self.configuration.method.signed_requests

    @property
    def persistent_requests(self) -> bool:
        return isinstance(self.configuration.method, RequestsMethodConfig) and self.configuration.method.persistent_requests

    @property
    def configuration(self) -> scenario_parser.GeneratorConfig:
        return self._configuration

    def get_persistent_connection(self, url: str) -> "pycurl_module.Curl":
        """Creates a persistent connection for the given URL

        If the url does have already a persistent connection, we create a new session with following setup:
          - we drop the output
          - we enforce the reusing of the connection
          - we enforce insecure connection (i.e. no verification of host/peer)
          - we make the output silent
          - we disable the signals during the transfer
          - we set the buffer for getting the headers

        We can then use perform() to send the request: the first request will do the TCP+TLS/SLL handshake
        and then next requests will be direct.

        :param url: target endpoint url, where we will send the request
        :return: persistent connection
        """
        if url not in self._persistent_connection_map:
            conn = pycurl.Curl()
            conn.setopt(pycurl.URL, url)
            # Discard the body
            conn.setopt(pycurl.WRITEDATA, io.BytesIO())
            # Ensure that the connection will be reused
            conn.setopt(pycurl.FORBID_REUSE, False)
            conn.setopt(pycurl.FRESH_CONNECT, False)
            # Disable signals during the connection
            conn.setopt(pycurl.NOSIGNAL, 1)
            # Don't verify host and peer
            conn.setopt(pycurl.SSL_VERIFYHOST, 0)
            conn.setopt(pycurl.SSL_VERIFYPEER, 0)
            # Be silent
            conn.setopt(pycurl.VERBOSE, 0)

            # Set buffer to collect the headers
            self._persistent_connection_buffers_map[url] = io.BytesIO()
            conn.setopt(pycurl.HEADERFUNCTION, self._persistent_connection_buffers_map[url].write)
            self._persistent_connection_map[url] = conn
        return self._persistent_connection_map[url]


    def get_authorization_key(self, method: str, url: str) -> "PreparedRequest":
        """Gets the authorization key for the given HTTP method and the url reusing the key

        :param method: HTTP method; needed for signing.
        :param url: endpoint url; needed for signing.
        :return: prepared request containing the target url and the authentication headers
        """
        # Since, tuples cannot be used as a keys, we construct the key for map
        key = f"{method},{url}"
        if key not in self._authorization_headers:
            self._authorization_headers[key] = gos_oci.create_signed_request(method, url)
        return self._authorization_headers[key]

    def wrk_path(self) -> str:
        # assumes that the binary is on the PATH locally
        return "wrk"

    def wrk2_path(self) -> str:
        # assumes that the binary is on the PATH locally
        return "wrk2"

    def curl_path(self) -> str:
        # assumes that the binary is on the PATH locally
        return "curl"

    def curl_format_file(self) -> str:
        return CURL_FORMAT_FILE_LOCATION

    def scripts_dir(self) -> str:
        if self._script_temp_dir is None:
            # parallel target policy requires creating files in the scripts directory,
            # so each load generator must have a unique scripts directory to work with
            temp_script_dir = tempfile.TemporaryDirectory().name
            shutil.copytree(SCRIPTS_DIR, temp_script_dir)
            logging.log(f"Copied scripts into temporary directory at {temp_script_dir}")
            self._script_temp_dir = temp_script_dir
        return self._script_temp_dir

    def cleanup(self) -> None:
        if self._script_temp_dir is not None:
            logging.log(f"Cleaning up script dir at {self._script_temp_dir}...")
            shutil.rmtree(self._script_temp_dir)
            self._script_temp_dir = None

    async def initialize(self) -> None:
        pass

    async def _run_cmd(
        self,
        cmd: list[str],
        timeout: int = 360,
        cmd_mapping: bool = True,
        quiet: bool = False,
        env: Optional[dict[str, str]] = None,
        no_proxy: Optional[list[str]] = None,
    ) -> tuple[subprocess.CompletedProcess[bytes], float, float]:
        if not self._initialized:
            raise Exception("Load generator must be initialized first!")
        mapped_command = self._fn_map(cmd) if cmd_mapping else cmd
        final_env = self._env_vars.copy() if self._env_vars else os.environ.copy()
        if env:
            for k, v in env.items():
                final_env[k] = v
        if no_proxy:
            final_env = append_list_to_env_var(final_env, "NO_PROXY", no_proxy)
            final_env = append_list_to_env_var(final_env, "no_proxy", no_proxy)
        res, start_ts, end_ts = await async_cmd_runner.run(mapped_command, caller_name=f"{self.__class__.__name__}({self.id})", timeout=timeout, quiet=quiet, env=final_env)
        return res, start_ts, end_ts

    def _extract_list_of_requests(self, metric_list: MetricList) -> list[Request]:
        """Extracts list of requests from "requests" method load generator represented by Request objects.

        This is used for further analysis, e.g. for computing the latency-distribution of these requests.

        The bench server schema is insufficient here, as the data are all over place and we need to relate
        all breakdowns to particular requests, in order to identify each request.

        :return: list of requests
        """
        # We first iterate over requests and create a Request objects without the breakdown
        #  - We temporarily map this using metric_iteration (this should be unique for request load tester)
        #  - This will be used to map individual breakdowns to the same request time
        requests_per_app = {}
        for res in metric_list.iterate(**{"metric.name": "request-time"}):
            request_time = res["metric.value"]
            start_ts = res["load-tester.start-ts"]
            end_ts = res["load-tester.end-ts"]
            metric_iteration = res["metric.iteration"]
            requests_per_app[metric_iteration] = Request(start_ts, end_ts, metric_iteration, request_time, {})

        # Now we populate the breakdown for each request based on metric.iteration
        for res in metric_list.iterate(**{"metric.name": "request-time-breakdown"}):
            metric_iteration = res["metric.iteration"]
            if metric_iteration in requests_per_app:
                requests_per_app[metric_iteration].breakdown[res["metric.object"]] = res["metric.value"]

        # Flatten to single list of requests
        return list(requests_per_app.values())

    def _compute_latency_for_raw_requests(self, metric_list: MetricList) -> None:
        """Computes the latency-distribution for the requests from the "requests" load generator.

        For each app, we compute the latency distribution and latency distribution breakdown.
        """
        requests = self._extract_list_of_requests(metric_list)

        if requests:
            metric_groups = self.configuration.metric_groups
            sorted_raw_times = sorted(requests, key=lambda req: req.request_time)
            min_ts = min(req.start_ts for req in requests)
            max_ts = max(req.end_ts for req in requests)
            latencies_of_requests = [
                (quantile_rank, Request.percentile(quantile_rank, sorted_raw_times))
                for quantile_rank in [0, 25, 50, 75, 90, 95, 99, 100]
            ]
            for quantile_rank, request in latencies_of_requests:
                metric_list.add_metric(LatencyDistributionMetric(metric_value=request.request_time, metric_percentile=quantile_rank, metric_iteration=0).with_load_generator(load_generator=self, start_ts=min_ts, end_ts=max_ts), metric_groups)
            latency_distribution_breakdown = [
                (metric_object, quantile_rank, metric_value)
                for quantile_rank, request in latencies_of_requests
                for metric_object, metric_value in request.breakdown.items()
                if metric_object in LATENCY_DISTRIBUTION_METRIC_OBJECTS
            ]
            for metric_object, quantile_rank, metric_value in latency_distribution_breakdown:
                metric_list.add_metric(LatencyDistributionBreakdownMetric(metric_object=metric_object, metric_value=metric_value, metric_percentile=quantile_rank, metric_iteration=0).with_load_generator(load_generator=self, start_ts=min_ts, end_ts=max_ts), metric_groups)

    async def _send_persistent_request(self, url: str, curl_idx: int, resolve: Optional[str], no_proxy: list[str]) -> tuple[str, float, float]:
        """Sends a request to the target endpoint, maintaining persistent connection

        :param url: target endpoint url
        :param curl_idx: order of the persistent request
        :param resolve: preresolution of the domain name
        :param no_proxy: list of no proxy entries
        :return: decoded output, start and end timestamps
        """
        # Create a connection and set pre-resolution and no proxy
        connection = self.get_persistent_connection(url)
        connection.setopt(pycurl.RESOLVE, [resolve] if resolve else [])
        connection.setopt(pycurl.NOPROXY, ",".join(no_proxy))

        # Process headers for the request
        additional_http_headers = [
            f'opc-request-id: {time.time()}/{curl_idx}'
        ]
        additional_http_headers.extend(self.configuration.http_headers)
        connection.setopt(pycurl.HTTPHEADER, additional_http_headers)

        # Clear the buffer
        header_buffer = self._persistent_connection_buffers_map[url]
        header_buffer.truncate(0)
        header_buffer.seek(0)

        # Print rough equivalent of the curl that is sent
        curl_cmd = "curl"
        if resolve:
            curl_cmd += f" --resolve {resolve}"
        for header in additional_http_headers:
            curl_cmd += f" -H {header}"
        curl_cmd += f" {url}"
        logging.log(curl_cmd)

        # Send the request
        start_ts = time.time()
        connection.perform()

        # Get the headers
        headers = {}
        for header in header_buffer.getvalue().decode().splitlines():
            if ":" in header:
                header_name, header_value = header.split(":", 1)
                headers[header_name.strip()] = header_value.strip()

        # Reconstruct roughly the output of the curl
        output = (
            f" curl: {connection.getinfo(pycurl.TOTAL_TIME):.6f}s "
            f"code: {connection.getinfo(pycurl.HTTP_CODE)} "
            f"url: {connection.getinfo(pycurl.EFFECTIVE_URL)} "
            f"local_ip: {connection.getinfo(pycurl.LOCAL_IP)} "
            f"local_port: {connection.getinfo(pycurl.LOCAL_PORT)} "
            f"(namelookup: {connection.getinfo(pycurl.NAMELOOKUP_TIME):.6f}s, "
            f"connect: {connection.getinfo(pycurl.CONNECT_TIME):.6f}s, "
            f"appconnect: {connection.getinfo(pycurl.APPCONNECT_TIME):.6f}s, "
            f"pretransfer: {connection.getinfo(pycurl.PRETRANSFER_TIME):.6f}s, "
            f"redirect: {connection.getinfo(pycurl.REDIRECT_TIME):.6f}s, "
            f"starttransfer: {connection.getinfo(pycurl.STARTTRANSFER_TIME):.6f}s, "
            f"x_request_start: {headers.get('X-OCI-GRAAL-Request-Start', headers.get('x-oci-graal-request-start', ''))}, "
            f"tinyinit_activation: {headers.get('TINYINIT_ACTIVATION', headers.get('tinyinit_activation', ''))}, "
            f"tinyinit_fork: {headers.get('TINYINIT_FORK', headers.get('tinyinit_fork', ''))}, "
            f"tinyinit_exec: {headers.get('TINYINIT_EXEC', headers.get('tinyinit_exec', ''))}, "
            f"x_app_start: {headers.get('X-App-Start', headers.get('x-app-start', ''))}, "
            f"x_response_start: {headers.get('X-Response-Start', headers.get('x-response-start', ''))})"
        )
        logging.log(output.strip())
        end_ts = time.time()

        return output, start_ts, end_ts

    def _curl(self, url: str, curl_idx: int, timeout_sec: int = 10, resolve: Optional[str] = None) -> list[str]:
        """
        :param url: endpoint where the request is send in form of the URI
        :param curl_idx: order of the request as it is send (e.g., used to identify particular request's body)
        :param timeout_sec: timeout in seconds for the response; the response will return code 0
        :param resolve: manual DNS resolve in form of host:port:ip
           - this speeds up the curl time (we will not contact DNS resolver)
           - for dataplane and service deployments we can extract the IP of the endpoint before curling
        """
        resolve_opt = ['--resolve', resolve] if resolve else []
        # This is an additional header that is used to identify individual curl requests
        additional_http_headers = [
            '-H', f'opc-request-id: {time.time()}/{curl_idx}'
        ]
        if self.signed_requests:
            # We create a prepared signed request
            #  - contains url (this is not needed as it is the same)
            #  - contains additional authentication headers - these are passed to the curl
            signed_request = self.get_authorization_key('GET', url)
            for header, val in signed_request.headers.items():
                additional_http_headers.append("-H")
                header_value = val.decode("utf-8") if isinstance(val, bytes) else val
                additional_http_headers.append(f"{header}: {header_value}")
        for header in self.configuration.http_headers:
            additional_http_headers.append("-H")
            additional_http_headers.append(header)
        output_file = '/dev/null' if self.configuration.expects.body is None else os.path.join(self.scripts_dir(), f"curl-{curl_idx}.response")
        return [self.curl_path(), '--insecure', '--max-time', str(timeout_sec), '-w', f"@{self.curl_format_file()}", '-o', output_file, '-s'] + resolve_opt + additional_http_headers + [url]

    async def round_robin_request(
        self,
        target: RoundRobinTargetConfig,
        num_requests: int,
        endpoints_for_apps: Sequence[Tuple[str, BaseApp]],
        request_timeout: int = 10,
        agents: Optional[list[Agent]] = None,
    ) -> tuple[str, float, float]:
        """Performs (currently as part of the script) a series of requests in round-robin policy

        :param target: target round robin target configuration
        :param num_requests: number of sent round robin requests
        :param endpoint_for_apps: list of endpoints that are curled in round-robin policy
        :param request_timeout: timeout for individual requests
        :param agents: list of agents
        """
        # Sending requests in round robin policy
        #  - before sending next request we wait for the response
        endpoints_for_apps_len = len(endpoints_for_apps)
        start_ts = time.time()
        result = ''
        no_proxy_entries = list(set(sum([app.no_proxy_entries() for _, app in endpoints_for_apps], [])))
        for i in range(num_requests):
            # Between robins
            if i != 0 and i % endpoints_for_apps_len == 0:
                sleep_result, _, _ = await self.schedule_sleep_and_emits(target, target.between_rounds_sleep, "between-rounds", agents=agents)
                result += sleep_result
            endpoint, app = endpoints_for_apps[i % endpoints_for_apps_len]
            # Note: even though we might use /etc/hosts to resolve the services, keep this resolution here !!!
            # This has virtually no overhead, while /etc/hosts has some small overhead (probably due to some syscall)
            dns_resolve = app.curl_dns_resolve()
            if self.persistent_requests:
                # Persistent-connection request are send with the pycurl library, that uses libcurl
                curl_response, start_ts, _  = await self._send_persistent_request(endpoint, curl_idx=i, resolve=dns_resolve, no_proxy=no_proxy_entries)
            else:
                # Non-persistent-connection request are send just with the curl
                curl_cmd = self._curl(endpoint, curl_idx=i, resolve=dns_resolve, timeout_sec=request_timeout)
                curl_res, start_ts, _  = await self._run_cmd(curl_cmd, request_timeout, no_proxy=no_proxy_entries)
                curl_response = curl_res.stdout.decode('utf-8')
            result += f"{start_ts}{curl_response}\n"
            # Between requests
            if target.request_sleep_interval:
                await asyncio.sleep(target.request_sleep_interval)
        end_ts = time.time()
        return result, start_ts, end_ts

    async def spike_requests(
        self,
        _: SpikeTargetConfig,
        num_requests: int,
        endpoints_for_apps: Sequence[Tuple[str, BaseApp]],
        request_timeout: int = 10,
    ) -> tuple[str, float, float]:
        """Performs (currently as part of the script) a series of requests in a single spike

        :param target: target spike target configuration
        :param num_requests: number of sent round robin requests
        :param endpoint_for_apps: list of endpoints that are curled in one spike
        :param request_timeout: timeout for individual requests
        :param agents: list of agents
        """
        # Sending requests in spike policy
        #  - we use send the request at the same time not waiting for reply of the previous request
        endpoints_for_apps_len = len(endpoints_for_apps)
        tasks = []
        start_ts = time.time()
        no_proxy_entries = list(set(sum([app.no_proxy_entries() for _, app in endpoints_for_apps], [])))
        for i in range(num_requests):
            # Between robins
            endpoint, app = endpoints_for_apps[i % endpoints_for_apps_len]
            dns_resolve = app.curl_dns_resolve()
            assert not self.persistent_requests, "Sending request through persistent connection cannot be performed in spike!"
            curl_call = self._curl(endpoint, curl_idx=i, resolve=dns_resolve, timeout_sec=request_timeout)
            # This ensures two things:
            #  - $() ensures that the date and the curl output will be output on the same line
            #  - & ensures that the requests are sent at the same time
            tasks.append(self._run_cmd(curl_call, no_proxy=no_proxy_entries))
        end_ts = time.time()
        results = await asyncio.gather(*tasks)
        return "".join(f"{res[1]}{res[0].stdout.decode('utf-8')}\n" for res in results), start_ts, end_ts


    async def notify_agents(self, agents: list[Agent], event: str) -> tuple[str, float, float]:
        """Notifies all agents and collects the results.

        Each results of the agent is prepended with its type and index:

        smaps1: {smaps output}

        :param agents: list of agents
        :param event: event to tag the results
        """
        start_ts = time.time()
        results = await asyncio.gather(*[self._run_cmd(shlex.split(agent.callback(event))) for agent in agents])
        end_ts = time.time()
        return ''.join(f"{agents[i].type}{agents[i].idx}: {res[0].stdout.decode('utf-8')}" for i, res in enumerate(results)), start_ts, end_ts

    async def schedule_sleep_and_emits(
        self,
        target: RoundRobinTargetConfig | SpikeTargetConfig,
        sleep_duration: int,
        event: str,
        agents: Optional[list[Agent]],
    ) -> tuple[str, float, float]:
        """Helper function that appends sleeps and notify events to script

        :param target: target configuration
        :param script_lines: script that contains requests
        :param sleep_duration: duration of the sleep
        :param event: tag that identifies when we are sleeping
        """
        # No sleep
        start_ts = time.time()
        results = ''
        if sleep_duration <= 0:
            return results, start_ts, time.time()
        # If there are no agents we sleep for the full duration
        elif not agents or (target.agent_emit_interval_during_sleep <= 0):
            await asyncio.sleep(sleep_duration)
        # We ceil the number of sleeps according to the emit time interval;
        # Note: the sleep might not be as precise for some combinations of sleep_duration and emit_interval
        else:
            sleep_rate = math.ceil(sleep_duration / target.agent_emit_interval_during_sleep)
            for _ in range(0, sleep_rate):
                res, _, _ = await self.notify_agents(agents, event)
                results += res
                await asyncio.sleep(target.agent_emit_interval_during_sleep)
        return results, start_ts, time.time()


    async def requests(
        self,
        num_requests: int,
        success_threshold: float,
        endpoints_for_apps: Sequence[Tuple[str, BaseApp]],
        agents: list[Agent],
        target: RoundRobinTargetConfig | SpikeTargetConfig,
        delay: int = 0,
        timeout: int = 0,
        request_timeout: int = 10,
    ) -> MetricList:
        if delay > 0:
            await asyncio.sleep(delay)

        metric_list = MetricList(self)
        metric_groups = self.configuration.metric_groups
        endpoints_for_apps_len = len(endpoints_for_apps)
        result = ''

        # Pre-begin
        sleep_result, start_ts, _ = await self.schedule_sleep_and_emits(target, target.pre_begin_sleep, "pre-begin", agents=agents)
        result += sleep_result

        if isinstance(target, RoundRobinTargetConfig):
            round_robin_result, _, _ = await self.round_robin_request(target, num_requests, endpoints_for_apps, request_timeout, agents=agents)
            result += round_robin_result
        elif isinstance(target, SpikeTargetConfig):
            spike_result, _, _ = await self.spike_requests(target, num_requests, endpoints_for_apps, request_timeout)
            result += spike_result
        else:
            assert False, f"unknown 'request' policy: {target.__class__.__name__}"

        # Post-end
        sleep_result, _, end_ts = await self.schedule_sleep_and_emits(target, target.post_end_sleep, "post-end", agents=agents)
        result += sleep_result

        # If there are no metric groups, we terminate already and don't parse the results
        if not self.configuration.metric_groups:
            return metric_list

        # Parse the results
        timings = _parse_curl_outputs(result)

        if len(timings) < num_requests:
            raise ValueError(f"Number of parsed results does not match number of requests! Got {len(timings)} instead of {num_requests}")
        count_ok = 0
        http_counts: dict[int, int] = defaultdict(int)
        processed_cold_starts = set()
        for i, m in enumerate(timings):
            request_ts, time_sec, http_code, parsed_url = float(m["request_ts"]), m["curl"], int(m["http_code"]), m["url"]
            local_ip = m["local_ip"]
            local_port = int(m["local_port"]) if m["local_port"] != "" else None
            endpoint, app = endpoints_for_apps[i % endpoints_for_apps_len]
            assert parsed_url == endpoint
            metric_list.add_metric(RequestTimeMetric(metric_value=float(time_sec) * 1000.0, metric_iteration=i).with_app(app=app, app_endpoint=endpoint).with_request(http_code=http_code, local_ip=local_ip, local_port=local_port, request_ts=request_ts).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts), metric_groups)
            http_counts[http_code] += 1
            # Check if the returned HTTP code and the body is expected (if generators.expect was set)
            response_file = os.path.join(self.scripts_dir(), f"curl-{i}.response")
            code_ok, body_ok = self.configuration.expects.check(http_code, response_file)
            if code_ok and body_ok:
                count_ok += 1
            elif not self.configuration.expects.is_empty:
                logging.log(f"Load generator {self.configuration.unique_name} received incorrect response for request {i+1}:")
                if self.configuration.expects.code and not code_ok:
                    logging.log(f"  - expected HTTP code of the response '{self.configuration.expects.code}', got '{http_code}'")
                if self.configuration.expects.body and not body_ok:
                    if not os.path.exists(response_file):
                        logging.log(f"  - expected body of the response '{self.configuration.expects.body}', got no response")
                    else:
                        with open(response_file, 'r') as response_handle:
                            response = response_handle.read()
                        logging.log(f"  - expected body '{self.configuration.expects.body}', got '{response}'")

            # Add the breakdown of the 'request-time' for cold starts:
            #  - TINYINIT_ACTIVATION is set only once (when process is created)
            #  - We check for the request_ts being bigger than TINYINIT_ACTIVATION timestamp to identify fresh process
            #  - Note: older version of curl returns the string `%header{<HEADER_NAME>}` instead of headers (we skip the breakdown then)
            #  - The order of the timestamps and the appropriate breakdown of the times can be visualized as:
            #
            #                 TIMESTAMP | metric.object
            #
            #                request_ts *
            #                           |
            #                           * (curl starts)
            #                           |
            #                           | pretransfer
            # X-OCI-GRAAL-Request-Start *
            #                           | nginx-time
            #       TINYINIT_ACTIVATION *
            #                           |
            #             TINYINIT_FORK | tinyinit-time
            #                           |
            #             TINYINIT_EXEC *
            #                           | app-cold-start
            #               X-App-Start *
            #                           | app-post-cold-start
            #          X-Response-Start *
            #                           | response-time
            #                           |
            #                           * (curl ends)

            # The request is not a cold start unless this is a newly seen activation time.
            is_cold_start = False

            # If the request does not have the breakdown timestamps, we fall back to less fine-grained breakdown
            #  - if we have breakdown we output everything in the diagram above,
            #  - otherwise, we only ouput the times from the curl (pretransfer, connect, appconnect)
            has_breakdown_timestamps = m["tinyinit_activation"] and m["tinyinit_activation"] != r"%header{TINYINIT_ACTIVATION}"
            if has_breakdown_timestamps and m["tinyinit_activation"] not in processed_cold_starts:
                # Quite an hack, to detect cold-starts: we check if we have seen this activation and if we did, then we do not record the breakdown
                #  - We assume the precision is enough to identify all of the three cold starts for services
                #    - i.e. there won't be two different tinyinits with same activation time, but different PID
                #  - We might lose some cold starts, though
                processed_cold_starts.add(m["tinyinit_activation"])
                is_cold_start = True

            if m["pretransfer"]:
                namelookup_time = float(m["namelookup"]) * 1000.0
                pretransfer_time = float(m["pretransfer"]) * 1000.0
                connect_time = float(m["connect"]) * 1000.0
                appconnect_time = float(m["appconnect"]) * 1000.0
                curl_time = float(m["curl"]) * 1000.0

                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="namelookup", metric_value=namelookup_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="pretransfer-time", metric_value=pretransfer_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="connect-time", metric_value=connect_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="appconnect-time", metric_value=appconnect_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)

            if has_breakdown_timestamps and is_cold_start and m["x_request_start"] and m["tinyinit_activation"]:
                # X-OCI-GRAAL-Request-Start is in [s], we convert to [ms]
                x_request_time = float(m["tinyinit_activation"]) - float(m["x_request_start"]) * 1000.0
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="nginx-time", metric_value=x_request_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)

            # Collected both for non-cold starts and cold-starts, approximation of how long we stay in GraalOS
            #  - the X-OCI-GRAAL-Request-Start is set for each request
            #  - TINYINIT_* and X-App-Start are set only once, when the app cold-starts
            #  - we thus use X-Response-Start as second timestamp
            #  - this includes time it takes from nginx to be propagated to the app and before we start the reply
            node_time = None
            if has_breakdown_timestamps and m["x_request_start"] and m["x_response_start"]:
                node_time = float(m["x_response_start"]) - float(m["x_request_start"]) * 1000.0
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="node-time", metric_value=node_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
            if has_breakdown_timestamps and is_cold_start and m["tinyinit_activation"] and m["tinyinit_exec"]:
                tinyinit_time = float(m["tinyinit_exec"]) - float(m["tinyinit_activation"])
                activation_time = float(m["tinyinit_fork"]) - float(m["tinyinit_activation"])
                fork_time = float(m["tinyinit_exec"]) - float(m["tinyinit_fork"])
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="tinyinit-time", metric_value=tinyinit_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="tinyinit-activation-time", metric_value=activation_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="tinyinit-fork-time", metric_value=fork_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
            if has_breakdown_timestamps and is_cold_start and m["tinyinit_exec"] and m["x_app_start"]:
                app_cold_start = float(m["x_app_start"]) - float(m["tinyinit_exec"])
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="app-cold-start-time", metric_value=app_cold_start, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
            if has_breakdown_timestamps and is_cold_start and m["x_app_start"] and m["x_response_start"]:
                app_time = float(m["x_response_start"]) - float(m["x_app_start"])
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="app-post-cold-start-time", metric_value=app_time, metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)

            if has_breakdown_timestamps and node_time is not None:
                #  We add a guard to prevent negative values
                #  - since we have only ms-precision for node-time, we are actually aproximating the node-time, which can be bigger than it really is.
                #  - however, for curl, we get ns-precision, so potentially, we can get difference from the interval (-1, 0) for response/network-time
                #  - e.g. if node_time is 1ms, and curl_time is 0.5s, then response_time will be -0.5
                #  - the suitable guard would be probably to have max(curl_time - node_time - pretransfer_time, 0) and underapproximate the response/network-time
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="response-time", metric_value=max(curl_time - node_time - pretransfer_time, 0), metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
                # response_time = total-curl-time - (nginx-time + tiny-init-time + app-cold-start-time + app-post-cold-start-time + pretransfer)
                metric_list.add_metric(RequestTimeBreakdownMetric(metric_object="network-time", metric_value=max(curl_time - node_time, 0), metric_iteration=i).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_app(app=app, app_endpoint=endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_request(request_ts=request_ts, local_ip=local_ip, local_port=local_port, http_code=http_code), metric_groups)
                # network_time = total-curl-time - (nginx-time + tiny-init-time + app-cold-start-time + app-post-cold-start-time)

        expected_http_code = 200
        if self.configuration.expects and self.configuration.expects.code is not None:
            # the configuration may define another HTTP code for success
            expected_http_code = self.configuration.expects.code

        success = http_counts.get(expected_http_code, 0) >= success_threshold * sum(http_counts.values())
        for http_code, http_code_counts in http_counts.items():
            metric_list.add_metric(RequestCodeCountMetric(http_code=http_code, metric_value=http_code_counts).with_app(app=app).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts), metric_groups)
        metric_list.add_metric(LoadTesterSuccessMetric(metric_value=int(success)).with_app(app=app).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts), metric_groups)

        # If we are doing the function testing, we raise the exception, if the number of expected requests are lower than the number of requests
        if not self.configuration.expects.is_empty and count_ok < num_requests:
            # this is used for functional testing; otherwise the scenario terminates without issue and the success is saved in 'load-tester.success' field
            raise ValueError(f"Received non-200 HTTP responses during requests load generation! {http_counts}")

        # The outputs of the agents now needs to be parsed from the stdout
        for agent in agents:
            for agent_metric in agent.parse_agent_output(result):
                endpoint, app = endpoints_for_apps[agent.attached_to_app]
                metric_list.add_metric(agent_metric.with_app(app=app, app_endpoint=endpoint), agent.metric_groups)

        self._compute_latency_for_raw_requests(metric_list)

        return metric_list

    async def throughput(
        self,
        app_url: str,
        app: BaseApp,
        success_threshold: float,
        duration_sec: int = 600,
        threads: int = 4,
        connections: int = 10,
        delay: int = 0,
        iterations: int = 1,
    ) -> MetricList:
        if delay > 0:
            await asyncio.sleep(delay)

        wrk_params = ['-s', os.path.join(self.scripts_dir(), "http_codes.lua"), '-d', str(duration_sec), '-t', str(threads), '-c', str(connections)]
        cmd = build_wrk_cmd(self.wrk_path(), wrk_params, self.configuration.http_headers, app_url, app)
        wrk_config = {
            "options": ' '.join(wrk_params),
            "threads": threads,
            "connections": connections,
            "duration": duration_sec
        }
        res = MetricList(self)
        metric_groups = self.configuration.metric_groups
        success = True
        http_counts: dict[int, int] = defaultdict(int)
        load_generation_start_ts = time.time()
        for iteration in range(iterations):
            logging.log(f"Throughput iteration {iteration + 1}/{iterations} for load generator {self.id}")
            result, start_ts, end_ts = await self._run_cmd(cmd, timeout=10 * duration_sec, no_proxy=app.no_proxy_entries())
            # If there are no metric groups, we terminate already and don't parse the results
            if not self.configuration.metric_groups:
                continue
            output = result.stdout.decode('utf-8')
            ops_per_sec = parse_wrk(output)
            if ops_per_sec is not None:
                res.add_metric(ThroughputMetric(metric_value=ops_per_sec, metric_iteration=iteration).with_app(app=app, app_endpoint=app_url).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
            iteration_success, iteration_http_counts = parse_http_counts(output, success_threshold)
            success = success and iteration_success
            for http_code, http_code_counts in iteration_http_counts.items():
                http_counts[http_code] += http_code_counts
        load_generation_end_ts = time.time()
        for http_code, http_code_counts in http_counts.items():
            res.add_metric(RequestCodeCountMetric(http_code=http_code, metric_value=http_code_counts).with_app(app=app).with_load_generator(load_generator=self, start_ts=load_generation_start_ts, end_ts=load_generation_end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
        res.add_metric(LoadTesterSuccessMetric(metric_value=int(success)).with_app(app=app).with_load_generator(load_generator=self, start_ts=load_generation_start_ts, end_ts=load_generation_end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
        return res

    async def write_script(self, name: str, file_content: str) -> str:
        script_path = os.path.join(self.scripts_dir(), name)
        with open(script_path, "w") as script_file:
            script_file.write(file_content)
        logging.log(f"Wrote script at {script_path}")
        return script_path

    async def delete_script(self, script_path: str) -> None:
        logging.log(f"Deleting {script_path}...")
        os.unlink(script_path)

    async def parallel_throughput(
        self,
        endpoints_for_apps: Sequence[Tuple[str, BaseApp]],
        success_threshold: float,
        duration_sec: int = 600,
        threads: int = 4,
        connections: int = 10,
        delay: int = 0,
        iterations: int = 1,
    ) -> MetricList:
        if len(endpoints_for_apps) == 0:
            raise ValueError("Parallel throughput measurement require apps to apply load to!")
        dummy_url = endpoints_for_apps[0][0]
        dummy_app = endpoints_for_apps[0][1]

        if len(endpoints_for_apps) != threads and len(endpoints_for_apps) % threads != 0:
            raise ValueError(f"The number of apps should be identical or a multiple of the number of threads for evenly-distributed load! "
                             f"Got {len(endpoints_for_apps)} apps but {threads} threads.")

        if delay > 0:
            await asyncio.sleep(delay)
        endpoint_urls = [endpoint for endpoint, _ in endpoints_for_apps]
        quoted_endpoints = ['"' + url + '"' for url in endpoint_urls]
        lua_urls = 'urls = {' + ', '.join(quoted_endpoints) + '}'
        urls_script_name = "load-generator-urls.lua"
        script_path = await self.write_script(urls_script_name, lua_urls)

        # wrk requires a valid URL to be passed on the command line. Hence, the dummy URL.
        # This URL will be resolved but no request will ever be sent to it when using the parallel.lua script
        wrk_params = ['-d', str(duration_sec), '-t', str(threads), '-c', str(connections), '-s', os.path.join(self.scripts_dir(), "parallel.lua")]
        cmd = build_wrk_cmd(self.wrk_path(), wrk_params, self.configuration.http_headers, dummy_url, dummy_app)
        wrk_config = {
            "options": ' '.join(wrk_params),
            "threads": threads,
            "connections": connections,
            "duration": duration_sec
        }
        result = MetricList(self)
        metric_groups = self.configuration.metric_groups
        no_proxy_entries = list(set(sum([app.no_proxy_entries() for _, app in endpoints_for_apps], [])))
        success = True
        http_counts: dict[int, int] = defaultdict(int)
        load_generation_start_ts = time.time()
        for iteration in range(iterations):
            logging.log(f"Parallel throughput iteration {iteration + 1}/{iterations} for load generator {self.id}")
            cmd_result, start_ts, end_ts = await self._run_cmd(cmd, timeout=10 * duration_sec, no_proxy=no_proxy_entries)
            # If there are no metric groups, we terminate already and don't parse the results
            if not self.configuration.metric_groups:
                continue
            output = cmd_result.stdout.decode('utf-8')
            matches = re.findall(r"^Thread (\d+) throughput is (\d*\.?\d+) reqs/s for endpoint (https?://\S+)$", output, re.MULTILINE)
            if len(matches) < len(endpoints_for_apps):
                raise ValueError(f"Couldn't match individual throughputs! Found only {len(matches)}, but there are {len(endpoints_for_apps)} endpoints.")
            logging.log("Individual thread throughputs:")
            for idx, (thread_id, thread_throughput, thread_endpoint) in enumerate(matches):
                app_idx = idx % len(endpoints_for_apps)
                app = endpoints_for_apps[app_idx][1]
                assert endpoint_urls[app_idx] == thread_endpoint, f"Thread throughput parsing error: {endpoint_urls[app_idx]} != {thread_endpoint}"
                result.add_metric(ThroughputMetric(metric_value=float(thread_throughput), metric_iteration=iteration).with_app(app=app, app_endpoint=thread_endpoint).with_load_generator(load_generator=self, start_ts=start_ts, end_ts=end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
                logging.log(f"\t {float(thread_throughput):.3f} reqs/s for thread {thread_id} on {thread_endpoint}")

            iteration_success, iteration_http_counts = parse_http_counts(output, success_threshold)
            success = success and iteration_success
            for http_code, http_code_counts in iteration_http_counts.items():
                http_counts[http_code] += http_code_counts

        load_generation_end_ts = time.time()
        for http_code, http_code_counts in http_counts.items():
            result.add_metric(RequestCodeCountMetric(http_code=http_code, metric_value=http_code_counts).with_app(app=dummy_app).with_load_generator(load_generator=self, start_ts=load_generation_start_ts, end_ts=load_generation_end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
        result.add_metric(LoadTesterSuccessMetric(metric_value=int(success)).with_app(app=dummy_app).with_load_generator(load_generator=self, start_ts=load_generation_start_ts, end_ts=load_generation_end_ts).with_wrk(wrk_config=wrk_config), metric_groups)

        await self.delete_script(script_path)
        return result

    async def latency(
        self,
        app_url: str,
        app: BaseApp,
        success_threshold: float,
        duration_sec: int = 200,
        rate: int = 100,
        threads: int = 4,
        connections: int = 10,
        delay: int = 0,
        iterations: int = 1,
    ) -> MetricList:
        if delay > 0:
            await asyncio.sleep(delay)


        wrk_params = ['--latency', '-s', os.path.join(self.scripts_dir(), "http_codes_and_breakdown.lua"), '-d', str(duration_sec), '-R', str(rate), '-t', str(threads), '-c', str(connections)]
        cmd = build_wrk_cmd(self.wrk2_path(), wrk_params, self.configuration.http_headers, app_url, app)
        wrk_config = {
            "rate": rate,
            "options": ' '.join(wrk_params),
            "threads": threads,
            "connections": connections,
            "duration": duration_sec
        }
        metric_list = MetricList(self)
        metric_groups = self.configuration.metric_groups
        success = True
        http_counts: dict[int, int] = defaultdict(int)
        load_generation_start_ts = time.time()
        for iteration in range(iterations):
            logging.log(f"Latency iteration {iteration + 1}/{iterations} for load generator {self.id}")
            result, iteration_start_ts, end_ts = await self._run_cmd(cmd, timeout=10 * duration_sec, no_proxy=app.no_proxy_entries())
            output = result.stdout.decode('utf-8')
            latencies = parse_wrk2(output)
            latencies_breakdown = extract_latency_breakdown_from_wrk2_output(output)
            # If there are no metric groups, we terminate already and don't parse the results
            if not self.configuration.metric_groups:
                continue
            for percentile, latency in latencies:
                metric_list.add_metric(LatencyDistributionMetric(metric_value=latency, metric_percentile=percentile, metric_iteration=iteration).with_app(app=app).with_load_generator(load_generator=self, start_ts=iteration_start_ts, end_ts=end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
            for metric_object, percentile, latency in latencies_breakdown:
                metric_list.add_metric(LatencyDistributionBreakdownMetric(metric_object=metric_object, metric_value=latency, metric_percentile=percentile, metric_iteration=iteration).with_app(app=app).with_load_generator(load_generator=self, start_ts=iteration_start_ts, end_ts=end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
            iteration_success, iteration_http_counts = parse_http_counts(output, success_threshold)
            success = success and iteration_success
            for http_code, http_code_counts in iteration_http_counts.items():
                http_counts[http_code] += http_code_counts
        load_generation_end_ts = time.time()
        for http_code, http_code_counts in http_counts.items():
            metric_list.add_metric(RequestCodeCountMetric(http_code=http_code, metric_value=http_code_counts).with_app(app=app).with_load_generator(load_generator=self, start_ts=load_generation_start_ts, end_ts=load_generation_end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
        metric_list.add_metric(LoadTesterSuccessMetric(metric_value=int(success)).with_app(app=app).with_load_generator(load_generator=self, start_ts=load_generation_start_ts, end_ts=load_generation_end_ts).with_wrk(wrk_config=wrk_config), metric_groups)
        return metric_list

    async def run(self) -> MetricList:
        endpoints_for_apps = self.urls_and_apps_for(self.configuration.target)

        method = self.configuration.method

        if method.strategy == LoadStrategy.REQUESTS:
            assert isinstance(method, RequestsMethodConfig)
            target = self.configuration.target
            if isinstance(target, RoundRobinTargetConfig):
                return await self.requests(method.total_requests, self.configuration.success_threshold, endpoints_for_apps, self.agents, delay=self.configuration.delay, timeout=method.timeout, target=target, request_timeout=method.request_timeout)
            elif isinstance(target, SpikeTargetConfig):
                return await self.requests(method.total_requests, self.configuration.success_threshold, endpoints_for_apps, self.agents, delay=self.configuration.delay, timeout=method.timeout, target=target, request_timeout=method.request_timeout)
            else:
                raise ValueError(f"Unsupported target for requests: {target.__class__.__name__}")
        elif len(endpoints_for_apps) == 1:
            # Load will be applied on a single endpoint of a single app for the given load generator
            endpoint_url, graal_app = endpoints_for_apps[0]
            if method.strategy == LoadStrategy.THROUGHPUT:
                assert isinstance(method, ThroughputMethodConfig)
                return await self.throughput(endpoint_url, graal_app, self.configuration.success_threshold, duration_sec=method.duration, threads=method.threads, connections=method.connections, delay=self.configuration.delay, iterations=method.iterations)

            elif method.strategy == LoadStrategy.LATENCY:
                assert isinstance(method, LatencyMethodConfig)
                return await self.latency(endpoint_url, graal_app, self.configuration.success_threshold, duration_sec=method.duration, rate=method.rate, threads=method.threads, connections=method.connections, delay=self.configuration.delay, iterations=method.iterations)
            else:
                raise ValueError(f"Unsupported method: {method.__class__.__name__}")
        else:
            if isinstance(self.configuration.target, RoundRobinTargetConfig):
                logging.log("Round robin target configs still have to be implemented...")

            assert isinstance(self.configuration.target, ParallelTargetConfig)

            if method.strategy == LoadStrategy.THROUGHPUT:
                assert isinstance(method, ThroughputMethodConfig)
                return await self.parallel_throughput(endpoints_for_apps, self.configuration.success_threshold, duration_sec=method.duration, threads=method.threads, connections=method.connections, delay=self.configuration.delay, iterations=method.iterations)
            elif method.strategy == LoadStrategy.LATENCY:
                assert isinstance(method, LatencyMethodConfig)
                # Note that we could decide to allow this mode while discarding the latency results and return them as throughput numbers
                # This could be a useful mode to keep apps active with minimal load.
                raise ValueError("Targeting multiple endpoints for a single latency measurement is not allowed since it will produce meaningless results.")
            else:
                raise ValueError(f"Unsupported method: {method.__class__.__name__}")

    def url_and_app_for(self, app_idx: int, endpoint_idx: int) -> Tuple[str, BaseApp]:
        if app_idx >= len(self.apps):
            raise IndexError(f"App index {app_idx} out of bounds ! ({len(self.apps)} apps)")
        graal_app = self.apps[app_idx]
        assert graal_app.app_config.idx == app_idx
        if endpoint_idx > len(graal_app.app_config.endpoints):
            raise IndexError(
                f"Endpoint index {endpoint_idx} out of bounds ! ({len(graal_app.app_config.endpoints)} endpoints)")
        return graal_app.endpoint_url(graal_app.app_config.endpoints[endpoint_idx]), graal_app

    def urls_and_apps_for(self, target: TargetConfig) -> List[Tuple[str, BaseApp]]:
        if not self.apps:
            raise ValueError("Apps must be provided to run scenario-based load generator!")
        return [self.url_and_app_for(app_idx, endpoint_idx)
                for endpoint_idx in target.endpoint_indices
                for app_idx in target.app_indices
                ]

    def extras(self) -> dict[str, Any]:
        """
        Metadata for bench server collection with the 'extra.' prefix
        """
        return {}






class RemoteLoadGenerator(LoadGenerator):
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Remote load generation is unavailable in the local-only build.")

