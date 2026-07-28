from __future__ import annotations

import subprocess
from collections import defaultdict

import time
import os
import socket
from typing import Optional, Any

from gos_harness.scenario_parser import AppConfig, LocalBinaryConfig, RunningKegBinaryConfig, RunningBinaryConfig, ScenarioConfig

SCENARIO_STATUS_SUCCESS = "success"
SCENARIO_STATUS_FAILED_SUCCESS_RATE = "failed-success-rate"
SCENARIO_STATUS_FAILED_TIMEOUT = "failed-timeout"
SCENARIO_STATUS_FAILED_EXCEPTION = "failed-exception"
SCENARIO_STATUS_FAILED_POSTPROCESSING = "failed-postprocessing"


class ScenarioExecutionContext:
    """The context for executing the scenario.

    Groups the parameters that are used through the orchestration as well as some temporary configurations.

    :ivar tmp_dir: temporary directory for storing temporary files
    :ivar registered_etc_host_entries: set of entries registered in /etc/hosts
    :ivar pid_register: PID registers map
      - Maps apps based on their unique identifiers to dictionary of particular processes (tinyinit, nginx, graalhost, etc.) to their PID
      - Each local app should be identified by a pair of (app-name, index) delimited by ','
      - Each local keg should be identified by its keg-id
      - Example of the PID register map: {
         "local-app-0": {'tinyinit': 1215},
         "serv0001": {'tinyinit': 14143}
      }
    :ivar scenario_id: id of the scenario that is run
    :ivar scenario_config: parsed configuration for the scenario
    :ivar load_generation: type of the load generation
      - LoadGeneration.Local = the load generation will be performed from the same machine
      - LoadGeneration.Remote = the load generation will be performed from freshly provisioned OCI instances
    :ivar upload_results: flag whether the results should be uploaded to the bench server
    :ivar log_service_apps: flag whether we should create service logs for the graal applications
    :ivar suite_name: name of the benchmarking suite
    :ivar suite_version: version of the benchmarking suite
    :ivar scenario_start_ts: timestamp when the scenario has started
    :ivar scenario_end_ts: timestamp when the scenario has ended
    """
    def __init__(
        self,
        *, # <- marks that the following options should be passed by keyword only (for readability)
        scenario_id: str,
        scenario_config: ScenarioConfig,
        load_generation: Any,
        upload_results: bool,
        log_service_apps: bool,
        suite_name: Optional[str],
        suite_version: Optional[str],
        scenario_start_ts: float,
    ):
        # Immutable, internal state containers
        self._tmp_dir: str = os.path.join(os.getenv("TMP_DIR", "/tmp"), f"gos-{os.getpid()}-{scenario_id}")
        self._registered_etc_host_entries: set[str] = set()
        self._pid_register: dict[str, dict[str, int]] = defaultdict(dict)

        # Scenario run parameters (readonly via properties)
        self._scenario_config: ScenarioConfig = scenario_config
        self._load_generation: Any = load_generation  # Set to Any to avoid circular import on LoadGeneration
        self._upload_results: bool = upload_results
        self._log_service_apps: bool = log_service_apps
        self._suite_name: Optional[str] = suite_name
        self._suite_version: Optional[str] = suite_version
        self._scenario_start_ts: float = scenario_start_ts
        self._scenario_end_ts: Optional[float] = None
        self._scenario_status: str = "unknown"

    @property
    def tmp_dir(self) -> str:
        return self._tmp_dir

    @property
    def registered_etc_host_entries(self) -> set[str]:
        return self._registered_etc_host_entries

    @property
    def pid_register(self) -> dict[str, dict[str, int]]:
        return self._pid_register

    # Readonly scenario parameters
    @property
    def scenario_config(self) -> ScenarioConfig:
        return self._scenario_config

    @property
    def load_generation(self) -> Any:
        return self._load_generation

    @property
    def upload_results(self) -> bool:
        return self._upload_results

    @property
    def log_service_apps(self) -> bool:
        return self._log_service_apps

    @property
    def suite_name(self) -> Optional[str]:
        return self._suite_name

    @property
    def suite_version(self) -> Optional[str]:
        return self._suite_version

    @property
    def scenario_start_ts(self) -> float:
        return self._scenario_start_ts

    @property
    def scenario_end_ts(self) -> Optional[float]:
        return self._scenario_end_ts

    @property
    def scenario_status(self) -> str:
        return self._scenario_status

    def set_scenario_status(self, status: str) -> None:
        self._scenario_status = status

    def mark_benchmarking_as_finished(self) -> None:
        """Records the end timestamp of benchmarking. Can only be called once."""
        if self._scenario_end_ts is not None:
            raise RuntimeError("Scenario end timestamp already set")
        self._scenario_end_ts = time.time()

    def get_pid(self, app_config: AppConfig) -> int:
        """Obtains the pid from the pid_register for the given app

        1. The local binaries are identified by pair of (app_name, idx)
        2. The local kegs are identified by its keg_id (the service name)

        We consider tinyinit as the base point of the attachement, it spawns the additional processes.
        The agents themselves have to take care of looking up the child PIDs.

        TODO: Consider extracting to RunningApp class

        :param app_config: configuration of the app
        :param PID corresponding to the tinyinit of the given app.
        """
        if isinstance(app_config.binary, RunningKegBinaryConfig):
            return self.pid_register[app_config.binary.keg_id]['tinyinit']
        elif isinstance(app_config.binary, LocalBinaryConfig):
            return self.pid_register[app_config.unique_name]['tinyinit']
        elif isinstance(app_config.binary, RunningBinaryConfig):
            pid = _get_listening_pid(app_config.binary.base_url, app_config.port)
            if pid is None:
                raise RuntimeError(f"Could not find PID listening on {app_config.binary.base_url}:{app_config.port}")
            return pid
        else:
            assert False, f"Obtaining PID for {type(app_config.binary)} is unsupported"


def _get_listening_pid(host: str, port: int) -> Optional[int]:
    try:
        result = subprocess.run(["lsof", "-t", "-i", f"TCP@{host}:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if line.strip().isdigit():
                return int(line.strip())
    except OSError:
        pass
    return _get_listening_pid_from_proc(host, port)


def _get_listening_pid_from_proc(host: str, port: int) -> Optional[int]:
    inodes = _listening_socket_inodes(host, port)
    if not inodes:
        return None
    for pid in sorted(name for name in os.listdir("/proc") if name.isdigit()):
        fd_dir = os.path.join("/proc", pid, "fd")
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    return int(pid)
        except OSError:
            continue
    return None


def _listening_socket_inodes(host: str, port: int) -> set[str]:
    host_addrs = _proc_net_host_addresses(host)
    port_hex = f"{port:04X}"
    inodes: set[str] = set()
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_file) as fp:
                next(fp, None)
                for line in fp:
                    fields = line.split()
                    if len(fields) < 10:
                        continue
                    local_address, local_port = fields[1].split(":")
                    state = fields[3]
                    if local_port == port_hex and state == "0A" and _proc_net_address_matches(local_address, host_addrs):
                        inodes.add(fields[9])
        except OSError:
            continue
    return inodes


def _proc_net_host_addresses(host: str) -> set[str]:
    addresses: set[str] = set()
    try:
        addresses.add(socket.inet_aton(host)[::-1].hex().upper())
    except OSError:
        pass
    try:
        addresses.add(socket.inet_pton(socket.AF_INET6, host)[::-1].hex().upper())
    except OSError:
        pass
    return addresses


def _proc_net_address_matches(proc_address: str, host_addrs: set[str]) -> bool:
    return proc_address in host_addrs or proc_address in {"00000000", "00000000000000000000000000000000"}
