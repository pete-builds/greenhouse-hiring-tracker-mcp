"""Tools for the application pipeline and the job seeker's profile."""

from __future__ import annotations

import json

from hub import insights
from hub.models import STAGES
from hub.tools import Hub, fmt, tool_guard

#: Profile keys scoring understands; anything else is kept but flagged, so a
#: typo ("must_haves") reads as a warning instead of silently scoring nothing.
PROFILE_KEYS = {
    "name", "summary", "target_titles", "title_keywords", "must_have_any", "nice_to_have",
    "avoid_titles", "avoid_keywords", "seniority", "location", "salary", "companies",
    "max_age_days", "goal", "notes",
}


def register(mcp, hub: Hub) -> None:
    @mcp.tool()
    @tool_guard
    async def track_application(
        company: str,
        title: str,
        stage: str = "applied",
        job_key: str | None = None,
        url: str | None = None,
        applied_on: str | None = None,
        contact: str | None = None,
        folder: str | None = None,
        salary_note: str | None = None,
        next_action: str | None = None,
        next_action_due: str | None = None,
        note: str | None = None,
    ) -> str:
        """Log an application (or a shortlisted role) in the pipeline.

        Idempotent: the same job_key, or the same company and title, updates
        the existing row and records a stage change event. Pass job_key when
        the role came from this hub, so the pipeline can warn when its posting
        closes. The hub never submits anything: apply on the employer's site,
        then log it here. Stages: shortlisted, applied, screen, interview,
        final, offer, accepted, rejected, withdrawn, ghosted. Returns the
        application with its event history.

        Args:
            company: Employer name.
            title: Role title.
            stage: Pipeline stage (default "applied").
            job_key: Hub job key, when the role is in the store.
            url: Posting URL, for roles found elsewhere.
            applied_on: ISO date; defaults to today for any stage past shortlisted.
            contact: Recruiter or hiring manager name and channel.
            folder: Where the application materials live, e.g. a repo path.
            salary_note: Posted range or what was discussed.
            next_action: The next thing to do, e.g. "follow up with recruiter".
            next_action_due: ISO date the next action is due.
            note: Free-text note added to the event history.
        """
        if job_key and not url:
            job = hub.store.get_job(job_key)
            if job is None:
                return fmt({"error": "Job not found", "key": job_key})
            url = job["url"]
        app = hub.store.upsert_application(
            company=company, title=title, stage=stage, job_key=job_key, url=url,
            applied_on=applied_on, contact=contact, folder=folder, salary_note=salary_note,
            next_action=next_action, next_action_due=next_action_due, note=note,
        )
        return fmt(app)

    @mcp.tool()
    @tool_guard
    async def update_application(
        application_id: int,
        stage: str | None = None,
        note: str | None = None,
        next_action: str | None = None,
        next_action_due: str | None = None,
        contact: str | None = None,
        folder: str | None = None,
        salary_note: str | None = None,
    ) -> str:
        """Move an application to a new stage, or add a note or next action.

        Unset arguments are left unchanged. Returns the application with its
        event history, or {error: "Application not found", application_id}.

        Args:
            application_id: From track_application or list_applications.
            stage: New stage.
            note: Note for the event history (interview feedback, who you spoke to).
            next_action: Next thing to do.
            next_action_due: ISO date.
            contact: Recruiter or hiring manager.
            folder: Materials location.
            salary_note: Pay discussed.
        """
        if stage is not None and stage not in STAGES:
            raise ValueError(f"stage must be one of {', '.join(STAGES)}")
        app = hub.store.update_application(
            application_id, stage=stage, note=note, next_action=next_action,
            next_action_due=next_action_due, contact=contact, folder=folder,
            salary_note=salary_note,
        )
        if app is None:
            return fmt({"error": "Application not found", "application_id": application_id})
        return fmt(app)

    @mcp.tool()
    @tool_guard
    async def list_applications(stage: str | None = None, open_only: bool = False) -> str:
        """List pipeline rows, most recently updated first.

        Each row: id, company, title, stage, applied_on, job_key, url, contact,
        folder, next_action, next_action_due, updated_at. Returns {count, applications}.

        Args:
            stage: Only this stage.
            open_only: Only stages that are still live (shortlisted through offer).
        """
        rows = hub.store.list_applications(stage=stage, open_only=open_only)
        return fmt({"count": len(rows), "applications": rows})

    @mcp.tool()
    @tool_guard
    async def pipeline_summary() -> str:
        """Weekly review in one call: funnel, what is due, what went quiet, goal pace.

        Returns {counts (per stage), open, response_rate, due (next actions
        due today or earlier), gone_quiet (no movement for 7+ days, with a
        suggestion; 30+ days suggests ghosted), posting_closed_under_open_application,
        goal {target_date, days_left, applied_this_week, weekly_target, on_pace,
        live_processes, offers}}. goal is null until the profile sets one.
        """
        stored = hub.store.get_profile()
        return fmt(insights.pipeline_summary(hub.store, stored["profile"] if stored else None))

    @mcp.tool()
    @tool_guard
    async def get_profile() -> str:
        """The stored job-search profile that drives top_matches and scoring.

        Returns {profile, updated_at}, or {profile: null} when none is set.
        """
        return fmt(hub.store.get_profile() or {"profile": None})

    @mcp.tool()
    @tool_guard
    async def set_profile(profile_json: str, merge: bool = True) -> str:
        """Store the job-search profile as a JSON object.

        Keys (all optional): target_titles, title_keywords, must_have_any,
        nice_to_have, avoid_titles, avoid_keywords, seniority {exclude, prefer},
        location {remote, remote_regions, onsite_ok}, salary {floor, target},
        companies {prefer, avoid}, max_age_days, goal {target_date,
        weekly_applications}, summary, notes. merge=true updates only the keys
        given. Unknown keys are kept and listed under warnings. Returns
        {profile, warnings}.

        Args:
            profile_json: JSON object, e.g. {"target_titles": ["AI Enablement"], "salary": {"floor": 140000}}.
            merge: Merge into the stored profile (default) or replace it.
        """
        try:
            incoming = json.loads(profile_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"profile_json is not valid JSON: {exc}") from None
        if not isinstance(incoming, dict):
            raise ValueError("profile_json must be a JSON object, not a list or a scalar")  # noqa: TRY004 - the guard reports ValueError as bad input
        current = (hub.store.get_profile() or {}).get("profile") or {}
        profile = {**current, **incoming} if merge else incoming
        hub.store.set_profile(profile)
        warnings = [f"unknown key {k!r} is stored but not used in scoring"
                    for k in profile if k not in PROFILE_KEYS]
        return fmt({"profile": profile, "warnings": warnings})
