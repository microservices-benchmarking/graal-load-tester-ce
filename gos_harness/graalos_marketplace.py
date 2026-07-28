from __future__ import annotations

from typing import Any

from gos_harness.gos_oci import BaseApp


class GraalOSStack:
    @classmethod
    async def create_from_config(cls, *_args: Any, **_kwargs: Any) -> "GraalOSStack":
        raise RuntimeError("This deployment is unavailable in the local-only build.")


class GraalOSStackApp(BaseApp):
    pass
