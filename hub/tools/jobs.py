"""Tools for finding, ranking and judging jobs across every tracked board."""

from __future__ import annotations

import asyncio

from hub import insights, scoring
from hub.connectors.aggregators import AGGREGATORS, DEFAULT_AGGREGATORS
from hub.sync import sync_company
from hub.tools import Hub, fmt, tool_guard

#: How much description a single job_detail call returns. Enough to tailor an
#: application; the full text stays in the store for search and scoring.
DETAIL_CHARS = 12_000


#: Ranking reads full descriptions, so it scans a bounded candidate set.
SCAN_LIMIT = 2000


def _prefilter(profile: dict) -> str | None:
    """An FTS query from the profile's titles and keywords, OR-ed.

    Matched against titles only. With 25,000 jobs across 139 boards, a
    description match on "AI" hit the 2,000-row scan cap and cut off older
    relevant roles; a job whose title matches none of these words is capped
    by the title gate in scoring anyway.
    """
    terms = list(profile.get("target_titles") or []) + list(profile.get("title_keywords") or [])
    return ", ".join(t for t in terms if str(t).strip()) or None


def _profile(hub: Hub) -> dict:
    stored = hub.store.get_profile()
    return stored["profile"] if stored else {}


def register(mcp, hub: Hub) -> None:
    @mcp.tool()
    @tool_guard
    async def search_jobs(
        query: str | None = None,
        company: str | None = None,
        ats: str | None = None,
        tag: str | None = None,
        remote_only: bool = False,
        location: str | None = None,
        min_salary: int | None = None,
        posted_within_days: int | None = None,
        active_only: bool = True,
        include_dismissed: bool = False,
        limit: int = 50,
        title_only: bool = False,
    ) -> str:
        """Full-text search over every stored job (title, company, location, description).

        Query syntax: words in a group are AND-ed, comma-separated groups are
        OR-ed ("ai enablement, solutions architect"). Words prefix-match, so
        "architect" also finds "architecture". min_salary keeps only postings whose top of
        range reaches it: unposted pay is excluded, so search without it too.
        Newest first. Each job carries key (pass to job_detail, score_job,
        track_application), company_name, title, url, location, remote,
        salary_min/max (annual), first_seen, removed_at, triage,
        application_stage. Returns {count, jobs}.

        Args:
            query: Search text. Omit to list by filters only.
            company: Company name or slug substring.
            ats: Only this ATS.
            tag: Only companies with this tag.
            remote_only: Only postings detected as remote.
            location: Location substring, e.g. "New York".
            min_salary: Top of posted range at least this (annual, posting currency).
            posted_within_days: Posted (or first seen) in the last N days.
            active_only: Exclude closed postings (default true).
            include_dismissed: Include jobs you dismissed.
            limit: Max rows (default 50, max 2000).
            title_only: Match the query against titles only.
        """
        rows = hub.store.search_jobs(
            query, company=company, ats=ats, tag=tag.lower() if tag else None,
            remote_only=remote_only, location=location, min_salary=min_salary,
            posted_within_days=posted_within_days, active_only=active_only,
            include_dismissed=include_dismissed, limit=limit, title_only=title_only,
        )
        return fmt({"count": len(rows), "jobs": rows})

    @mcp.tool()
    @tool_guard
    async def job_detail(key: str, recheck: bool = False) -> str:
        """One job in full: description, pay, lifecycle, fit against your profile.

        recheck=true re-syncs the company's board first, so "is_active" answers
        "is this still open right now?". Do that before drafting an
        application: postings close quietly. Returns the job with description
        (up to 12,000 chars), first_seen, last_seen, removed_at, repost_of,
        triage, application_id, and fit {score, blockers, reasons, flags};
        or {error: "Job not found", key}.

        Args:
            key: Job key from search_jobs or top_matches (source:company:id).
            recheck: Re-sync the board before answering.
        """
        job = hub.store.get_job(key)
        if job is None:
            return fmt({"error": "Job not found", "key": key})
        resync = None
        if recheck:
            company = next((c for c in hub.store.list_companies() if c["id"] == job["company_id"]), None)
            if company:
                resync = await sync_company(hub.store, hub.http, company)
                job = hub.store.get_job(key) or job
        fit = scoring.score_job(job, _profile(hub))
        desc = job.get("description") or ""
        job["description"] = desc[:DETAIL_CHARS]
        job["description_truncated"] = len(desc) > DETAIL_CHARS
        return fmt({**job, "fit": fit, "recheck": resync})

    @mcp.tool()
    @tool_guard
    async def top_matches(
        limit: int = 25,
        min_score: int = 50,
        posted_within_days: int | None = 30,
        company: str | None = None,
        tag: str | None = None,
        include_applied: bool = False,
    ) -> str:
        """Best-fit open jobs for the stored profile, ranked and explained.

        Scores every candidate on title, skills, location, pay, freshness and
        company preference (0-100) and drops anything a profile blocker
        rules out (avoided title words, below the pay floor, not remote when
        remote is required). Each result carries fit {score, parts, reasons,
        flags}; read flags, they hold "salary not posted" and possible ghost
        jobs. Requires set_profile first. Returns {profile_set, scanned,
        count, jobs}.

        Args:
            limit: How many to return (default 25).
            min_score: Minimum fit score (default 50).
            posted_within_days: Only jobs posted or first seen in this window (default 30; null for all).
            company: Company name or slug substring.
            tag: Only companies with this tag.
            include_applied: Include jobs already in your application pipeline.
        """
        profile = _profile(hub)
        if not profile:
            return fmt({"profile_set": False, "error": "No profile", "detail": "call set_profile first"})
        rows = hub.store.search_jobs(
            _prefilter(profile), company=company, tag=tag.lower() if tag else None,
            posted_within_days=posted_within_days, limit=SCAN_LIMIT, with_description=True,
            title_only=True,
        )
        if not include_applied:
            rows = [r for r in rows if not r.get("application_stage")]
        ranked = scoring.rank(rows, profile, min_score=min_score, limit=limit)
        return fmt({"profile_set": True, "scanned": len(rows), "count": len(ranked), "jobs": ranked})

    @mcp.tool()
    @tool_guard
    async def whats_new(days: int = 1, min_score: int = 40, limit: int = 30) -> str:
        """Daily brief: new matching jobs, closed postings you cared about, failing boards.

        Ranks jobs first seen in the last N days against the profile, lists
        saved or applied jobs whose posting has since closed, and names boards
        whose last sync failed, so an empty brief can be told apart from a
        broken one. Returns {days, new_matches, new_total, closed_watched,
        failing_boards}.

        Args:
            days: Lookback in days (default 1).
            min_score: Minimum fit score for new matches (default 40).
            limit: Max new matches (default 30).
        """
        profile = _profile(hub)
        changes = hub.store.recent_changes(days=days)
        new_keys = [a["key"] for a in changes["added"]]
        rows = [hub.store.get_job(k) for k in new_keys[:3000]]
        rows = [r for r in rows if r and r.get("is_active") and not r.get("application_stage")]
        ranked = scoring.rank(rows, profile, min_score=min_score, limit=limit) if profile else []
        removed = {r["key"] for r in changes["removed"]}
        watched = [
            {"key": k, "company": j["company_name"], "title": j["title"],
             "removed_at": j["removed_at"], "triage": j.get("triage"),
             "application_stage": j.get("application_stage")}
            for k in removed
            if (j := hub.store.get_job(k)) and (j.get("triage") == "saved" or j.get("application_stage"))
        ]
        failing = hub.store.stats()["failing_companies"]
        return fmt({
            "days": days, "profile_set": bool(profile), "new_total": len(new_keys),
            "new_matches": ranked, "closed_watched": watched, "failing_boards": failing,
        })

    @mcp.tool()
    @tool_guard
    async def score_job(key: str) -> str:
        """Explain how one job scores against the profile, part by part.

        Returns {key, title, company, fit: {score, blockers, parts, reasons,
        flags}} or {error: "Job not found", key}.

        Args:
            key: Job key.
        """
        job = hub.store.get_job(key)
        if job is None:
            return fmt({"error": "Job not found", "key": key})
        return fmt({"key": key, "title": job["title"], "company": job["company_name"],
                    "fit": scoring.score_job(job, _profile(hub))})

    @mcp.tool()
    @tool_guard
    async def triage_job(key: str, status: str, reason: str | None = None) -> str:
        """Save a job for later, dismiss it so it stops surfacing, or clear either.

        Dismissed jobs drop out of search_jobs, top_matches and whats_new.
        Saved jobs are watched: whats_new reports when their posting closes.
        Returns {key, status}.

        Args:
            key: Job key.
            status: "save", "dismiss", or "clear".
            reason: Why, for your own later reference (e.g. "on-site only").
        """
        mapping = {"save": "saved", "saved": "saved", "dismiss": "dismissed",
                   "dismissed": "dismissed", "clear": None}
        if status not in mapping:
            raise ValueError('status must be "save", "dismiss" or "clear"')
        if hub.store.get_job(key) is None:
            return fmt({"error": "Job not found", "key": key})
        hub.store.set_triage(key, mapping[status], reason)
        return fmt({"key": key, "status": mapping[status]})

    @mcp.tool()
    @tool_guard
    async def compensation_stats(
        query: str,
        remote_only: bool = False,
        company: str | None = None,
    ) -> str:
        """Posted pay bands for roles matching a query, across every tracked board.

        Matches job titles only, so "ai enablement" means roles titled that,
        not every posting that mentions it. Uses only postings that publish a
        range, in USD, annualised. Gives
        25th/50th/75th percentile of range midpoints, the top-paying companies
        and the ten highest postings, which is what to anchor a salary ask on.
        Returns {query, count, midpoint_p25, midpoint_median, midpoint_p75,
        highest_max, top_paying_companies, examples}.

        Args:
            query: Role search text, e.g. "ai enablement, ai solutions architect".
            remote_only: Only remote postings.
            company: Company name or slug substring.
        """
        rows = hub.store.search_jobs(query, remote_only=remote_only, company=company, limit=2000,
                                     title_only=True)
        return fmt({"query": query, **insights.pay_bands(rows)})

    @mcp.tool()
    @tool_guard
    async def recent_changes(days: int = 7, company: str | None = None) -> str:
        """Jobs opened and closed across tracked boards in the last N days.

        Added rows carry repost_of when a listing with the same title closed
        within 60 days, a common sign of an evergreen or ghost posting.
        Returns {since, added, removed}.

        Args:
            days: Lookback (default 7, max 365).
            company: Company name substring.
        """
        return fmt(hub.store.recent_changes(days=days, company=company))

    @mcp.tool()
    @tool_guard
    async def company_activity(days: int = 30) -> str:
        """Hiring velocity per company: roles opened and closed in the window.

        A company opening many roles is hiring into budget; one closing more
        than it opens may be freezing. Returns a list of {name, ats, slug,
        active_jobs, opened, closed, last_synced_at, last_sync_status}.

        Args:
            days: Window in days (default 30).
        """
        return fmt(hub.store.company_activity(days=days))

    @mcp.tool()
    @tool_guard
    async def search_job_boards(query: str, sources: str | None = None, limit: int = 40) -> str:
        """Live search of multi-company boards: Himalayas, Remote OK, HN Who is Hiring.

        Finds roles at companies you do not track yet. Results are not stored;
        when one looks good, add the employer's own board with add_company or
        discover_company. Each row carries source and attribution, which
        Remote OK's terms ask you to keep when sharing. Remotive is available
        by naming it ("remotive") and is cached six hours, since its API asks
        for at most four calls a day. Returns {query, count, errors, jobs}.

        Args:
            query: Search text; commas separate alternatives for remoteok.
            sources: Comma-separated subset of himalayas, remoteok, hackernews, remotive.
            limit: Max rows (default 40).
        """
        names = [s.strip().lower() for s in (sources or ",".join(DEFAULT_AGGREGATORS)).split(",") if s.strip()]
        unknown = [n for n in names if n not in AGGREGATORS]
        if unknown:
            raise ValueError(f"unknown sources {unknown}; one of {sorted(AGGREGATORS)}")
        results = await asyncio.gather(
            *(AGGREGATORS[n](hub.http, query) for n in names), return_exceptions=True
        )
        jobs, errors = [], {}
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                errors[name] = f"{type(res).__name__}: {res}"
            else:
                jobs += res
        profile = _profile(hub)
        if profile:
            for j in jobs:
                j["fit"] = scoring.score_job({**j, "company_name": j.get("company")}, profile)
            jobs.sort(key=lambda j: j["fit"]["score"], reverse=True)
        for j in jobs:
            j.pop("description", None)
        return fmt({"query": query, "count": len(jobs[:limit]), "errors": errors, "jobs": jobs[:limit]})
