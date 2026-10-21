"""Anthropic history from the original tracker's database, when it is mounted.

Before this server covered every board, it read the nightly snapshots that
pete-builds/greenhouse-hiring-tracker writes for Anthropic alone. Those
snapshots hold department counts and alerts going back to April 2026, which
the hub's own store cannot reconstruct. When TRACKER_DB_PATH points at that
database, these four tools expose it read-only; otherwise they are not
registered at all. Live Anthropic jobs come from the hub like any other board
(add_company "https://job-boards.greenhouse.io/anthropic").
"""

from __future__ import annotations

from hub.tools import Hub, fmt, tool_guard


def register(mcp, hub: Hub) -> None:
    db = hub.legacy

    @mcp.tool()
    @tool_guard
    async def anthropic_department_trends(name: str | None = None, days: int = 30) -> str:
        """Anthropic per-department open-role counts over time, from nightly snapshots.

        Returns {department: [{date, count}]} sorted by date ascending.

        Args:
            name: Department substring filter (case-insensitive).
            days: Lookback in days (default 30, max 365).
        """
        return fmt(db.department_trends(name=name, days=days))

    @mcp.tool()
    @tool_guard
    async def anthropic_daily_summary(date: str | None = None) -> str:
        """One day's Anthropic snapshot: totals, adds, removals, departments, locations.

        Returns the snapshot, or {date, found: false} when none exists for that date.

        Args:
            date: ISO date. Default: latest snapshot.
        """
        summary = db.daily_summary(date=date)
        return fmt(summary if summary is not None else {"date": date, "found": False})

    @mcp.tool()
    @tool_guard
    async def anthropic_alerts(severity: str | None = None) -> str:
        """Unacknowledged Anthropic hiring alerts: freezes, surges, mass removals.

        Returns {count, alerts: [{id, triggered_at, type, severity, message}]}.

        Args:
            severity: info, warning or critical.
        """
        alerts = db.active_alerts(severity=severity)
        return fmt({"count": len(alerts), "alerts": alerts})

    @mcp.tool()
    @tool_guard
    async def anthropic_history_stats() -> str:
        """Row counts and date range of the Anthropic snapshot history.

        Returns {table_row_counts, active_jobs, latest_snapshot_date,
        total_snapshots, db_path, read_only}.
        """
        return fmt(db.db_stats())
