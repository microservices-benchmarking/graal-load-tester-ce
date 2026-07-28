from __future__ import annotations

from typing import Any, Optional


class ListedApp:
    pass


async def fetch_apps(*_args: Any, **_kwargs: Any) -> list[ListedApp]:
    raise RuntimeError("This operation is unavailable in the local-only build.")
