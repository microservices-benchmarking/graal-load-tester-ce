from __future__ import annotations

import argparse
from enum import Enum
from typing import Any, Optional


class GraalOSEnvironment(Enum):
    DEV = "dev"
    RB = "rb"
    PREPROD = "preprod"


class Cluster:
    def __init__(self, *args: Any, **kwargs: Any):
        self.args = args
        self.kwargs = kwargs


class LocalOnlyConfig:
    auth: Optional[str] = None
    profile: Optional[str] = None
    remote_oci = False

    @staticmethod
    def add_auth_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        return parser

    def configure_from_scenario_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set_scenario_config(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("This deployment is unavailable in the local-only build.")

    def __getattr__(self, _name: str) -> Any:
        raise RuntimeError("This deployment is unavailable in the local-only build.")


_config = LocalOnlyConfig()


def get_config() -> LocalOnlyConfig:
    return _config
