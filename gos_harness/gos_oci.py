from __future__ import annotations

import functools
import subprocess
from typing import Any, Optional

from gos_harness import gos_logging as logging
from gos_harness.scenario_parser import AppConfig, LocalBinaryConfig


@functools.lru_cache(maxsize=None)
def _runtime_version(binary_name: str) -> Optional[str]:
    try:
        result = subprocess.run(
            [binary_name, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as err:
        logging.log(f"WARNING: Failed to get {binary_name} version: {err}")
        return None
    if result.returncode != 0:
        return None
    first_line = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    if binary_name == "firecracker" and first_line.startswith("Firecracker "):
        return first_line.removeprefix("Firecracker ").strip()
    if binary_name == "runsc" and first_line.startswith("runsc version "):
        return first_line.removeprefix("runsc version ").strip()
    return first_line or None


class BaseApp:
    def __init__(self, app_config: AppConfig):
        self._app_config = app_config

    @property
    def app_config(self) -> AppConfig:
        return self._app_config

    @property
    def index(self) -> int:
        return self.app_config.idx

    @property
    def deployment(self) -> str:
        return self.app_config.deployment

    @property
    def app_name(self) -> str:
        return self.app_config.app_name

    @property
    def unique_name(self) -> str:
        return self.app_config.unique_name

    @property
    def memory(self) -> int:
        return self.app_config.memory

    @property
    def ocpus(self) -> float:
        return self.app_config.ocpus

    @property
    def app_type(self) -> str:
        return self.app_config.type

    @property
    def base_url(self) -> str:
        raise NotImplementedError

    def endpoint_url(self, endpoint: Optional[str]) -> str:
        if not endpoint:
            return self.base_url
        return self.base_url + endpoint if endpoint.startswith("/") else f"{self.base_url}/{endpoint}"

    def curl_dns_resolve(self) -> Optional[str]:
        return None

    def no_proxy_entries(self) -> list[str]:
        return []

    def metric_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "app.index": self.index,
            "app.name": self.app_name,
            "app.type": self.app_type,
            "app.memory": self.memory,
            "app.ocpus": self.ocpus,
            "app.deployment": self.deployment,
        }
        if self.app_config.version:
            fields["app.version"] = self.app_config.version
        if self.deployment == "nginx-tinyinit-firecracker":
            version = _runtime_version("firecracker")
            if version:
                fields["extra.firecracker-version"] = version
        elif self.deployment == "nginx-tinyinit-gvisor":
            version = _runtime_version("runsc")
            if version:
                fields["extra.gvisor-version"] = version
        return fields


class LocalBinaryApp(BaseApp):
    def __init__(self, app_config: AppConfig):
        super().__init__(app_config)
        binary = app_config.binary
        assert isinstance(binary, LocalBinaryConfig)
        port = f":{app_config.port}" if app_config.port != 80 else ""
        self._base_url = f"{app_config.protocol}://{binary.base_url}{port}"

    @property
    def base_url(self) -> str:
        return self._base_url

    def __str__(self) -> str:
        return f"{self.__class__.__name__}(url = {self.base_url})"


class RunningApp(BaseApp):
    pass


class KegBinaryApp(BaseApp):
    pass


class DeployedGraalApp(BaseApp):
    pass


def num_disposable_instances() -> int:
    return 0


async def terminate_disposable_instances() -> None:
    return None


async def refresh_token(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("This operation is unavailable in the local-only build.")


def create_signed_request(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("Signed requests are unavailable in the local-only build.")


def __getattr__(_name: str) -> Any:
    raise AttributeError(_name)
