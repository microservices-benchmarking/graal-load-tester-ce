from __future__ import annotations

from typing import Any


class LocalOnlyConfig:
    def __getattr__(self, _name: str) -> Any:
        raise RuntimeError("This deployment is unavailable in the local-only build.")


_config = LocalOnlyConfig()


def get_config() -> LocalOnlyConfig:
    return _config
