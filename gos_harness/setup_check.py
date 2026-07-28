from __future__ import annotations

import importlib
import os
import sys

from gos_harness import gos_logging as logging


def die(reason: str) -> None:
    logging.log(f"!!! ERROR: Requirements not met: {reason}")
    sys.exit(1)


def which(program: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, program)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def verify_requirements(
    check_oci_cli: bool = False,
    check_load_generation: bool = True,
    check_uploader: bool = False,
    check_remote_load_generators: bool = False,
    check_local_runners: bool = True,
    check_local_gvisor: bool = False,
    check_local_firecracker: bool = False,
    check_dataplane: bool = False,
    check_docker: bool = False,
    check_signed_request_support: bool = False,
) -> None:
    if any((check_oci_cli, check_uploader, check_remote_load_generators, check_dataplane,
            check_docker, check_signed_request_support)):
        die("The requested non-local capability is unavailable in this build.")
    commands: list[str] = []
    if check_load_generation:
        commands += ["wrk", "wrk2", "curl"]
    if check_local_runners:
        commands += ["tinyinit", "nginx"]
    if check_local_gvisor:
        commands += ["runsc"]
    if check_local_firecracker:
        commands += ["firecracker"]
    for command in commands:
        location = which(command)
        if location is None:
            die(f"'{command}' must be on PATH")
        logging.log(f" - found {command}: {location}")
    if check_local_firecracker:
        if not os.path.exists("/dev/kvm"):
            die("'/dev/kvm' must exist")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            die("'/dev/kvm' must be readable and writable")


def require_package(package_name: str) -> None:
    try:
        importlib.import_module(package_name)
    except ImportError:
        die(f"Cannot import '{package_name}'")
