# Hiring Hub design notes (1.0, 2026-10-21)

Why the hub is shaped the way it is, what it borrowed, and what it left out on purpose.

## Prior art surveyed (2026-10-20)

| Project | What we took | What we did not |
|---|---|---|
| [kalil0321/ats-scrapers](https://github.com/kalil0321/ats-scrapers) (MIT code) | Per-ATS `companies.csv` directories as the way to reach "every company on a platform": `import_companies` reads them as-is. A normalised job schema with annualised salary and an `is_remote` column. | Bundling its lists: the data licence is not stated, so the hub imports them at the user's request instead of redistributing them. |
| [Riley-Dorrington/job-board-aggregator](https://github.com/Riley-Dorrington/job-board-aggregator) | Per-source volume anomaly detection, which became the collapse guard. Per-vendor concurrency caps. | Its Common Crawl company lists (CC BY-NC). |
| [career-ops](https://github.com/career-ops-hq/career-ops) | A fit score with a floor below which you do not apply; a "still open?" check before any drafting (`job_detail(recheck=true)`); follow-up cadence; never auto-submitting. | Its 1-to-5 model-graded score: ours is deterministic and explainable, so it can be tested. |
| [gzchenhao/openhire](https://github.com/gzchenhao/openhire) | Ranking on real posting age and a ghost signal (`repost_of`, "open N days" flags). | |
| [groundtruthtools/ats-jobs-mcp](https://github.com/groundtruthtools/ats-jobs-mcp) | A conservative salary parser: needs a currency marker, refuses non-salary ranges. | Refusing hourly rates: we annualise them and label the period instead. |
| [amikai/openings-mcp](https://github.com/amikai/openings-mcp) | Breadth across many ATSes from one tool surface. | No rate limiting and no history: the hub keeps snapshots and is polite by construction. |
| LinkedIn and Indeed MCPs, JobSpy | Nothing. | Their access breaks those sites' terms (LinkedIn User Agreement 8.2) and invites account bans; employers' own ATS boards carry the same jobs without that. |
| Auto-apply bots (AIHawk and successors) | Nothing. | Auto-submitting spams employers' ATSes and puts unreviewed claims in front of hiring managers. |

## Decisions

1. **One writable store owned by the server**, not a cron writer plus a read-only reader as in 0.x. A read-only mount could never hold a pipeline or a profile, and the 0.x `immutable=1` reader could not see rows still in the writer's WAL (audit 2026-09-08 #3).
2. **Connectors return complete boards; the store decides lifecycle.** "Missing from a successful sync" is the only way a job is removed, so lifecycle logic lives in one place and every connector stays a pure fetch-and-normalise.
3. **Identity confirmation on discovery.** Building the seed, probing names found live, unrelated boards under `zendesk` (BambooHR), `palo`, `par` (Teamtailor) and `prompt` (Ashby), and the `xai` Greenhouse board now posts SpaceX jobs. A guessed slug is a guess until the board names the company.
4. **Deterministic scoring.** Every part is visible, every blocker names its rule, and tests pin each rule to the real posting that motivated it (hybrid-flagged-remote on Ashby, "Remote - Sweden", "Senior Privacy Counsel" at an AI company).
5. **Background sync above 15 companies.** The first full sync of the seed took 712 seconds; no MCP client waits that long on one call.
6. **No auth on the HTTP endpoint**, matching the fleet's LAN posture. The profile and pipeline are personal data, so the README says plainly to keep the port private.

## Not yet built

- iCIMS, Jobvite, Taleo and SuccessFactors boards: no clean public JSON endpoint was verified.
- Workday boards over 2,000 jobs: the API caps `total` at 2,000 and wraps silently past it; splitting by facets would get the rest.
- ETag / If-Modified-Since: no vendor surveyed was confirmed to honour them.
- A web view of the pipeline. The MCP tools and the agent's markdown files cover it today.
