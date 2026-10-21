"""Tools for choosing which boards the hub tracks, and syncing them."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from hub import connectors, discovery
from hub.models import Company
from hub.store import now_iso
from hub.sync import sync_company, sync_many
from hub.tools import Hub, fmt, tool_guard

SEED_PATH = Path(__file__).resolve().parent.parent / "seed" / "companies.csv"
#: A sync of more companies than this runs in the background: all 139 seed
#: boards took 712 s end to end on 2026-10-20, longer than MCP clients wait.
BACKGROUND_THRESHOLD = 15

#: A directory import larger than this needs an explicit higher limit, so one
#: call cannot queue six thousand boards for the next background sync by accident.
IMPORT_DEFAULT_LIMIT = 500


def _split_tags(tags: str | None) -> list[str]:
    return [t.strip().lower() for t in (tags or "").split(",") if t.strip()]


def register(mcp, hub: Hub) -> None:
    @mcp.tool()
    @tool_guard
    async def add_company(
        board: str,
        ats: str | None = None,
        name: str | None = None,
        tags: str | None = None,
        sync_now: bool = True,
    ) -> str:
        """Start tracking a company's job board, from a careers URL or a slug.

        Pass any careers or job URL (job-boards.greenhouse.io/anthropic,
        jobs.lever.co/palantir, jobs.ashbyhq.com/openai, a myworkdayjobs.com
        site, ...) and the ATS and slug are detected. Or pass a bare slug with
        ``ats``. Unsure which ATS a company uses? Call discover_company first.
        Idempotent: re-adding updates the name and tags. Returns
        {company, sync} where sync is {status, fetched, added, removed} or an
        error, or {error, detail} if the board is not recognised.

        Args:
            board: Careers/job URL, or a board slug when ``ats`` is given.
            ats: greenhouse, lever, ashby, workable, smartrecruiters, recruitee, bamboohr, rippling, workday (slug tenant/wdN/site), teamtailor, breezy, personio.
            name: Display name. Defaults to the slug.
            tags: Comma-separated labels for filtering, e.g. "ai-lab, target".
            sync_now: Pull the board immediately (default true).
        """
        board = board.strip()
        if ats:
            connector = connectors.get(ats)
            slug = connector.slug_from_url(board) if "/" in board and ats != "workday" else None
            slug = slug or board
            ats_name = connector.ats
        else:
            hit = connectors.detect(board)
            if not hit:
                raise ValueError(
                    "could not tell which ATS this URL is; pass ats=..., or call discover_company"
                )
            ats_name, slug = hit
        connectors.get(ats_name).check_slug(slug)
        company = hub.store.add_company(Company(
            ats=ats_name, slug=slug, name=name or slug.split("/")[0],
            careers_url=board if "://" in board else connectors.get(ats_name).board_url(slug),
            tags=_split_tags(tags),
        ))
        result = {"company": company}
        if sync_now:
            result["sync"] = await sync_company(hub.store, hub.http, company)
        return fmt(result)

    @mcp.tool()
    @tool_guard
    async def discover_company(name: str, add: bool = False, tags: str | None = None) -> str:
        """Find which ATS a company uses by probing likely slugs on every platform.

        Tries slugs derived from the name ("Hugging Face" -> huggingface,
        hugging-face, ...) against Greenhouse, Lever, Ashby, Workable,
        SmartRecruiters, Recruitee, BambooHR, Rippling, Teamtailor, Breezy and
        Personio concurrently. Workday cannot be guessed from a name: paste
        its myworkdayjobs.com URL into add_company instead. Slugs collide with
        unrelated companies often, so each hit carries identity: "confirmed"
        (the board or its postings name the company), "mismatch", or
        "unverified". Only confirmed hits are added automatically. Returns
        {name, hits: [{ats, slug, open_jobs, identity, board_url}], added}.

        Args:
            name: Company name as people write it.
            add: Track the best hit immediately, if it is confirmed.
            tags: Tags for the added company.
        """
        hits = await discovery.discover(hub.http, name)
        added = None
        if add and hits and hits[0]["identity"] != "confirmed":
            return fmt({"name": name, "hits": hits, "added": None,
                        "note": "no hit is confirmed as this company; check board_url, then add_company"})
        if add and hits:
            best = hits[0]
            added = hub.store.add_company(Company(
                ats=best["ats"], slug=best["slug"], name=name, careers_url=best["board_url"],
                tags=_split_tags(tags),
            ))
            await sync_company(hub.store, hub.http, added)
        return fmt({"name": name, "hits": hits, "added": added})

    @mcp.tool()
    @tool_guard
    async def list_companies(ats: str | None = None, tag: str | None = None) -> str:
        """List tracked companies with sync health.

        Each row: name, ats, slug, tags, active_jobs, last_synced_at,
        last_sync_status (ok/error), last_sync_error. Returns {count, companies}.

        Args:
            ats: Only this ATS.
            tag: Only companies carrying this tag.
        """
        rows = hub.store.list_companies(ats=ats, tag=tag.lower() if tag else None)
        return fmt({"count": len(rows), "companies": rows})

    @mcp.tool()
    @tool_guard
    async def remove_company(ats: str, slug: str) -> str:
        """Stop tracking a company and delete its stored jobs.

        Applications already logged for its jobs are kept. Returns {removed: bool}.

        Args:
            ats: The company's ATS.
            slug: The board slug, as list_companies shows it.
        """
        return fmt({"removed": hub.store.remove_company(ats.lower(), slug)})

    @mcp.tool()
    @tool_guard
    async def import_companies(
        source: str = "seed",
        ats: str | None = None,
        tag: str | None = None,
        name_filter: str | None = None,
        limit: int = IMPORT_DEFAULT_LIMIT,
    ) -> str:
        """Bulk-add companies from the bundled seed list or a CSV directory.

        ``source="seed"`` loads the curated starter list shipped with the hub
        (tech and AI employers, every slug confirmed live when it was added).
        Otherwise pass a local CSV path or an https URL to a CSV with a header
        naming any of name/company, slug/token, ats, url, tags; for example a
        per-ATS companies.csv from github.com/kalil0321/ats-scrapers with
        ats=greenhouse. Rows are added, not synced: the background sync picks
        them up, or call sync_companies. Returns {parsed, added, skipped,
        tag_applied, sample}.

        Args:
            source: "seed", a file path, or an https:// URL.
            ats: ATS for rows that do not name one.
            tag: Tag added to every imported company.
            name_filter: Only import rows whose name or slug contains this text.
            limit: Maximum rows to add (default 500).
        """
        if source == "seed":
            text = SEED_PATH.read_text()
        elif source.startswith("https://"):
            text = await hub.http.text(source)
        elif source.startswith("http://"):
            raise ValueError("use an https:// URL")
        else:
            # Only CSVs: a tool that reads any path a model names is a file
            # reader, and the parse errors would echo file contents back.
            local = Path(os.path.expanduser(source))
            if local.suffix.lower() != ".csv":
                raise ValueError("source must be 'seed', an https:// URL, or a .csv file")
            text = local.read_text()
        rows = discovery.parse_directory(text, ats=ats.lower() if ats else None)
        if name_filter:
            needle = name_filter.lower()
            rows = [r for r in rows if needle in r["name"].lower() or needle in r["slug"].lower()]
        extra = _split_tags(tag)
        added = []
        for row in rows[: max(0, int(limit))]:
            added.append(hub.store.add_company(Company(
                ats=row["ats"], slug=row["slug"], name=row["name"],
                careers_url=row["careers_url"], tags=row["tags"] + extra,
            )))
        return fmt({
            "parsed": len(rows), "added": len(added), "skipped": max(0, len(rows) - len(added)),
            "tag_applied": extra, "sample": [f"{a['ats']}/{a['slug']}" for a in added[:10]],
        })

    @mcp.tool()
    @tool_guard
    async def sync_companies(
        company: str | None = None,
        ats: str | None = None,
        tag: str | None = None,
        stale_hours: float = 0,
        force: bool = False,
        wait: bool = False,
    ) -> str:
        """Pull fresh job lists now for some or all tracked companies.

        With no filters, syncs every company. ``stale_hours`` skips companies
        synced more recently than that. A board that suddenly returns under a
        fifth of its last job count is refused (recorded as an error) rather
        than marking everything removed; pass force=true once you have checked
        the company really closed those roles. More than 15 companies run in
        the background and this returns {status: "started", companies}
        at once; hub_stats.background_sync reports progress and the result.
        Otherwise returns {companies, ok, failed, added, removed, fetched, busiest}.

        Args:
            company: Name or slug substring.
            ats: Only this ATS.
            tag: Only companies with this tag.
            stale_hours: Only companies not synced in this many hours.
            force: Apply even a collapsed board.
            wait: Block until a large sync finishes instead of backgrounding it.
        """
        rows = hub.store.find_companies(company) if company else hub.store.list_companies(
            ats=ats, tag=tag.lower() if tag else None
        )
        if company and ats:
            rows = [r for r in rows if r["ats"] == ats]
        if stale_hours:
            due = {r["id"] for r in hub.store.companies_due(stale_hours)}
            rows = [r for r in rows if r["id"] in due]
        if len(rows) <= BACKGROUND_THRESHOLD or wait:
            return fmt(await sync_many(hub.store, hub.http, rows, force=force))
        task = hub.background.get("task")
        if task is not None and not task.done():
            return fmt({"status": "already running", "started_at": hub.background["started_at"],
                        "companies": hub.background["companies"]})

        async def run() -> None:
            try:
                hub.background["result"] = await sync_many(hub.store, hub.http, rows, force=force)
            except Exception as exc:  # noqa: BLE001 - surfaced through hub_stats
                hub.background["result"] = {"error": f"{type(exc).__name__}: {exc}"}
            hub.background["finished_at"] = now_iso()

        hub.background.clear()
        hub.background.update(started_at=now_iso(), companies=len(rows))
        hub.background["task"] = asyncio.create_task(run())
        return fmt({"status": "started", "companies": len(rows),
                    "check": "hub_stats -> background_sync"})

    @mcp.tool()
    @tool_guard
    async def hub_stats() -> str:
        """Health of the hub: what is tracked, how fresh, what is failing.

        Returns {companies, companies_never_synced, active_jobs, jobs_ever_seen,
        with_salary, remote, applications (by stage), by_ats, failing_companies,
        last_sync, db_path, connectors, legacy_anthropic_history, and
        background_sync {running, started_at, finished_at, companies, result}
        once a large sync has been started}.
        """
        stats = hub.store.stats()
        stats["connectors"] = sorted(connectors.CONNECTORS)
        stats["legacy_anthropic_history"] = hub.legacy is not None
        bg = hub.background
        if bg:
            task = bg.get("task")
            result = bg.get("result") or {}
            stats["background_sync"] = {
                "running": bool(task and not task.done()),
                "started_at": bg.get("started_at"), "finished_at": bg.get("finished_at"),
                "companies": bg.get("companies"),
                "result": {k: v for k, v in result.items() if k != "busiest"} if result else None,
            }
        return fmt(stats)
