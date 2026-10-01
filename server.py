"""MCP Anthropic Tracker - access to Anthropic hiring data.

Two data sources, ten tools total:

* 7 cached-DB tools (read-only SQLite mount, populated by the nightly
  `anthropic-tracker` cron container on nix1).
* 3 live tools that hit Greenhouse's public board API directly so you can
  bypass the cache when freshness matters.

The DB is mounted read-only at the volume layer (`anthropic-tracker-data:/data:ro`)
and opened read-only at the SQLite driver layer (`file:...?mode=ro`) for defense
in depth. The live tools never touch the DB.
"""

import functools
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import fastmcp
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

from clients.greenhouse import (
    GreenhouseClient,
    build_department_map,
    enrich_jobs_with_departments,
)
from clients.parser import parse_compensation
from clients.tracker import TrackerDB, _escape_like

load_dotenv()

# --- Config ---
DB_PATH = os.getenv("TRACKER_DB_PATH", "/data/tracker.db")

if not os.path.exists(DB_PATH):
    print(
        f"FATAL: tracker DB not found at {DB_PATH}.\n"
        "Mount the `anthropic-tracker-data` volume read-only at /data:\n"
        "  -v anthropic-tracker-data:/data:ro\n",
        file=sys.stderr,
    )
    sys.exit(1)

db = TrackerDB(DB_PATH)

# Single Greenhouse client reused across tool calls for connection pooling.
greenhouse = GreenhouseClient()

# --- MCP Server ---
mcp = FastMCP("Anthropic Tracker")

log = logging.getLogger("anthropic-tracker.tools")


def _format(data: object) -> str:
    """Format response data as readable JSON string."""
    return json.dumps(data, indent=2, default=str)


def _greenhouse_error(exc: Exception) -> dict:
    """Shape an httpx exception into a structured tool response."""
    return {
        "error": "Greenhouse API unavailable",
        "detail": f"{type(exc).__name__}: {exc}",
    }


def tool_guard(func: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Backstop: turn any escaping exception into the declared error contract.

    Every tool here documents what it returns on failure, and the live tools
    handle the failures they expect. This catches what neither anticipated, so
    the documented contract is what a caller actually gets rather than what the
    tool meant to give them.

    Two concrete escapes motivated it:

    * a **non-JSON 200** from Greenhouse. Every live tool calls .json() on the
      response, which raises a JSONDecodeError -- not an httpx error, so not in
      any of the caught tuples. A captive-portal login page, an HTML error page
      from a CDN, or a truncated body all produce exactly this.
    * a **sqlite3.Error** from any of the seven cached-DB tools, none of which
      catch anything at all. The DB is a read-only mount populated by a cron
      container on another host; a partial write, a schema change, or a missing
      file surfaces here and nowhere else.

    Order matters below: the specific handlers inside each tool still run first
    and still produce their own richer messages ("Job not found" and its
    job_id). This only sees what got past them.

    On the "would crash the MCP session" claim this replaces: it is false, and
    worth correcting rather than deleting. FastMCP catches a raising tool and
    returns an isError result; the session survives. What actually happens is
    that the caller gets a framework-shaped error instead of the documented
    envelope -- so an agent parsing for "error" finds nothing it recognises and
    treats a hard failure as an unreadable response. Less dramatic than a crash,
    and more likely to be acted on wrongly, because it looks like the tool
    answered.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - catching everything IS the backstop
            log.error(
                "tool %s raised %s: %s", func.__name__, type(exc).__name__, exc
            )
            return _format(
                {
                    "error": f"Unexpected failure in {func.__name__}",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

    return wrapper


# ============================================================
# Cached DB tools (read-only)
# ============================================================


@mcp.tool()
@tool_guard
async def search_jobs(
    query: str,
    department: str | None = None,
    active_only: bool = True,
) -> str:
    """Search Anthropic job postings by title.

    Comma-separated terms are OR-joined (e.g. "engineer, manager" matches
    either). Optional department filter (substring match, case-insensitive).

    Args:
        query: Comma-separated title search terms. Required.
        department: Optional department name substring (e.g. "Research").
        active_only: If True (default), only currently-active jobs are returned.

    Returns:
        JSON list of matching jobs with id, title, department, location, url, first_seen.
    """
    results = db.search_jobs(query, department=department, active_only=active_only)
    return _format({"count": len(results), "jobs": results})


@mcp.tool()
@tool_guard
async def recent_changes(days: int = 7) -> str:
    """List jobs added and removed in the last N days.

    Args:
        days: Lookback window in days (default 7, max 365).

    Returns:
        JSON with `added` and `removed` arrays of jobs, each tagged with the
        first_seen / removed_date that triggered inclusion.
    """
    return _format(db.recent_changes(days=days))


@mcp.tool()
@tool_guard
async def compensation_for(role_pattern: str) -> str:
    """Look up posted compensation for jobs matching a title pattern.

    Salaries returned in dollars (DB stores cents internally).

    Args:
        role_pattern: Substring of the job title (e.g. "research engineer").

    Returns:
        JSON list of jobs with min/max salary, currency, comp_type, raw_text.
    """
    matches = db.compensation_for(role_pattern)
    return _format({"pattern": role_pattern, "count": len(matches), "matches": matches})


@mcp.tool()
@tool_guard
async def department_trends(name: str | None = None, days: int = 30) -> str:
    """Per-department active job counts over the last N days.

    Args:
        name: Optional department-name substring filter (case-insensitive).
              If omitted, returns every department in the snapshots.
        days: Lookback window in days (default 30, max 365).

    Returns:
        JSON with each department's date->count series sorted ascending by date.
    """
    return _format(db.department_trends(name=name, days=days))


@mcp.tool()
@tool_guard
async def active_alerts(severity: str | None = None) -> str:
    """List unacknowledged tracker alerts.

    Args:
        severity: Optional filter — one of "info", "warning", "critical".

    Returns:
        JSON list of alerts (id, triggered_at, type, severity, message).
    """
    alerts = db.active_alerts(severity=severity)
    return _format({"count": len(alerts), "alerts": alerts})


@mcp.tool()
@tool_guard
async def daily_summary(date: str | None = None) -> str:
    """Snapshot of total/added/removed jobs and per-department/location counts.

    Args:
        date: ISO date (YYYY-MM-DD). Default: latest available snapshot.

    Returns:
        JSON with date, total_active_jobs, jobs_added, jobs_removed, departments,
        locations. Returns `null` if no snapshot exists for that date.
    """
    summary = db.daily_summary(date=date)
    if summary is None:
        return _format({"date": date, "found": False})
    return _format(summary)


@mcp.tool()
@tool_guard
async def db_stats() -> str:
    """Database health snapshot: row counts per table, latest snapshot, totals.

    Returns:
        JSON with table_row_counts, active_jobs, latest_snapshot_date,
        total_snapshots, db_path, read_only.
    """
    return _format(db.db_stats())


# ============================================================
# Live Greenhouse tools (no cache, direct API)
# ============================================================


def _filter_live_jobs(
    jobs: list[dict],
    query: str | None,
    department: str | None,
) -> list[dict]:
    """Apply in-memory filters on a fetched job list.

    `query`: comma-separated terms, OR semantics, case-insensitive substring
             match against `title`. `_escape_like` is applied for consistency
             with the DB tools, even though we're matching in Python.
    `department`: exact match against any of the job's `departments[*].name`.
    """
    terms: list[str] = []
    if query:
        terms = [
            _escape_like(t.strip()).lower()
            for t in query.split(",")
            if t.strip()
        ]

    out: list[dict] = []
    for job in jobs:
        title = (job.get("title") or "").lower()
        if terms and not any(t in title for t in terms):
            continue
        if department:
            dept_names = [d.get("name") for d in (job.get("departments") or [])]
            if department not in dept_names:
                continue
        out.append(job)
    return out


def _shape_live_job(job: dict) -> dict:
    """Shape a Greenhouse job dict to match the DB tools' output shape."""
    departments = job.get("departments") or []
    dept_name = departments[0]["name"] if departments else "Unknown"
    location = job.get("location") or {}
    return {
        "id": job.get("id"),
        "title": job.get("title"),
        "department": dept_name,
        "location_raw": location.get("name"),
        "absolute_url": job.get("absolute_url"),
        "first_published": job.get("first_published"),
    }


@mcp.tool()
@tool_guard
async def live_jobs(
    query: str | None = None,
    department: str | None = None,
) -> str:
    """Fetch the live Anthropic job list directly from Greenhouse.

    Bypasses the nightly DB snapshot. Use this when you need the absolute
    latest postings (within minutes of being added/removed) instead of the
    cached `search_jobs` view.

    Args:
        query: Optional comma-separated title search terms (OR semantics,
               case-insensitive substring match against the job title).
        department: Optional exact match against the job's department name(s).

    Returns:
        JSON list of {id, title, department, location_raw, absolute_url,
        first_published}. On API failure: {error, detail}.
    """
    try:
        jobs = await greenhouse.fetch_jobs()
        # The bare /jobs endpoint omits department data. Pull /departments
        # separately and stitch the two together so the `department` filter
        # has something to match against.
        departments = await greenhouse.fetch_departments()
    except (
        httpx.HTTPStatusError,
        httpx.TransportError,
        httpx.TimeoutException,
    ) as exc:
        return _format(_greenhouse_error(exc))

    enrich_jobs_with_departments(jobs, build_department_map(departments))

    filtered = _filter_live_jobs(jobs, query, department)
    shaped = [_shape_live_job(j) for j in filtered]
    return _format({"count": len(shaped), "jobs": shaped})


@mcp.tool()
@tool_guard
async def live_job_detail(job_id: int) -> str:
    """Fetch a single Anthropic job with full HTML content from Greenhouse.

    Args:
        job_id: Greenhouse job ID.

    Returns:
        JSON {id, title, department, location, content, absolute_url}.
        On 404: {error: "Job not found", job_id}.
        On API failure: {error: "Greenhouse API unavailable", detail}.
    """
    try:
        job = await greenhouse.fetch_job_detail(job_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _format({"error": "Job not found", "job_id": job_id})
        return _format(_greenhouse_error(exc))
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        return _format(_greenhouse_error(exc))

    departments = job.get("departments") or []
    dept_name = departments[0]["name"] if departments else "Unknown"
    location = job.get("location") or {}

    return _format({
        "id": job.get("id"),
        "title": job.get("title"),
        "department": dept_name,
        "location": location.get("name"),
        "content": job.get("content") or "",
        "absolute_url": job.get("absolute_url"),
    })


@mcp.tool()
@tool_guard
async def live_compensation(job_id: int) -> str:
    """Parse compensation directly from a live Greenhouse job description.

    Hits the single-job endpoint, runs the same parser the nightly tracker
    uses against the returned HTML, and returns dollar-denominated values.

    Args:
        job_id: Greenhouse job ID.

    Returns:
        JSON {job_id, salary_min, salary_max, currency, comp_type, raw_text}
        on success (salaries in DOLLARS).
        On no salary detected: {job_id, compensation: null}.
        On 404: {error: "Job not found", job_id}.
        On API failure: {error: "Greenhouse API unavailable", detail}.
    """
    try:
        job = await greenhouse.fetch_job_detail(job_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _format({"error": "Job not found", "job_id": job_id})
        return _format(_greenhouse_error(exc))
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        return _format(_greenhouse_error(exc))

    parsed = parse_compensation(job.get("content") or "")
    if not parsed:
        return _format({"job_id": job_id, "compensation": None})

    # Parser returns CENTS — surface as dollars to match `compensation_for`.
    return _format({
        "job_id": job_id,
        "salary_min": parsed["salary_min"] // 100 if parsed.get("salary_min") else None,
        "salary_max": parsed["salary_max"] // 100 if parsed.get("salary_max") else None,
        "currency": parsed.get("currency", "USD"),
        "comp_type": parsed.get("comp_type", "annual"),
        "raw_text": parsed.get("raw_text"),
    })


# ============================================================
# Entry point
# ============================================================

#: Close a streamable-HTTP session after this long with no request in flight.
#: A client that disconnects without a DELETE (a killed process, a dropped
#: laptop lid) otherwise leaves its session and transport in memory forever.
SESSION_IDLE_TIMEOUT_SECONDS = 1800.0
_SESSION_IDLE_ENV = "FASTMCP_HTTP_SESSION_IDLE_TIMEOUT"


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
    # Honor both FASTMCP_* and legacy MCP_* env vars. FastMCP itself reads
    # FASTMCP_HOST / FASTMCP_PORT from the environment when it sets up the
    # streamable-http listener, so we mirror MCP_* into those names too.
    host = os.getenv("FASTMCP_HOST", os.getenv("MCP_HOST", "0.0.0.0"))
    port = int(os.getenv("FASTMCP_PORT", os.getenv("MCP_PORT", "3713")))
    os.environ["FASTMCP_HOST"] = host
    os.environ["FASTMCP_PORT"] = str(port)
    print(f"Starting MCP Anthropic Tracker on {host}:{port} (Streamable HTTP transport)")
    print(f"DB (read-only): {DB_PATH}")
    print("Live source: https://boards-api.greenhouse.io/v1/boards/anthropic")
    _apply_session_idle_timeout()
    mcp.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
