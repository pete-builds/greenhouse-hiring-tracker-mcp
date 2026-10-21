"""SQLite store: companies, job lifecycle, triage, applications, profile.

One writable database owned by this server. The legacy Anthropic tracker DB
stays a separate read-only mount (see `tools/legacy.py`); nothing here writes
to it.

Lifecycle rules, which every query below relies on:

* a job is keyed ``source:company_slug:external_id`` and upserted on every sync
* ``first_seen`` is set once; ``last_seen`` moves on every sync that returns it
* a job missing from a *successful* sync of its company gets ``removed_at``;
  a failed sync never removes anything, so an outage cannot read as a layoff
* a removed job that comes back is reactivated, not duplicated
* a new key whose company and normalised title match a job removed in the last
  60 days is recorded as a repost of it (the ghost-job signal)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

from hub.models import OPEN_STAGES, STAGES, Company, Job

SCHEMA_VERSION = 1
REPOST_WINDOW_DAYS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    ats TEXT NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    careers_url TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    added_at TEXT NOT NULL,
    last_synced_at TEXT,
    last_sync_status TEXT,
    last_sync_error TEXT,
    active_jobs INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ats, slug)
);

CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company_name TEXT NOT NULL,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT,
    remote INTEGER,
    department TEXT,
    employment_type TEXT,
    posted_at TEXT,
    updated_at TEXT,
    description TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT,
    salary_period TEXT,
    salary_source TEXT,
    content_hash TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    removed_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    repost_of TEXT
);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_id, is_active);
CREATE INDEX IF NOT EXISTS jobs_first_seen ON jobs(first_seen);
CREATE INDEX IF NOT EXISTS jobs_title_norm ON jobs(company_name, title_norm);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    key UNINDEXED, title, company_name, location, department, description,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    fetched INTEGER, added INTEGER, removed INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS triage (
    job_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('dismissed', 'saved')),
    reason TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    job_key TEXT,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    stage TEXT NOT NULL,
    applied_on TEXT,
    salary_note TEXT,
    contact TEXT,
    folder TEXT,
    next_action TEXT,
    next_action_due TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS applications_job ON applications(job_key)
    WHERE job_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_JOB_COLUMNS = (
    "key, source, external_id, company_name, title, url, location, remote, "
    "department, employment_type, posted_at, updated_at, salary_min, salary_max, "
    "salary_currency, salary_period, salary_source, first_seen, last_seen, "
    "removed_at, is_active, repost_of"
)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def local_today() -> date:
    """Today in the server's own timezone (TZ), which is how rows are stamped."""
    return datetime.now().astimezone().date()


def today() -> str:
    """The local calendar date, matching how first_seen is stamped.

    Every date comparison in this module is done in Python against this, never
    with SQLite's date('now'), which is UTC and drifted the old tracker's
    lookbacks by a day every evening Eastern.
    """
    return local_today().isoformat()


def _days_ago(days: int) -> str:
    return (local_today() - timedelta(days=days)).isoformat()


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(text: str, title_only: bool = False) -> str:
    """Turn user text into a safe FTS5 query.

    Comma-separated groups are OR-ed; words inside a group are AND-ed and
    prefix-matched ("architect" finds "architecture"). Every word is quoted,
    so FTS5 operators typed by a user (NEAR, -, *, ") are searched for rather
    than executed.
    """
    groups = []
    for group in text.split(","):
        words = [w.replace('"', "") for w in group.split() if w.replace('"', "")]
        if words:
            groups.append(" ".join(f'"{w}"*' for w in words))
    query = " OR ".join(f"({g})" for g in groups)
    return f"title : ({query})" if title_only and query else query


class Store:
    """Thread-safe wrapper over one SQLite connection.

    The MCP server is a single async process; a lock around a single
    connection is simpler than a pool and plenty for a personal or team hub.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _rows(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params))]

    def _one(self, sql: str, params: Iterable = ()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ---------------------------------------------------------------- companies

    def add_company(self, company: Company) -> dict:
        """Insert or refresh a company. Idempotent on (ats, slug)."""
        with self._tx() as c:
            c.execute(
                "INSERT INTO companies (ats, slug, name, careers_url, tags, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (ats, slug) DO UPDATE SET name = excluded.name, "
                "careers_url = COALESCE(excluded.careers_url, companies.careers_url), "
                "tags = excluded.tags",
                (
                    company.ats,
                    company.slug,
                    company.name,
                    company.careers_url,
                    json.dumps(sorted(set(company.tags))),
                    now_iso(),
                ),
            )
        return self.get_company(company.ats, company.slug)  # type: ignore[return-value]

    def get_company(self, ats: str, slug: str) -> dict | None:
        row = self._one("SELECT * FROM companies WHERE ats = ? AND slug = ?", (ats, slug))
        return _decode_company(row) if row else None

    def find_companies(self, name_or_slug: str) -> list[dict]:
        term = f"%{escape_like(name_or_slug.lower())}%"
        rows = self._rows(
            "SELECT * FROM companies WHERE lower(name) LIKE ? ESCAPE '\\' "
            "OR lower(slug) LIKE ? ESCAPE '\\' ORDER BY name",
            (term, term),
        )
        return [_decode_company(r) for r in rows]

    def list_companies(self, ats: str | None = None, tag: str | None = None) -> list[dict]:
        sql = "SELECT * FROM companies"
        params: list = []
        if ats:
            sql += " WHERE ats = ?"
            params.append(ats)
        rows = [_decode_company(r) for r in self._rows(sql + " ORDER BY name", params)]
        if tag:
            rows = [r for r in rows if tag in r["tags"]]
        return rows

    def remove_company(self, ats: str, slug: str) -> bool:
        with self._tx() as c:
            company = c.execute(
                "SELECT id FROM companies WHERE ats = ? AND slug = ?", (ats, slug)
            ).fetchone()
            if not company:
                return False
            keys = [r[0] for r in c.execute("SELECT key FROM jobs WHERE company_id = ?", (company[0],))]
            c.executemany("DELETE FROM jobs_fts WHERE key = ?", [(k,) for k in keys])
            c.execute("DELETE FROM companies WHERE id = ?", (company[0],))
        return True

    def companies_due(self, max_age_hours: float) -> list[dict]:
        """Companies never synced, or last synced longer ago than the window."""
        cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).replace(microsecond=0)
        rows = self._rows(
            "SELECT * FROM companies WHERE last_synced_at IS NULL OR last_synced_at < ? "
            "ORDER BY last_synced_at IS NOT NULL, last_synced_at",
            (cutoff.isoformat(),),
        )
        return [_decode_company(r) for r in rows]

    # -------------------------------------------------------------------- sync

    def known_jobs(self, company_id: int) -> dict[str, str]:
        """external_id -> stored updated_at, for active rows that have a description.

        Connectors use it to skip detail calls for postings they already hold.
        Rows without a description are left out so they get filled in.
        """
        rows = self._rows(
            "SELECT external_id, COALESCE(updated_at, '') AS u FROM jobs "
            "WHERE company_id = ? AND is_active = 1 AND description IS NOT NULL",
            (company_id,),
        )
        return {r["external_id"]: r["u"] for r in rows}

    def record_sync_failure(self, company_id: int, error: str) -> None:
        stamp = now_iso()
        with self._tx() as c:
            c.execute(
                "INSERT INTO sync_runs (company_id, started_at, finished_at, status, error) "
                "VALUES (?, ?, ?, 'error', ?)",
                (company_id, stamp, stamp, error[:500]),
            )
            c.execute(
                "UPDATE companies SET last_synced_at = ?, last_sync_status = 'error', "
                "last_sync_error = ? WHERE id = ?",
                (stamp, error[:500], company_id),
            )

    def apply_sync(self, company_id: int, jobs: list[Job]) -> dict:
        """Upsert one company's complete job list and retire what vanished.

        Only call this with the result of a sync that succeeded end to end:
        anything not in ``jobs`` is marked removed.
        """
        stamp, day = now_iso(), today()
        repost_cutoff = _days_ago(REPOST_WINDOW_DAYS)
        added: list[str] = []
        reactivated: list[str] = []
        with self._tx() as c:
            existing = {
                r["key"]: dict(r)
                for r in c.execute(
                    "SELECT key, is_active, content_hash FROM jobs WHERE company_id = ?",
                    (company_id,),
                )
            }
            seen: set[str] = set()
            for job in jobs:
                if job.key in seen:
                    continue
                seen.add(job.key)
                prior = existing.get(job.key)
                chash = job.content_hash()
                values = (
                    job.company_name, job.title, job.title_norm, job.url, job.location,
                    None if job.remote is None else int(job.remote), job.department,
                    job.employment_type, job.posted_at, job.updated_at, job.description,
                    job.salary_min, job.salary_max, job.salary_currency, job.salary_period,
                    job.salary_source, chash,
                )
                if prior is None:
                    repost = c.execute(
                        "SELECT key FROM jobs WHERE company_id = ? AND title_norm = ? "
                        "AND is_active = 0 AND removed_at >= ? ORDER BY removed_at DESC LIMIT 1",
                        (company_id, job.title_norm, repost_cutoff),
                    ).fetchone()
                    c.execute(
                        "INSERT INTO jobs (company_name, title, title_norm, url, location, remote, "
                        "department, employment_type, posted_at, updated_at, description, "
                        "salary_min, salary_max, salary_currency, salary_period, salary_source, "
                        "content_hash, key, company_id, source, external_id, first_seen, "
                        "last_seen, is_active, repost_of) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                        (*values, job.key, company_id, job.source, job.external_id, day, stamp,
                         repost[0] if repost else None),
                    )
                    added.append(job.key)
                elif job.description is None:
                    # The connector skipped this posting's detail call: refresh
                    # the list-level fields and keep the stored body and pay.
                    if not prior["is_active"]:
                        reactivated.append(job.key)
                    c.execute(
                        "UPDATE jobs SET company_name=?, title=?, title_norm=?, url=?, "
                        "location=COALESCE(?, location), remote=COALESCE(?, remote), "
                        "department=COALESCE(?, department), last_seen=?, is_active=1, "
                        "removed_at=NULL WHERE key=?",
                        (job.company_name, job.title, job.title_norm, job.url, job.location,
                         values[5], job.department, stamp, job.key),
                    )
                    continue
                else:
                    if not prior["is_active"]:
                        reactivated.append(job.key)
                    c.execute(
                        "UPDATE jobs SET company_name=?, title=?, title_norm=?, url=?, location=?, "
                        "remote=?, department=?, employment_type=?, posted_at=?, updated_at=?, "
                        "description=?, salary_min=?, salary_max=?, salary_currency=?, "
                        "salary_period=?, salary_source=?, content_hash=?, last_seen=?, "
                        "is_active=1, removed_at=NULL WHERE key=?",
                        (*values, stamp, job.key),
                    )
                    if prior["content_hash"] == chash and prior["is_active"]:
                        continue
                c.execute("DELETE FROM jobs_fts WHERE key = ?", (job.key,))
                c.execute(
                    "INSERT INTO jobs_fts (key, title, company_name, location, department, "
                    "description) VALUES (?,?,?,?,?,?)",
                    (job.key, job.title, job.company_name, job.location or "",
                     job.department or "", job.description or ""),
                )
            gone = [k for k, r in existing.items() if r["is_active"] and k not in seen]
            c.executemany(
                "UPDATE jobs SET is_active = 0, removed_at = ? WHERE key = ?",
                [(day, k) for k in gone],
            )
            c.execute(
                "INSERT INTO sync_runs (company_id, started_at, finished_at, status, fetched, "
                "added, removed) VALUES (?, ?, ?, 'ok', ?, ?, ?)",
                (company_id, stamp, now_iso(), len(seen), len(added), len(gone)),
            )
            c.execute(
                "UPDATE companies SET last_synced_at = ?, last_sync_status = 'ok', "
                "last_sync_error = NULL, active_jobs = ? WHERE id = ?",
                (stamp, len(seen), company_id),
            )
        return {
            "fetched": len(seen),
            "added": len(added),
            "removed": len(gone),
            "reactivated": len(reactivated),
        }

    # -------------------------------------------------------------------- jobs

    def get_job(self, key: str) -> dict | None:
        row = self._one(
            "SELECT j.*, t.status AS triage, t.reason AS triage_reason, a.id AS application_id, "
            "a.stage AS application_stage FROM jobs j "
            "LEFT JOIN triage t ON t.job_key = j.key "
            "LEFT JOIN applications a ON a.job_key = j.key WHERE j.key = ?",
            (key,),
        )
        return _decode_job(row) if row else None

    def search_jobs(
        self,
        query: str | None = None,
        *,
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
        with_description: bool = False,
        title_only: bool = False,
    ) -> list[dict]:
        cols = "j.*" if with_description else ", ".join(
            f"j.{c.strip()}" for c in _JOB_COLUMNS.split(",")
        )
        sql = [(
            f"SELECT {cols}, co.ats, co.slug AS company_slug, co.tags AS company_tags, "
            "t.status AS triage, a.stage AS application_stage FROM jobs j "
            "JOIN companies co ON co.id = j.company_id "
            "LEFT JOIN triage t ON t.job_key = j.key "
            "LEFT JOIN applications a ON a.job_key = j.key"
        )]
        where: list[str] = []
        params: list = []
        if query and _fts_query(query):
            where.append("j.key IN (SELECT key FROM jobs_fts WHERE jobs_fts MATCH ?)")
            params.append(_fts_query(query, title_only))
        if company:
            term = f"%{escape_like(company.lower())}%"
            where.append("(lower(j.company_name) LIKE ? ESCAPE '\\' OR co.slug LIKE ? ESCAPE '\\')")
            params += [term, term]
        if ats:
            where.append("co.ats = ?")
            params.append(ats)
        if tag:
            where.append("co.tags LIKE ? ESCAPE '\\'")
            params.append(f'%"{escape_like(tag)}"%')
        if remote_only:
            where.append("j.remote = 1")
        if location:
            where.append("lower(j.location) LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(location.lower())}%")
        if min_salary:
            where.append("j.salary_max >= ?")
            params.append(int(min_salary))
        if posted_within_days:
            where.append("COALESCE(substr(j.posted_at, 1, 10), j.first_seen) >= ?")
            params.append(_days_ago(posted_within_days))
        if active_only:
            where.append("j.is_active = 1")
        if not include_dismissed:
            where.append("(t.status IS NULL OR t.status != 'dismissed')")
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY j.first_seen DESC, j.company_name LIMIT ?")
        params.append(max(1, min(int(limit), 2000)))
        return [_decode_job(r) for r in self._rows(" ".join(sql), params)]

    def recent_changes(self, days: int = 7, company: str | None = None) -> dict:
        cutoff = _days_ago(max(1, min(days, 365)))
        extra, params = "", []
        if company:
            extra = " AND lower(j.company_name) LIKE ? ESCAPE '\\'"
            params = [f"%{escape_like(company.lower())}%"]
        added = self._rows(
            "SELECT j.key, j.company_name, j.title, j.location, j.url, j.first_seen, j.repost_of "
            f"FROM jobs j WHERE j.first_seen >= ?{extra} ORDER BY j.first_seen DESC",
            [cutoff, *params],
        )
        removed = self._rows(
            "SELECT j.key, j.company_name, j.title, j.first_seen, j.removed_at FROM jobs j "
            f"WHERE j.removed_at >= ?{extra} ORDER BY j.removed_at DESC",
            [cutoff, *params],
        )
        return {"since": cutoff, "added": added, "removed": removed}

    def company_activity(self, days: int = 30) -> list[dict]:
        """Per-company hiring velocity: who is opening and closing roles."""
        cutoff = _days_ago(max(1, min(days, 365)))
        return self._rows(
            "SELECT co.name, co.ats, co.slug, co.active_jobs, co.last_synced_at, "
            "co.last_sync_status, "
            "SUM(CASE WHEN j.first_seen >= ? THEN 1 ELSE 0 END) AS opened, "
            "SUM(CASE WHEN j.removed_at >= ? THEN 1 ELSE 0 END) AS closed "
            "FROM companies co LEFT JOIN jobs j ON j.company_id = co.id "
            "GROUP BY co.id ORDER BY opened DESC, co.active_jobs DESC",
            (cutoff, cutoff),
        )

    def set_triage(self, key: str, status: str | None, reason: str | None = None) -> None:
        with self._tx() as c:
            if status is None:
                c.execute("DELETE FROM triage WHERE job_key = ?", (key,))
            else:
                c.execute(
                    "INSERT INTO triage (job_key, status, reason, at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (job_key) DO UPDATE SET status = excluded.status, "
                    "reason = excluded.reason, at = excluded.at",
                    (key, status, reason, now_iso()),
                )

    def scoring_candidates(self, posted_within_days: int | None, limit: int) -> list[dict]:
        return self.search_jobs(
            None, posted_within_days=posted_within_days, limit=limit, with_description=True
        )

    # ------------------------------------------------------------ applications

    def upsert_application(
        self,
        *,
        company: str,
        title: str,
        stage: str = "applied",
        job_key: str | None = None,
        url: str | None = None,
        applied_on: str | None = None,
        salary_note: str | None = None,
        contact: str | None = None,
        folder: str | None = None,
        next_action: str | None = None,
        next_action_due: str | None = None,
        note: str | None = None,
    ) -> dict:
        if stage not in STAGES:
            raise ValueError(f"stage must be one of {', '.join(STAGES)}")
        stamp = now_iso()
        if stage != "shortlisted" and not applied_on:
            applied_on = today()
        with self._tx() as c:
            existing = None
            if job_key:
                existing = c.execute(
                    "SELECT id, stage FROM applications WHERE job_key = ?", (job_key,)
                ).fetchone()
            if existing is None:
                existing = c.execute(
                    "SELECT id, stage FROM applications WHERE lower(company) = lower(?) "
                    "AND lower(title) = lower(?)",
                    (company, title),
                ).fetchone()
            if existing:
                app_id = existing[0]
                c.execute(
                    "UPDATE applications SET stage = ?, job_key = COALESCE(?, job_key), "
                    "url = COALESCE(?, url), applied_on = COALESCE(applied_on, ?), "
                    "salary_note = COALESCE(?, salary_note), contact = COALESCE(?, contact), "
                    "folder = COALESCE(?, folder), next_action = COALESCE(?, next_action), "
                    "next_action_due = COALESCE(?, next_action_due), updated_at = ? WHERE id = ?",
                    (stage, job_key, url, applied_on, salary_note, contact, folder,
                     next_action, next_action_due, stamp, app_id),
                )
                if existing[1] != stage:
                    c.execute(
                        "INSERT INTO application_events (application_id, at, kind, detail) "
                        "VALUES (?, ?, 'stage', ?)",
                        (app_id, stamp, f"{existing[1]} -> {stage}"),
                    )
            else:
                cur = c.execute(
                    "INSERT INTO applications (job_key, company, title, url, stage, applied_on, "
                    "salary_note, contact, folder, next_action, next_action_due, created_at, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_key, company, title, url, stage, applied_on, salary_note, contact,
                     folder, next_action, next_action_due, stamp, stamp),
                )
                app_id = cur.lastrowid
                c.execute(
                    "INSERT INTO application_events (application_id, at, kind, detail) "
                    "VALUES (?, ?, 'stage', ?)",
                    (app_id, stamp, f"created as {stage}"),
                )
            if note:
                c.execute(
                    "INSERT INTO application_events (application_id, at, kind, detail) "
                    "VALUES (?, ?, 'note', ?)",
                    (app_id, stamp, note),
                )
        return self.get_application(app_id)  # type: ignore[arg-type,return-value]

    def update_application(self, app_id: int, **fields) -> dict | None:
        current = self.get_application(app_id)
        if current is None:
            return None
        merged = {k: current[k] for k in ("company", "title")}
        merged.update({k: v for k, v in fields.items() if v is not None})
        merged.setdefault("stage", current["stage"])
        if fields.get("job_key") is None and current.get("job_key"):
            merged["job_key"] = current["job_key"]
        return self.upsert_application(**merged)

    def get_application(self, app_id: int) -> dict | None:
        app = self._one("SELECT * FROM applications WHERE id = ?", (app_id,))
        if app is None:
            return None
        app["events"] = self._rows(
            "SELECT at, kind, detail FROM application_events WHERE application_id = ? ORDER BY id",
            (app_id,),
        )
        return app

    def list_applications(self, stage: str | None = None, open_only: bool = False) -> list[dict]:
        sql, params = "SELECT * FROM applications", []
        if stage:
            sql += " WHERE stage = ?"
            params.append(stage)
        elif open_only:
            sql += f" WHERE stage IN ({','.join('?' * len(OPEN_STAGES))})"
            params += list(OPEN_STAGES)
        return self._rows(sql + " ORDER BY updated_at DESC", params)

    def application_counts(self) -> dict[str, int]:
        rows = self._rows("SELECT stage, COUNT(*) AS n FROM applications GROUP BY stage")
        return {r["stage"]: r["n"] for r in rows}

    def applications_since(self, day: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM applications WHERE applied_on >= ?", (day,)
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ profile

    def get_profile(self) -> dict | None:
        row = self._one("SELECT data, updated_at FROM profile WHERE id = 1")
        if not row:
            return None
        return {"profile": json.loads(row["data"]), "updated_at": row["updated_at"]}

    def set_profile(self, data: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO profile (id, data, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET data = excluded.data, "
                "updated_at = excluded.updated_at",
                (json.dumps(data), now_iso()),
            )

    # -------------------------------------------------------------------- stats

    def stats(self) -> dict:
        def count(sql: str) -> int:
            row = self._one(sql)
            return int(next(iter(row.values()))) if row else 0

        by_ats = self._rows(
            "SELECT ats, COUNT(*) AS companies, SUM(active_jobs) AS active_jobs "
            "FROM companies GROUP BY ats ORDER BY companies DESC"
        )
        failing = self._rows(
            "SELECT name, ats, slug, last_sync_error FROM companies "
            "WHERE last_sync_status = 'error' ORDER BY name"
        )
        return {
            "companies": count("SELECT COUNT(*) FROM companies"),
            "companies_never_synced": count(
                "SELECT COUNT(*) FROM companies WHERE last_synced_at IS NULL"
            ),
            "active_jobs": count("SELECT COUNT(*) FROM jobs WHERE is_active = 1"),
            "jobs_ever_seen": count("SELECT COUNT(*) FROM jobs"),
            "with_salary": count(
                "SELECT COUNT(*) FROM jobs WHERE is_active = 1 AND salary_max IS NOT NULL"
            ),
            "remote": count("SELECT COUNT(*) FROM jobs WHERE is_active = 1 AND remote = 1"),
            "applications": self.application_counts(),
            "by_ats": by_ats,
            "failing_companies": failing,
            "last_sync": (self._one("SELECT MAX(finished_at) AS t FROM sync_runs") or {}).get("t"),
            "db_path": self.path,
        }


def _decode_company(row: dict) -> dict:
    row = dict(row)
    row["tags"] = json.loads(row.get("tags") or "[]")
    return row


def _decode_job(row: dict) -> dict:
    row = dict(row)
    if "remote" in row and row["remote"] is not None:
        row["remote"] = bool(row["remote"])
    if "is_active" in row:
        row["is_active"] = bool(row["is_active"])
    if "company_tags" in row:
        row["company_tags"] = json.loads(row["company_tags"] or "[]")
    return row
