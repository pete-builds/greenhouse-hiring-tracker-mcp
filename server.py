"""Hiring Hub MCP server: every public ATS job board, one store, one pipeline.

Started life as the Anthropic Greenhouse tracker. It now tracks any number of
companies across twelve applicant tracking systems, searches multi-company
boards live, ranks jobs against a job seeker's profile, and keeps the
application pipeline, all from one writable SQLite file.

Configuration is environment only (see .env.example):

* HUB_DB_PATH: the hub database. Default /hub/hub.db in the container,
  ~/.hiring-hub/hub.db elsewhere.
* HUB_SYNC_INTERVAL_HOURS: background re-sync age (default 12; 0 disables).
* HUB_AUTO_SEED: import the bundled starter companies into an empty store (default 1).
* HUB_PROFILE_PATH: a profile JSON or YAML file loaded when none is stored yet.
* TRACKER_DB_PATH: the original Anthropic tracker DB; enables the
  anthropic_* history tools when the file exists. Optional.
* MCP_TRANSPORT: http (default in the image) or stdio.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

import fastmcp
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from hub import __version__
from hub.http import Http
from hub.store import Store
from hub.sync import background_loop
from hub.tools import Hub, companies, jobs, legacy, pipeline

# Under stdio the working directory belongs to whichever project the MCP
# client was started in, so its .env is someone else's config. Read a .env
# only when running as a server (the container, or `python server.py`).
_STDIO = os.getenv("MCP_TRANSPORT", "").lower() == "stdio" or Path(sys.argv[0]).name == "hiring-hub"
if not _STDIO:
    load_dotenv()
log = logging.getLogger("hiring-hub")

#: Close a streamable-HTTP session after this long with no request in flight.
#: A client that disconnects without a DELETE (a killed process, a dropped
#: laptop lid) otherwise leaves its session and transport in memory forever.
SESSION_IDLE_TIMEOUT_SECONDS = 1800.0
_SESSION_IDLE_ENV = "FASTMCP_HTTP_SESSION_IDLE_TIMEOUT"

INSTRUCTIONS = """\
Hiring Hub tracks job boards across Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, Recruitee, BambooHR, Rippling, Workday, Teamtailor, Breezy and
Personio, and searches Himalayas, Remote OK and HN Who is Hiring live.
Typical loop: set_profile once; add_company / discover_company / import_companies
to choose boards; whats_new or top_matches daily; job_detail(recheck=true)
before writing an application; track_application after the person applies;
pipeline_summary weekly. The hub never submits applications or contacts anyone.
"""


def _default_db_path() -> str:
    if os.getenv("HUB_DB_PATH"):
        return os.environ["HUB_DB_PATH"]
    if Path("/hub").is_dir():
        return "/hub/hub.db"
    return str(Path.home() / ".hiring-hub" / "hub.db")


def _load_profile_file(path: str) -> dict:
    text = Path(path).expanduser().read_text()
    if path.endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError(f"{path}: profile must be a mapping")
    return data


def _open_legacy():
    path = os.getenv("TRACKER_DB_PATH", "/data/tracker.db")
    if not os.path.exists(path):
        return None
    from clients.tracker import TrackerDB

    return TrackerDB(path)


def build_hub() -> Hub:
    db_path = _default_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    profile_path = os.getenv("HUB_PROFILE_PATH")
    if profile_path and store.get_profile() is None:
        store.set_profile(_load_profile_file(profile_path))
    if os.getenv("HUB_AUTO_SEED", "1") != "0" and not store.list_companies():
        from hub.discovery import parse_directory
        from hub.models import Company
        from hub.tools.companies import SEED_PATH

        for row in parse_directory(SEED_PATH.read_text()):
            store.add_company(Company(**row))
    return Hub(store=store, http=Http(), legacy=_open_legacy())


hub = build_hub()


@lifespan
async def _background_sync(server):
    interval = float(os.getenv("HUB_SYNC_INTERVAL_HOURS", "12"))
    task = None
    if interval > 0:
        task = asyncio.create_task(background_loop(hub.store, hub.http, interval))
    try:
        yield {}
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


mcp = FastMCP("Hiring Hub", instructions=INSTRUCTIONS, version=__version__, lifespan=_background_sync)
companies.register(mcp, hub)
jobs.register(mcp, hub)
pipeline.register(mcp, hub)
if hub.legacy is not None:
    legacy.register(mcp, hub)


def _apply_session_idle_timeout() -> None:
    """Reap idle sessions after 30 minutes unless the operator chose otherwise.

    The MCP SDK's session manager defaults to 1800 seconds, but FastMCP 4
    always passes its own ``http_session_idle_timeout`` setting through, and
    that setting defaults to None, which means never. Left alone, the server
    never reaps a session. An explicit FASTMCP_HTTP_SESSION_IDLE_TIMEOUT
    is already in the setting and is left untouched.
    """
    if _SESSION_IDLE_ENV not in os.environ:
        fastmcp.settings.http_session_idle_timeout = SESSION_IDLE_TIMEOUT_SECONDS


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stderr)
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return
    # Honor both FASTMCP_* and legacy MCP_* env vars. FastMCP itself reads
    # FASTMCP_HOST / FASTMCP_PORT from the environment when it sets up the
    # streamable-http listener, so we mirror MCP_* into those names too.
    host = os.getenv("FASTMCP_HOST", os.getenv("MCP_HOST", "0.0.0.0"))
    port = int(os.getenv("FASTMCP_PORT", os.getenv("MCP_PORT", "3713")))
    os.environ["FASTMCP_HOST"] = host
    os.environ["FASTMCP_PORT"] = str(port)
    print(f"Starting Hiring Hub {__version__} on {host}:{port} (Streamable HTTP transport)")
    print(f"Hub DB: {hub.store.path}")
    print(f"Legacy Anthropic history: {'on' if hub.legacy else 'off'}")
    _apply_session_idle_timeout()
    mcp.run(transport="streamable-http", host=host, port=port)


def cli() -> None:
    """Console-script entry point: stdio unless MCP_TRANSPORT says otherwise."""
    os.environ.setdefault("MCP_TRANSPORT", "stdio")
    main()


if __name__ == "__main__":
    main()
