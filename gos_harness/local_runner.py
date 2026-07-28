"""
This file consists of functions used for running of local apps.
"""
from __future__ import annotations

from types import FrameType
from typing import Callable, Optional, List, TextIO

import atexit
import json
import os
import json
import shutil
import signal
import shlex
import subprocess
import time
import sys
import toml
import asyncio

from subprocess import Popen
from pathlib import Path

from gos_harness.gos_oci import KegBinaryApp
from gos_harness.scenario_parser import AppConfig, LocalBinaryConfig, RunningKegBinaryConfig, LOCAL_APP_DEPLOYMENT_TYPES
from gos_harness.scenario_execution_context import ScenarioExecutionContext
from gos_harness import gos_logging as logging, async_cmd_runner
from gos_harness.config import dataplane_config, local_configs

dataplane = dataplane_config.get_config()

# Maximal number of times we retry running keg list, which might fail due to GRAALOS-4355
#  - this corresponds roughly to 30s gracefuly waiting for keg list not to fail
MAX_KEG_LIST_RETRIES: int = 30


class KegDeploymentException(Exception):
    """Fired when anything bad happens during the keg initialization"""
    def __init__(self, message: str):
        super().__init__(message)


def stop_nginx(app_tmp_dir: str) -> None:
    """Stops the nginx.

    :param app_tmp_dir: directory where temporary files are stored
    """
    nginx_prefix = os.path.join(app_tmp_dir, "nginx")
    nginx_config_file = f"{nginx_prefix}.conf"
    cmd = ["nginx", "-p", nginx_prefix, "-c", nginx_config_file, "-s", "stop"]
    process = Popen(cmd)
    # Wait for the process to finish
    process.communicate()


def kill_process(pid: int) -> None:
    """Gracefully kills process and its subprocesses

    :param pid: killed process
    """
    try:
        child_pids = subprocess.check_output(["pgrep", "-P", f"{pid}"]).splitlines()
        for child_pid in child_pids:
            try:
                os.kill(int(child_pid), signal.SIGTERM)
            # We ignore failures to kill
            except (subprocess.CalledProcessError, ProcessLookupError):
                pass
    except (subprocess.CalledProcessError, ProcessLookupError):
        pass
    finally:
        os.kill(pid, signal.SIGTERM)


def kill_em_all(
        app_tmp_dir: str,
        killed_dirs: list[str],
        killed_pids: list[int],
) -> None:
    """Cleans up resources after the load testing

    :param app_tmp_dir: directory where temporary files are stored
    :param killed_dirs: list of removed directories
    :param killed_pids list of pids to be killed
    """
    # We poll and wait for nginx to stop
    stop_nginx(app_tmp_dir)
    poll_file(os.path.join(app_tmp_dir, "nginx.pid"), 50, poll_condition=True)

    # Kill pids and its subprocesses and wait for sockets to be released
    for kpid in killed_pids:
        kill_process(kpid)

    # Remove directories
    for kdir in killed_dirs:
        shutil.rmtree(kdir)

def poll_file(polled_file: str, max_pols: int, poll_condition: bool = False) -> None:
    """Sleeps and wait until the poll_file is created and ready

    :param polled_file: file for which existence we are polling
    :param max_pols: maximum number of polls made to file, if exceeded, TimeoutError is raised
    :param poll_condition: condition we are polling each iteration
    """
    counter = 0
    while os.path.exists(polled_file) == poll_condition:
        if counter > max_pols:
            raise TimeoutError(f"Timeout for {'stopping' if poll_condition else 'starting'} the nginx+tinyinit expired (polled on {polled_file}")
        time.sleep(0.1)
        counter += 1

def wait_for_nginx(max_wait: int, config: AppConfig) -> None:
    """Sleeps and wait until the nginx is ready by sending a ping to /heartbeat endpoint

    :param max_wait: max time spent waiting for the nginx to start
    """
    counter = 0
    assert isinstance(config.binary, LocalBinaryConfig)
    logging.log(" - waiting for nginx to start running")
    while True:
        result = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"{config.protocol}://{config.binary.base_url}:{config.port}/heartbeat"], capture_output=True, text=True)
        status_code = result.stdout.strip()
        if status_code == "200":
            logging.log(" - nginx is running!")
            break
        if counter > max_wait:
            raise TimeoutError(f"Timeout for the nginx+tinyinit expired (no response from /heartbeat)")
        time.sleep(0.1)
        counter += 1

def run_local_binary_on_nginx_tinyinit(
    config: AppConfig,
    scenario_ctx: ScenarioExecutionContext,
    nginx_log_file: Optional[TextIO],
    tinyinit_log_file: Optional[TextIO]
) -> None:
    """Setups the local binary to be run on config.base_url:config.port using combination of tinytinit and nginx

    1. Setups the necessary variables, and creates configurations for tinyinit and nginx in temporary directory.
    2. Starts tinyinit which will ensures that the application is running and does not end after first request.
    3. Starts nginx that ensures that the application will get the request we are sending to the base_url:port/endpoint.
    4. Signals to other application that server is ready.
    5. Cleans up any mess.

    :param config: scenario for the local binary
    """
    logging.log(f"Launching local binary {config.unique_name}")
    # 1. Setup the variables
    assert isinstance(config.binary, LocalBinaryConfig), f"Supplied configuration is not LocalBinaryConfig but {type(config.binary)}"
    # Keep local paths short enough for Unix-domain socket limits.
    app_tmp_dir = f"{scenario_ctx.tmp_dir}-app-{config.idx}"
    os.makedirs(app_tmp_dir, exist_ok=True)
    os.makedirs(os.path.join(app_tmp_dir, "nginx", "logs"), exist_ok=True)
    nginx_prefix = os.path.join(app_tmp_dir, 'nginx')
    nginx_pid_file = f"{nginx_prefix}.pid"
    socket_file = os.path.join(app_tmp_dir, "app.sock")
    binary_command = config.binary.binary_command
    binary_command_parts = shlex.split(binary_command)
    if config.binary.unique_copy:
        binary_original, *binary_args = binary_command_parts
        binary_copy = str(Path(app_tmp_dir) / f"{Path(binary_original).name}-copy")
        # Read the contents of the original binary
        with open(binary_original, mode="rb") as binary_source:
            contents = binary_source.read()
        # Write the contents to binary copy
        with open(binary_copy, mode="wb") as binary_target:
            binary_target.write(contents)
        # Make the binary copy executable
        subprocess.run(["chmod", "+x", binary_copy])
        # Update the binary command to use the binary copy
        binary_command_parts = [binary_copy] + binary_args
        binary_command = shlex.join(binary_command_parts)

    # 2. Create configurations for tinyinit and nginx (and graalhost if deployment is appropriate)
    nginx_config_file = local_configs.build_nginx_config(
        config.unique_name,
        app_tmp_dir,
        nginx_prefix,
        socket_file,
        config.port,
        keepalive=config.binary.nginx_keepalive,
        keepalive_timeout=config.binary.nginx_keepalive_timeout,
        keepalive_requests=config.binary.nginx_keepalive_requests,
    )
    if config.deployment == "nginx-tinyinit":
        command = binary_command
    elif config.deployment == "nginx-tinyinit-graalhost":
        graalhost_config_file = local_configs.build_graalhost_config(app_tmp_dir, config.binary)
        command = f'graalhost --enable_resolving_env_refs --run_config=@{graalhost_config_file} --log_to=syslog --run {binary_command}'
    elif config.deployment == "nginx-tinyinit-gvisor":
        command = f'runsc --rootless --network=host do {binary_command}'
    elif config.deployment == "nginx-tinyinit-firecracker":
        bundle_script = str(Path(__file__).parent.parent / "scripts" / "firecracker" / "make-firecracker-bundle.sh")
        bundle_dir = str(Path(app_tmp_dir) / "firecracker")
        firecracker_binary, *firecracker_binary_args = binary_command_parts
        firecracker_kernel_image = config.binary.firecracker_kernel_image
        assert firecracker_kernel_image is not None
        firecracker_cmd: list[str] = [
            bundle_script,
            "--binary", firecracker_binary,
            "--kernel", firecracker_kernel_image,
            "--out-dir", bundle_dir,
            "--mem-mib", str(config.binary.firecracker_mem_mib),
            "--vcpu-count", str(config.binary.firecracker_vcpu_count),
        ]
        for firecracker_binary_arg in firecracker_binary_args:
            firecracker_cmd += ["--app-arg", firecracker_binary_arg]
        if config.binary.firecracker_rootfs_size_mib is not None:
            firecracker_cmd += ["--rootfs-size-mib", str(config.binary.firecracker_rootfs_size_mib)]
        res = subprocess.run(firecracker_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create Firecracker bundle for {config.unique_name}:\n{res.stdout}")
        command = str(Path(bundle_dir) / "run-bridge.sh")
    else:
        # Fail gracefully
        assert False, f"Local binary can only be run for app.deployment in {LOCAL_APP_DEPLOYMENT_TYPES}; got {config.deployment}"
    tinyinit_config_dir = local_configs.build_tinyinit_config(app_tmp_dir, command, socket_file, "WAIT" if config.type == "server" else "NOWAIT", config.binary.environment)

    # 3. Prepare for the run
    if os.path.exists(socket_file):
        os.remove(socket_file)
    if os.path.exists(nginx_pid_file):
        stop_nginx(app_tmp_dir)

    # 4. Start tinyinit
    env = os.environ.copy()
    env["RUST_LOG"] = "trace"
    # --no-syslog: by default, tinyinit logs to syslog, for which not every machine have permissions (e.g. in the CI); this option disables the logging.
    tinyinit_cmd: list[str] = ["tinyinit", "--no-syslog", "--no-block-signal", "--conf-d-path", tinyinit_config_dir]
    tiny_process = subprocess.Popen(tinyinit_cmd, env=env, stderr=tinyinit_log_file)
    tinyinit_pid = tiny_process.pid
    poll_file(socket_file, 100)
    logging.log(" - tinyinit has started")

    # 5. Start nginx
    nginx_cmd: list[str] = ["nginx", "-p", nginx_prefix, "-c", nginx_config_file]
    subprocess.Popen(nginx_cmd, stderr=nginx_log_file)
    poll_file(nginx_pid_file, 100)

    # 6. Set up cleanup functions
    def on_exit() -> None:
        kill_em_all(app_tmp_dir, [app_tmp_dir], [tinyinit_pid])
    def handle_interrupt(signum: int, frame: Optional[FrameType]) -> None:
        logging.log(" - Interupted by user. Ending gracefully.")
        sys.exit(1)
    signal.signal(signal.SIGINT, handle_interrupt)
    atexit.register(on_exit)

    # 7. Verify nginx setup
    wait_for_nginx(100, config)

    # 8. Register PIDs for the application
    scenario_ctx.pid_register[config.unique_name]['tinyinit'] = tinyinit_pid










