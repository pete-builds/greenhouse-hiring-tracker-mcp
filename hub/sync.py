"""Sync engine: pull boards, apply them to the store, guard against bad pulls.

The silent-failure guard matters more than anything else in this module. A
board that answers 200 with zero jobs, or a fraction of yesterday's count, is
far more often a moved board or a broken connector than a company that closed
every role overnight. Applying it would mark every job removed, and every
"recently closed" answer downstream would then be wrong while every tool
reported success. So a collapse is refused and recorded as a sync error.
"""

from __future__ import annotations

import asyncio
import logging
import os

from hub import connectors
from hub.connectors.base import DEFAULT_DETAIL_LIMIT
from hub.http import Http
from hub.store import Store

log = logging.getLogger("hiring-hub.sync")

#: Refuse a sync that drops a board with at least this many active jobs...
COLLAPSE_MIN_PRIOR = 10
#: ...to under this share of them.
COLLAPSE_RATIO = 0.2
DEFAULT_CONCURRENCY = 6


async def sync_company(store: Store, http: Http, company: dict, *, force: bool = False,
                       detail_limit: int | None = None) -> dict:
    """Sync one company row. Never raises: failures come back as a status."""
    if detail_limit is None:
        detail_limit = int(os.getenv("HUB_DETAIL_LIMIT", DEFAULT_DETAIL_LIMIT))
    label = f"{company['ats']}/{company['slug']}"
    try:
        connector = connectors.get(company["ats"])
        known = store.known_jobs(company["id"])
        jobs = await connector.fetch(http, company["slug"], known=known, detail_limit=detail_limit)
    except Exception as exc:  # noqa: BLE001 - one board failing never stops the rest
        error = f"{type(exc).__name__}: {exc}"[:500]
        store.record_sync_failure(company["id"], error)
        log.warning("sync %s failed: %s", label, error)
        return {"company": company["name"], "board": label, "status": "error", "error": error}

    prior = company.get("active_jobs") or 0
    if (not force and prior >= COLLAPSE_MIN_PRIOR
            and len(jobs) < prior * COLLAPSE_RATIO):
        error = (f"refused: board returned {len(jobs)} jobs against {prior} last sync; "
                 "re-run with force=true if the company really closed them")
        store.record_sync_failure(company["id"], error)
        return {"company": company["name"], "board": label, "status": "refused", "error": error}

    for job in jobs:
        # Connectors that cannot see a display name fall back to the slug;
        # the name the user gave when adding the company is better.
        if job.company_name == company["slug"]:
            job.company_name = company["name"]
    result = store.apply_sync(company["id"], jobs)
    return {"company": company["name"], "board": label, "status": "ok", **result}


async def sync_many(store: Store, http: Http, companies: list[dict], *,
                    concurrency: int = DEFAULT_CONCURRENCY, force: bool = False) -> dict:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(company: dict) -> dict:
        async with sem:
            return await sync_company(store, http, company, force=force)

    results = await asyncio.gather(*(one(c) for c in companies))
    summary = {
        "companies": len(results),
        "ok": sum(r["status"] == "ok" for r in results),
        "failed": [r for r in results if r["status"] != "ok"],
        "added": sum(r.get("added", 0) for r in results),
        "removed": sum(r.get("removed", 0) for r in results),
        "fetched": sum(r.get("fetched", 0) for r in results),
    }
    summary["busiest"] = sorted(
        (r for r in results if r.get("added")), key=lambda r: r["added"], reverse=True
    )[:10]
    return summary


async def background_loop(store: Store, http: Http, interval_hours: float) -> None:
    """Re-sync every company older than the interval, forever.

    Checks every 15 minutes rather than sleeping a full interval, so a company
    added between runs, or a restart, picks up within minutes.
    """
    while True:
        try:
            due = store.companies_due(interval_hours)
            if due:
                summary = await sync_many(store, http, due)
                log.info("background sync: %s ok, %s failed, +%s -%s",
                         summary["ok"], len(summary["failed"]), summary["added"], summary["removed"])
        except Exception:
            log.exception("background sync run failed")
        await asyncio.sleep(900)
