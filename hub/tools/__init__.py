"""MCP tool modules. Each exposes ``register(mcp, hub)``.

Docstring rule for every tool here: FastMCP sends only the prose ABOVE
``Args:`` as the tool description, so the return shape and any caveat an
agent needs goes above it. tests/test_tools.py pins that for every tool.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from hub.http import Http
from hub.store import Store

log = logging.getLogger("hiring-hub.tools")


@dataclass
class Hub:
    store: Store
    http: Http
    legacy: Any = None  # clients.tracker.TrackerDB when the old Anthropic DB is mounted
    background: dict = field(default_factory=dict)  # the running or last large sync


def fmt(data: object) -> str:
    return json.dumps(data, indent=2, default=str)


def tool_guard(func: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Backstop: turn any escaping exception into the ``{error, detail}`` contract.

    FastMCP would catch a raising tool and return an isError result, and the
    session survives; but the caller then gets a framework-shaped error rather
    than the documented envelope, and an agent parsing for "error" treats a
    hard failure as an unreadable answer. ValueError is how tools here reject
    bad input, so it is reported as the caller's mistake rather than a crash.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except ValueError as exc:
            return fmt({"error": "Invalid input", "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - catching everything IS the backstop
            log.error("tool %s raised %s: %s", func.__name__, type(exc).__name__, exc)
            return fmt({
                "error": f"Unexpected failure in {func.__name__}",
                "detail": f"{type(exc).__name__}: {exc}",
            })

    return wrapper
