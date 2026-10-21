"""Read-side analysis: pipeline health, goal pace, pay bands.

The pipeline summary is the job seeker's weekly review in one call: what is
due, what has gone quiet, which postings closed under an open application,
and whether the pace of applications will reach the goal in time.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

from hub.models import OPEN_STAGES, STAGES
from hub.store import Store, local_today

#: An application with no event for this long is a nudge candidate...
FOLLOW_UP_AFTER_DAYS = 7
#: ...and after this long with no movement it is probably a silent no.
GHOSTED_AFTER_DAYS = 30


def _days_since(stamp: str | None) -> int | None:
    try:
        return (local_today() - date.fromisoformat((stamp or "")[:10])).days
    except ValueError:
        return None


def pipeline_summary(store: Store, profile: dict | None) -> dict:
    apps = store.list_applications()
    counts = {s: 0 for s in STAGES}
    for a in apps:
        counts[a["stage"]] = counts.get(a["stage"], 0) + 1

    due, quiet, closed_postings = [], [], []
    for a in apps:
        if a["stage"] not in OPEN_STAGES:
            continue
        brief = {"id": a["id"], "company": a["company"], "title": a["title"], "stage": a["stage"]}
        if a.get("next_action_due") and a["next_action_due"][:10] <= local_today().isoformat():
            due.append({**brief, "next_action": a.get("next_action"), "due": a["next_action_due"]})
        idle = _days_since(a.get("updated_at"))
        if a["stage"] != "shortlisted" and idle is not None:
            if idle >= GHOSTED_AFTER_DAYS:
                quiet.append({**brief, "idle_days": idle,
                              "suggest": "mark ghosted, or send one last check-in"})
            elif idle >= FOLLOW_UP_AFTER_DAYS and not a.get("next_action_due"):
                quiet.append({**brief, "idle_days": idle, "suggest": "follow up"})
        if a.get("job_key"):
            job = store.get_job(a["job_key"])
            if job and not job["is_active"]:
                closed_postings.append({**brief, "removed_at": job["removed_at"]})

    goal = (profile or {}).get("goal") or {}
    pace = None
    if goal:
        week_start = (local_today() - timedelta(days=local_today().weekday())).isoformat()
        this_week = store.applications_since(week_start)
        weekly = goal.get("weekly_applications")
        target = goal.get("target_date")
        days_left = None
        if target:
            try:
                days_left = (date.fromisoformat(target) - local_today()).days
            except ValueError:
                days_left = None
        active = sum(counts[s] for s in ("screen", "interview", "final", "offer"))
        pace = {
            "target_date": target,
            "days_left": days_left,
            "applied_this_week": this_week,
            "weekly_target": weekly,
            "on_pace": None if weekly is None else this_week >= weekly,
            "live_processes": active,
            "offers": counts["offer"],
        }

    applied_total = sum(counts[s] for s in STAGES if s != "shortlisted")
    responded = sum(counts[s] for s in ("screen", "interview", "final", "offer", "accepted"))
    return {
        "counts": counts,
        "open": sum(counts[s] for s in OPEN_STAGES),
        "response_rate": round(responded / applied_total, 2) if applied_total else None,
        "due": due,
        "gone_quiet": sorted(quiet, key=lambda q: -q["idle_days"]),
        "posting_closed_under_open_application": closed_postings,
        "goal": pace,
    }


def pay_bands(rows: list[dict]) -> dict:
    """Pay percentiles over rows that post a range, by midpoint, annual USD only."""
    usd = [r for r in rows if r.get("salary_max") and (r.get("salary_currency") or "USD") == "USD"]
    if not usd:
        return {"count": 0}
    mids = sorted(((r.get("salary_min") or r["salary_max"]) + r["salary_max"]) / 2 for r in usd)

    def pct(p: float) -> int:
        return int(mids[min(len(mids) - 1, int(p * (len(mids) - 1) + 0.5))])

    by_company: dict[str, list[float]] = {}
    for r in usd:
        by_company.setdefault(r["company_name"], []).append(r["salary_max"])
    return {
        "count": len(usd),
        "midpoint_p25": pct(0.25),
        "midpoint_median": int(median(mids)),
        "midpoint_p75": pct(0.75),
        "highest_max": max(r["salary_max"] for r in usd),
        "top_paying_companies": sorted(
            ({"company": c, "postings": len(v), "max": int(max(v))} for c, v in by_company.items()),
            key=lambda x: -x["max"],
        )[:10],
        "examples": [
            {k: r.get(k) for k in ("key", "company_name", "title", "salary_min", "salary_max",
                                   "salary_period", "location")}
            for r in sorted(usd, key=lambda r: -r["salary_max"])[:10]
        ],
    }
