# Hiring Hub MCP

An MCP server that turns every company's public job board into one searchable, ranked store, with an application pipeline beside it. Point Claude (or any MCP client) at it and ask "what's new that fits me?", "is this still open?", "what does this role pay elsewhere?", "what's waiting on me this week?".

- **12 applicant tracking systems:** Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, BambooHR, Rippling, Workday, Teamtailor, Breezy HR, Personio. All read through each vendor's public, unauthenticated job-board API.
- **4 multi-company boards, searched live:** Himalayas, Remote OK, Hacker News "Who is hiring?", Remotive.
- **139 starter companies**, each board confirmed live when added, plus discovery by name, careers-URL detection, and bulk import from CSV directories of thousands of boards.
- **Explainable fit scoring** against your profile: every score is a sum of named parts with reasons, hard blockers, and flags (unposted pay, region-limited remote, possible ghost jobs).
- **Job lifecycle:** first seen, last seen, removed, reopened, reposted. That gives you "is it still open?", hiring velocity per company, and ghost-job signals.
- **Application pipeline:** stages, events, next actions, follow-ups due, postings that closed under an open application, and pace against a goal date.

It never applies to anything, fills a form, or contacts anyone. It finds, ranks, checks and remembers; you apply.

> Formerly `greenhouse-hiring-tracker-mcp`, which tracked Anthropic's Greenhouse board alone. Anthropic is now one of 139 seed boards, and the old snapshot history is still served when its database is mounted (see [Upgrading](#upgrading-from-the-anthropic-tracker)).

## Quick start

**Docker** (a shared instance, the background sync keeps it fresh):

```bash
git clone https://github.com/pete-builds/greenhouse-hiring-tracker-mcp.git
cd greenhouse-hiring-tracker-mcp
docker compose up -d
claude mcp add hiring-hub -t http http://localhost:3713/mcp
```

**No Docker** (stdio, one process per client, data in `~/.hiring-hub/`):

```bash
claude mcp add hiring-hub -- uvx --from git+https://github.com/pete-builds/greenhouse-hiring-tracker-mcp hiring-hub
```

Then, in Claude:

1. *"Set my Hiring Hub profile from profile.yaml"*: start from [`profile.example.yaml`](profile.example.yaml).
2. *"Sync every company"*: the first full sync of the 139 seed boards takes a few minutes and runs in the background. Re-syncs are incremental.
3. *"What are my top matches?"*, *"Anything new today?"*, *"Is ashby:openai:... still open?"*

For a ready-made agent that runs daily sweeps, writes leads files and application packets, and keeps the pipeline, copy [`agent/job-search.md`](agent/job-search.md) into your project's `.claude/commands/`.

## Tools (23)

**Boards**

| Tool | What it does |
|---|---|
| `add_company(board, ats?, name?, tags?)` | Track a board from any careers or job URL (ATS and slug detected), or a slug plus `ats`. Syncs immediately. |
| `discover_company(name, add?)` | Probe likely slugs on 11 ATSes at once. Each hit is marked `confirmed`, `mismatch` or `unverified` by checking that the board names the company; only confirmed hits are added automatically. |
| `import_companies(source, ats?, tag?, name_filter?, limit?)` | Bulk-add from the bundled seed, a local CSV, or an https CSV, e.g. a per-ATS `companies.csv` from [ats-scrapers](https://github.com/kalil0321/ats-scrapers) (about 6,000 Greenhouse boards alone). |
| `list_companies`, `remove_company` | What is tracked, with sync health per board. |
| `sync_companies(company?, ats?, tag?, stale_hours?, force?)` | Pull boards now. Over 15 companies runs in the background; progress is in `hub_stats`. |
| `hub_stats` | Totals, per-ATS counts, failing boards, last sync, background sync status. |

**Jobs**

| Tool | What it does |
|---|---|
| `search_jobs(query, ...filters)` | Full-text search over title, company, location and description. Commas mean OR, words prefix-match. Filters: company, ats, tag, remote, location, min salary, posted within N days, title only. |
| `top_matches(limit, min_score, posted_within_days)` | Best open roles for your profile, scored and explained. |
| `whats_new(days)` | Daily brief: new matching roles, saved or applied roles whose posting closed, and failing boards, so an empty brief is distinguishable from a broken one. |
| `job_detail(key, recheck?)` | One role in full, with fit. `recheck=true` re-syncs the board first: "is this still open right now?" |
| `score_job(key)` | Part-by-part fit explanation. |
| `triage_job(key, save/dismiss/clear)` | Dismissed roles stop surfacing; saved roles are watched for closing. |
| `compensation_stats(query)` | 25th/50th/75th percentile of posted pay for matching titles, top-paying companies, highest postings. |
| `recent_changes(days)`, `company_activity(days)` | Opened and closed roles; hiring velocity per company. |
| `search_job_boards(query, sources?)` | Live search of Himalayas, Remote OK and HN Who is Hiring (Remotive on request), ranked against your profile, with attribution. |

**Pipeline and profile**

| Tool | What it does |
|---|---|
| `track_application(company, title, stage, job_key?, ...)` | Log an application or a shortlisted role. Idempotent. |
| `update_application(id, stage?, note?, next_action?, ...)` | Move a stage, add a note or a next action. Every change is an event. |
| `list_applications(stage?, open_only?)` | The pipeline. |
| `pipeline_summary` | Funnel counts, response rate, actions due, applications gone quiet (7 days: follow up; 30 days: probably ghosted), postings closed under open applications, and pace against your goal. |
| `get_profile`, `set_profile(json, merge?)` | The profile that drives scoring. Unknown keys are kept and flagged. |

Every tool returns a JSON string. Failures come back as `{error, detail}`, never as a raised exception.

## How it works

```
 Greenhouse  Lever  Ashby  Workable  SmartRecruiters  Recruitee
 BambooHR  Rippling  Workday  Teamtailor  Breezy  Personio
        │  public job-board APIs, polite client: per-host cap, retries, Retry-After
        ▼
   connectors ──► normalised Job (salary annualised, remote detected, HTML to text)
        │
        ▼
   sync ──► collapse guard ──► SQLite store (WAL, FTS5)
        │                       companies · jobs lifecycle · triage
        │                       applications · events · profile
        ▼
   scoring (profile) · insights (pipeline, pay bands) ──► 23 MCP tools
                                                          Streamable HTTP or stdio
```

Design choices worth knowing:

- **A failed sync never removes anything.** Only a sync that succeeded end to end retires missing jobs. A board that suddenly returns under a fifth of its last count is refused and recorded as an error, because that is far more often a moved board than a company closing every role overnight. `force=true` applies it once you have checked.
- **Incremental by default.** Greenhouse is fetched without bodies and only new or edited postings get their description; ATSes with no description in the list call (SmartRecruiters, BambooHR, Rippling, Workday) get at most `HUB_DETAIL_LIMIT` detail calls per sync, four at a time per host. A large board fills in over a few syncs. A full re-sync of the 139 seed boards took 160 seconds on 2026-10-20.
- **Pay is structured first.** Ashby's compensation components, Lever's `salaryRange`, Recruitee's salary block and Rippling's pay ranges are used as given; otherwise a conservative parser reads the posting text. It needs a currency marker and a salary-shaped band, so "10-15 years" never becomes pay, and it keeps the sentence the numbers came from. Hourly ranges are annualised at 2,080 hours and labelled.
- **Remote is read from what the posting says.** Ashby's `isRemote` flag is true on "Hybrid - San Francisco" postings, so a stated workplace outranks a vendor flag. "Remote - Sweden" is remote but region-limited for a US profile, and it is flagged as such.
- **Scores are explainable and title-gated.** A role whose title matches nothing you target is capped at 30 whatever else it has going for it.
- **Discovery verifies identity.** Building the seed list turned up live boards under `zendesk`, `palo` and `par` belonging to unrelated companies, and an `xai` board posting SpaceX jobs. A hit is `confirmed` only when the board's own company name, or its postings, name the company you asked for.

## Configuration

All optional; see [`.env.example`](.env.example).

| Var | Default | Purpose |
|---|---|---|
| `HUB_DB_PATH` | `/hub/hub.db` in the image, else `~/.hiring-hub/hub.db` | The store |
| `HUB_SYNC_INTERVAL_HOURS` | `12` | Background re-sync age; `0` disables |
| `HUB_DETAIL_LIMIT` | `150` | Per-sync detail calls on boards without list descriptions |
| `HUB_AUTO_SEED` | `1` | Import the 139 starter companies into an empty store |
| `HUB_PROFILE_PATH` | none | Profile JSON or YAML loaded when none is stored |
| `HUB_CONTACT` | none | Appended to the User-Agent, so a vendor can reach you instead of blocking you |
| `TRACKER_DB_PATH` | `/data/tracker.db` | The old Anthropic tracker DB; enables `anthropic_*` tools when present |
| `MCP_TRANSPORT` | `http` (`stdio` for the `hiring-hub` command) | Transport |
| `FASTMCP_HOST`, `FASTMCP_PORT` | `0.0.0.0`, `3713` | HTTP bind |

The HTTP endpoint has no authentication. Keep port 3713 on a LAN, a VPN or behind an authenticating proxy; it holds your applications and profile.

## Upgrading from the Anthropic tracker

The 1.0 server owns a new writable volume, `hiring-hub-data`, and needs nothing from the old one. To keep the Anthropic snapshot history (department trends and alerts since the tracker started), mount the old volume read-only in a `docker-compose.override.yml`; the commented block in [`docker-compose.yml`](docker-compose.yml) has the exact lines. Four `anthropic_*` tools then appear: `anthropic_department_trends`, `anthropic_daily_summary`, `anthropic_alerts`, `anthropic_history_stats`. The old `search_jobs`, `recent_changes`, `live_*` and `compensation_for` tools are replaced by the hub's own, which cover Anthropic like any other board.

## Good citizenship

These are public endpoints that employers publish so their jobs get seen. Keep it that way: the client caps concurrency per vendor, backs off on 429 and 5xx, honours `Retry-After`, and syncs incrementally. Remote OK asks that you link back and name it as the source; results carry an `attribution` field for that. Remotive asks for at most four calls a day, so it is opt-in and cached for six hours. The [ats-scrapers](https://github.com/kalil0321/ats-scrapers) company lists are MIT code with no stated data licence; other directories, such as the Common Crawl-derived lists in job-board-aggregator, are CC BY-NC. Check a list's terms before you import it for anything commercial.

## Development

```bash
uv venv -p 3.13 && uv pip install -r requirements.lock -r requirements-dev.txt
.venv/bin/pytest -q          # 138 tests; connector tests run on trimmed live payloads
.venv/bin/ruff check .
```

CI runs the lockfile drift check, ruff, the tests, and Trivy filesystem and image scans on every push. A `v*.*.*` tag publishes a multi-arch image with SBOM and provenance to GHCR, after a guard checks that `hub/__init__.py`, `pyproject.toml` and the compose pin all name the tag.

## License

MIT
