# Changelog

## 2026-09-30: FastMCP 4.0.10 on MCP SDK 2.2.0, and idle sessions reaped again

Supersedes Dependabot's grouped bump (#52): fastmcp 3.4.7 to 4.0.10 (it proposed 4.0.5), uvicorn 0.52.4 to 0.53.0, ruff 0.16.5 to 0.16.8. The lock is fresh-compiled from its own header with uv 0.12.5, which brings mcp 1.30.0 to 2.2.0.

- **Idle sessions were about to stop being reaped.** FastMCP 4 passes its own `http_session_idle_timeout` setting, default None, to the SDK session manager, overriding the SDK's 1800 second default. Measured on the live session manager: 1800 on the old stack, None after the bump. `main()` now sets 1800 unless `FASTMCP_HTTP_SESSION_IDLE_TIMEOUT` is set. The `if __name__ == "__main__"` block became `main()` so a test can run it.
- **LAN clients still get through.** `tests/test_lan_host.py` boots `server.py` and requires an MCP `initialize` sent with `Host: 192.168.86.20:3713` to be answered 200. Its control boots the same server bound to loopback with FastMCP's host guard on and requires 421. The same file reads the idle timeout off the live session manager and proves a 1 second override reaps a real session.
- The image sets `FASTMCP_CHECK_FOR_UPDATES=off`, so the server no longer asks pypi.org for FastMCP's latest version on every start.
- `pytest.ini` fails on any `FastMCPDeprecationWarning`, the bridge FastMCP 4 keeps for camelCase protocol field reads.

No tool, transport, or port changes. No client re-registration needed.

## 2026-08-25 — Hold the error contract for failures nobody anticipated

Every tool here documents what it returns on failure, and the three live tools handle the failures they expect. Two kinds got past all of it, from the overnight code review.

- **A non-JSON 200 from Greenhouse.** Every live tool calls `.json()` on the response, which raises `JSONDecodeError`. That is not an httpx exception, so it was in none of the caught tuples: `(HTTPStatusError, TransportError, TimeoutException)`. A captive-portal login page, an HTML error page from a CDN, or a truncated body all produce exactly this.
- **A `sqlite3.Error` from any cached-DB tool.** None of the seven caught anything at all. The DB is a read-only mount populated by a cron container on another host, so a partial write, a schema change, or a missing file surfaces here and nowhere else.

`tool_guard` is a backstop below the existing handlers, ported from the decorator already proven in `mcp-fleaflicker`. The specific handlers still run first and keep their richer messages: a 404 stays `{"error": "Job not found", "job_id": ...}` rather than being flattened, and a test pins that, since flattening it would make the guard a downgrade instead of a backstop.

Also corrected a comment that claimed an uncaught exception "would crash the MCP session for Claude". It does not. FastMCP catches a raising tool and returns an `isError` result, and the session survives. What actually happens is that the caller gets a framework-shaped error instead of the documented envelope, so an agent parsing for `error` finds nothing it recognises and treats a hard failure as an unreadable response. Less dramatic, and more likely to be acted on wrongly, because it looks like the tool answered.

- `server.py`: `tool_guard` added and applied to all 10 tools.
- `tests/test_tool_guard.py`: new. First tests to exercise `server.py` at all. Covers both escapes, the success path, and that the 404 handler is not shadowed. One test walks the live tool registry and fails if any tool is unguarded, which is how this regresses when tool 11 is added; verified by removing one decorator and watching it fail.

No tool, transport, or port changes. No client re-registration needed.

## 2026-08-13 — Drop pip from the runtime image

The Trivy image scan failed on two HIGH findings that came from the base image, not from this project's dependencies.

- **GHSA-6v7p-g79w-8964 (HIGH, msgpack 1.1.2):** out of bounds read and crash on `Unpacker` reuse. Fixed upstream in 1.2.1.
- **CVE-2025-47273 (HIGH, setuptools 70.3.0):** path traversal in `PackageIndex`. Fixed upstream in 78.1.1.

Neither package appears in `requirements.lock`. Both are vendored inside pip itself, at `pip/_vendor/msgpack` and `pip/_vendor/pkg_resources`, which is why the filesystem scan stayed green while the image scan failed. They arrived with pip 26.2.1 in the base image bump from `cea0e60` to `ce40764`.

Vendored copies cannot be upgraded on their own, so the fix is to delete pip once the install is done. Nothing at runtime uses it: neither `server.py` nor `healthcheck.py` shells out to pip.

- `Dockerfile`: base digest bumped to `ce40764`, and the install layer now runs `pip uninstall -y pip` followed by an explicit removal of `site-packages/pip*`, `ensurepip`, and the `/usr/local/bin/pip*` shims.
- Verified on nix1 before push: `trivy image --severity HIGH,CRITICAL --ignore-unfixed` exits 0 against the rebuilt image, the container starts against the `anthropic-tracker-data` volume, and `healthcheck.py` exits 0.

No tool, transport, or port changes. No client re-registration needed.

## 2026-05-11 — Security patch: fastmcp 3.2.4 (0.2.1)

Bumped `fastmcp` 3.1.0 -> 3.2.4 to pick up upstream security fixes. No tool or transport changes; semver patch.

- **CVE-2026-32871 (CRITICAL, SSRF in fastmcp):** unauthenticated server-side request forgery via the FastMCP HTTP transport in 3.1.x. Patched in 3.2.0.
- **CVE-2026-27124 (HIGH, OAuthProxy):** OAuth proxy path could leak/forward credentials in 3.1.x. Patched in 3.2.x.
- `requirements.txt` and `requirements.lock` regenerated with hash-pinned wheels for 3.2.4. Lockfile remains Python 3.13.
- `docker-compose.yml`: image tag 0.2.0 -> 0.2.1. Port 3713, Streamable HTTP transport, `/data:ro` mount, hardening all unchanged.

No client re-registration needed. Existing `claude mcp add anthropic-tracker -t http -s user http://<host>:3713/mcp` registrations keep working.

## 2026-05-11 — Streamable HTTP transport (0.2.0)

Migrated from SSE to Streamable HTTP per the current MCP spec (pure SSE was superseded 2025-03-26). Same port 3713, same 10 tools, same return shapes — only the transport and endpoint path change.

- `server.py`: `mcp.run(transport="streamable-http", ...)` instead of `"sse"`. Env vars switched to `FASTMCP_HOST` / `FASTMCP_PORT` (legacy `MCP_HOST` / `MCP_PORT` still honored as fallback).
- `healthcheck.py`: hits `/mcp` and treats HTTP 400/405/406 as healthy (Streamable HTTP rejects bare GETs — that response confirms FastMCP is listening and routing).
- `docker-compose.yml`: image tag bumped to `0.2.0`, env vars updated to `FASTMCP_*`.
- README: endpoint path documented as `/mcp`, registration command updated to `claude mcp add anthropic-tracker -t http -s user http://<host>:3713/mcp`.

Breaking change for clients: SSE registrations against `:3713/sse` will fail. Re-register with the new command.

## 2026-04-29 — Live Greenhouse tools (3 added, 10 total)

Added `live_jobs`, `live_job_detail`, `live_compensation` for queries that need data fresher than the daily cron's last snapshot.

- New: `clients/greenhouse.py` — async httpx client with exponential-backoff retries (1s, 2s; max 2 retries; 30s timeout; UA `anthropic-tracker-mcp/0.1.0`).
- New: `clients/parser.py` — copy of the upstream tracker's compensation parser. One small divergence vs upstream: `parse_compensation()` now `html.unescape()`s its input. The upstream tracker hits the bulk `/jobs?content=true` endpoint which returns raw HTML, but this MCP's `live_compensation` calls the per-job `/jobs/{id}` endpoint which returns HTML-entity-encoded content (`&lt;div&gt;...`). Without `html.unescape`, the parser silently returned `None` on every live job. Flagged in the file header so anyone re-syncing from upstream knows about the divergence.
- The bare `/jobs` endpoint omits department data; only `/departments` has it. `live_jobs` calls both and stitches with `enrich_jobs_with_departments()` so the `department` filter has data to match on.
- Bumped `requirements.txt` with `httpx>=0.27,<1` and `beautifulsoup4>=4.12,<5`.

## Initial release — 7 cached DB tools

- `search_jobs`, `recent_changes`, `compensation_for`, `department_trends`, `active_alerts`, `daily_summary`, `db_stats`.
- Read-only mount + `mode=ro&immutable=1` SQLite URI. No write code paths anywhere.
- FastMCP + SSE transport on port 3713, single Docker container, `unless-stopped` restart policy.
