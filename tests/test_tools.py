"""The MCP surface: registry, descriptions, error contract, and one full loop.

server.py builds its hub at import from the environment, so each fixture
points HUB_DB_PATH at a fresh file, turns off the seed import and background
sync (no network in tests), and reloads the module.
"""

import importlib
import json
import sqlite3
import sys

import pytest

from hub.models import Company, Job
from tests.conftest import _build_snapshot

HUB_TOOLS = 23
LEGACY_TOOLS = 4


def _load_server(monkeypatch, tmp_path, legacy: bool):
    monkeypatch.setenv("HUB_DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("HUB_AUTO_SEED", "0")
    monkeypatch.setenv("HUB_SYNC_INTERVAL_HOURS", "0")
    legacy_path = tmp_path / "tracker.db"
    if legacy:
        _build_snapshot(str(legacy_path))
    monkeypatch.setenv("TRACKER_DB_PATH", str(legacy_path))
    sys.modules.pop("server", None)
    return importlib.import_module("server")


@pytest.fixture
def server(monkeypatch, tmp_path):
    return _load_server(monkeypatch, tmp_path, legacy=False)


def parsed(raw):
    assert isinstance(raw, str), "every tool returns a JSON string"
    return json.loads(raw)


async def test_tool_count_and_every_tool_is_guarded(server):
    """functools.wraps sets __wrapped__; a tool added without the guard fails here."""
    tools = await server.mcp.list_tools()
    assert len(tools) == HUB_TOOLS
    assert [t.name for t in tools if not hasattr(getattr(t, "fn", None), "__wrapped__")] == []


async def test_descriptions_carry_the_return_shape(server):
    """FastMCP drops everything from Args: down, so the return shape must sit above it."""
    for tool in await server.mcp.list_tools():
        assert "returns" in tool.description.lower(), tool.name
        assert "Args:" not in tool.description, tool.name


async def test_legacy_tools_register_only_with_the_old_db(monkeypatch, tmp_path):
    srv = _load_server(monkeypatch, tmp_path, legacy=True)
    names = {t.name for t in await srv.mcp.list_tools()}
    assert len(names) == HUB_TOOLS + LEGACY_TOOLS
    assert {"anthropic_department_trends", "anthropic_daily_summary"} <= names
    stats = parsed(await (await srv.mcp.get_tool("anthropic_history_stats")).fn())
    assert stats["read_only"] is True and stats["table_row_counts"]["jobs"] == 5


async def test_unexpected_failure_stays_inside_the_contract(server, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(server.hub.store, "search_jobs", broken)
    tool = await server.mcp.get_tool("search_jobs")
    payload = parsed(await tool.fn(query="ai"))
    assert payload["error"] == "Unexpected failure in search_jobs"
    assert "DatabaseError" in payload["detail"]


async def test_bad_input_is_reported_as_invalid_input(server):
    tool = await server.mcp.get_tool("track_application")
    payload = parsed(await tool.fn(company="A", title="B", stage="hired"))
    assert payload["error"] == "Invalid input" and "stage must be one of" in payload["detail"]


async def test_import_refuses_non_csv_paths(server, tmp_path):
    secret = tmp_path / "secrets.env"
    secret.write_text("KEY=hunter2")
    tool = await server.mcp.get_tool("import_companies")
    payload = parsed(await tool.fn(source=str(secret)))
    assert payload["error"] == "Invalid input" and "hunter2" not in json.dumps(payload)


async def test_full_loop_profile_match_apply_review(server):
    """set_profile -> top_matches -> job_detail -> track_application -> pipeline_summary."""
    store = server.hub.store
    company = store.add_company(Company(ats="ashby", slug="acme", name="Acme AI"))
    store.apply_sync(company["id"], [
        Job(source="ashby", company_slug="acme", company_name="Acme AI", external_id="1",
            title="Senior AI Enablement Lead", url="https://jobs.ashbyhq.com/acme/1",
            location="Remote - US", remote=True, description="Roll out Claude and MCP servers",
            salary_min=150_000, salary_max=185_000),
        Job(source="ashby", company_slug="acme", company_name="Acme AI", external_id="2",
            title="Sales Development Rep", url="https://jobs.ashbyhq.com/acme/2",
            location="Remote - US", remote=True, description="Cold calls"),
    ])

    async def call(name, **kw):
        return parsed(await (await server.mcp.get_tool(name)).fn(**kw))

    assert (await call("top_matches"))["profile_set"] is False
    profile = {"target_titles": ["AI Enablement"], "must_have_any": ["Claude"],
               "avoid_titles": ["sales development"], "salary": {"floor": 140000, "target": 160000},
               "location": {"remote": True}, "goal": {"target_date": "2099-01-01", "weekly_applications": 3},
               "mustt_have": ["typo"]}
    set_result = await call("set_profile", profile_json=json.dumps(profile))
    assert any("mustt_have" in w for w in set_result["warnings"])

    matches = await call("top_matches", min_score=10)
    assert [m["key"] for m in matches["jobs"]] == ["ashby:acme:1"]
    assert matches["jobs"][0]["fit"]["score"] >= 80

    detail = await call("job_detail", key="ashby:acme:1")
    assert "Claude" in detail["description"] and detail["fit"]["score"] >= 80
    assert (await call("job_detail", key="nope"))["error"] == "Job not found"

    app = await call("track_application", company="Acme AI", title="Senior AI Enablement Lead",
                     job_key="ashby:acme:1", next_action="nudge recruiter",
                     next_action_due="2000-01-01")
    assert app["url"] == "https://jobs.ashbyhq.com/acme/1"
    moved = await call("update_application", application_id=app["id"], stage="screen", note="call booked")
    assert moved["stage"] == "screen"

    assert (await call("top_matches", min_score=10))["count"] == 0  # applied roles drop out
    summary = await call("pipeline_summary")
    assert summary["counts"]["screen"] == 1 and summary["due"][0]["id"] == app["id"]
    assert summary["goal"]["weekly_target"] == 3

    await call("triage_job", key="ashby:acme:2", status="dismiss", reason="sales")
    assert (await call("search_jobs", query="cold calls"))["count"] == 0

    stats = await call("hub_stats")
    assert stats["active_jobs"] == 2 and len(stats["connectors"]) == 12


async def test_a_large_sync_runs_in_the_background(server, monkeypatch):
    """All 139 seed boards took 712 s; no MCP client waits that long on one call."""
    import asyncio

    from hub.tools import companies as companies_tools

    for i in range(companies_tools.BACKGROUND_THRESHOLD + 1):
        server.hub.store.add_company(Company(ats="greenhouse", slug=f"co{i}", name=f"Co {i}"))
    gate = asyncio.Event()

    async def slow_sync(store, http, rows, force=False):
        await gate.wait()
        return {"companies": len(rows), "ok": len(rows), "failed": [], "added": 0, "removed": 0,
                "fetched": 0, "busiest": []}

    monkeypatch.setattr(companies_tools, "sync_many", slow_sync)
    sync = (await server.mcp.get_tool("sync_companies")).fn
    stats = (await server.mcp.get_tool("hub_stats")).fn
    started = parsed(await sync())
    assert started["status"] == "started" and started["companies"] == companies_tools.BACKGROUND_THRESHOLD + 1
    assert parsed(await sync())["status"] == "already running"
    assert parsed(await stats())["background_sync"]["running"] is True
    gate.set()
    await server.hub.background["task"]
    done = parsed(await stats())["background_sync"]
    assert done["running"] is False and done["result"]["ok"] == companies_tools.BACKGROUND_THRESHOLD + 1
