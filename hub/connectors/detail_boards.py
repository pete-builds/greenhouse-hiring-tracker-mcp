"""Connectors whose list endpoint has no description: one detail call per job.

New jobs, and stored jobs that still lack a description, get detail calls up to
``detail_limit`` per sync. Everything else comes back with
``skip_description=True`` so the store keeps what it already holds. A large
board therefore fills in over a few syncs instead of in one burst.
"""

from __future__ import annotations

import asyncio
import re

from hub.connectors.base import Connector, annual, make_job
from hub.http import BadPayload, Http
from hub.models import Job
from hub.text import html_to_text

#: Workday caps `limit` at 20 (21 returns HTTP 400) and stops reporting
#: `total` after the first page; past 2,000 it silently wraps to page one.
WORKDAY_PAGE = 20
WORKDAY_CAP = 2000
SMARTRECRUITERS_PAGE = 100


async def _detail(http: Http, url: str) -> dict | None:
    try:
        data = await http.json(url)
    except Exception:  # noqa: BLE001 - a missing detail never sinks the board
        return None
    return data if isinstance(data, dict) else None


async def _details(http: Http, wanted: dict[str, str], limit: int) -> dict[str, dict | None]:
    """Fetch up to ``limit`` detail URLs concurrently, keyed by external id.

    Concurrency is bounded by Http's per-host cap, so this is four requests in
    flight to one vendor, not ``limit``. Sequential calls made a 150-detail
    Workday sync take minutes.
    """
    picked = list(wanted.items())[: max(0, limit)]
    bodies = await asyncio.gather(*(_detail(http, url) for _, url in picked))
    return {ext: body for (ext, _), body in zip(picked, bodies, strict=True)}


class SmartRecruiters(Connector):
    ats, label, example = "smartrecruiters", "SmartRecruiters", "BoschGroup"
    url_patterns = (re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
                    re.compile(r"api\.smartrecruiters\.com/v1/companies/([\w-]+)"))
    api = "https://api.smartrecruiters.com/v1/companies"

    def board_url(self, slug: str) -> str:
        return f"https://jobs.smartrecruiters.com/{slug}"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        known = known or {}
        items: list[dict] = []
        offset = 0
        while True:
            page = await http.json(
                f"{self.api}/{slug}/postings?limit={SMARTRECRUITERS_PAGE}&offset={offset}"
            )
            if not isinstance(page, dict) or "content" not in page:
                raise BadPayload(f"smartrecruiters/{slug}: no content key")
            items += page["content"]
            offset += SMARTRECRUITERS_PAGE
            if not page["content"] or offset >= int(page.get("totalFound") or 0):
                break
        bodies = await _details(http, {
            str(p["id"]): f"{self.api}/{slug}/postings/{p['id']}"
            for p in items if str(p["id"]) not in known
        }, detail_limit)
        out = []
        for p in items:
            ext = str(p["id"])
            body = bodies.get(ext)
            sections = ((body or {}).get("jobAd") or {}).get("sections") or {}
            loc = p.get("location") or {}
            out.append(make_job(
                self, slug, (p.get("company") or {}).get("name") or slug,
                external_id=ext, title=p.get("name", ""),
                url=(body or {}).get("postingUrl") or f"https://jobs.smartrecruiters.com/{slug}/{ext}",
                location=loc.get("fullLocation"),
                remote=True if loc.get("remote") else None,
                department=(p.get("department") or {}).get("label") or (p.get("function") or {}).get("label"),
                employment_type=(p.get("typeOfEmployment") or {}).get("label"),
                posted_at=p.get("releasedDate"),
                description_html=" ".join(s.get("text", "") for s in sections.values() if isinstance(s, dict)),
                skip_description=body is None,
            ))
        return out


class BambooHR(Connector):
    ats, label, example = "bamboohr", "BambooHR", "linode"
    url_patterns = (re.compile(r"([\w-]+)\.bamboohr\.com"),)

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.bamboohr.com/careers"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        known = known or {}
        data = await http.json(f"https://{slug}.bamboohr.com/careers/list")
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise BadPayload(f"bamboohr/{slug}: no result list")
        details = await _details(http, {
            str(j["id"]): f"https://{slug}.bamboohr.com/careers/{j['id']}/detail"
            for j in data["result"] if str(j["id"]) not in known
        }, detail_limit)
        out = []
        for j in data["result"]:
            ext = str(j["id"])
            body = ((details.get(ext) or {}).get("result") or {}).get("jobOpening")
            loc = (body or j).get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("addressCountry")) if x)
            comp = (body or {}).get("compensation")
            out.append(make_job(
                self, slug, slug,
                external_id=ext, title=j.get("jobOpeningName", ""),
                url=f"https://{slug}.bamboohr.com/careers/{ext}",
                location=where, remote=True if j.get("isRemote") else None,
                department=j.get("departmentLabel"),
                employment_type=j.get("employmentStatusLabel"),
                posted_at=(body or {}).get("datePosted"),
                description_html=(body or {}).get("description"),
                salary_text=comp if isinstance(comp, str) else None,
                skip_description=body is None,
            ))
        return out


class Rippling(Connector):
    ats, label, example = "rippling", "Rippling", "rippling"
    url_patterns = (re.compile(r"ats\.rippling\.com/([\w-]+)"),
                    re.compile(r"/ats/v1/board/([\w-]+)"))
    api = "https://api.rippling.com/platform/api/ats/v1/board"

    def board_url(self, slug: str) -> str:
        return f"https://ats.rippling.com/{slug}/jobs"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        known = known or {}
        raw = await http.json(f"{self.api}/{slug}/jobs")
        if not isinstance(raw, list):
            raise BadPayload(f"rippling/{slug}: expected a list")
        bodies = await _details(http, {
            str(j["uuid"]): f"{self.api}/{slug}/jobs/{j['uuid']}" for j in raw if str(j["uuid"]) not in known
        }, detail_limit)
        out = []
        for j in raw:
            ext = str(j["uuid"])
            body = bodies.get(ext)
            desc = (body or {}).get("description")
            if isinstance(desc, dict):
                desc = " ".join(str(v) for v in desc.values())
            salary = None
            for pay in (body or {}).get("payRangeDetails") or []:
                salary = annual(pay.get("rangeStart"), pay.get("rangeEnd"), pay.get("currency"),
                                pay.get("frequency"))
                if salary:
                    break
            out.append(make_job(
                self, slug, slug,
                external_id=ext, title=j.get("name", ""), url=j.get("url", ""),
                location=(j.get("workLocation") or {}).get("label"),
                department=(j.get("department") or {}).get("label"),
                employment_type=((body or {}).get("employmentType") or {}).get("id"),
                posted_at=(body or {}).get("createdOn"),
                description_html=desc, salary=salary, skip_description=body is None,
            ))
        return out


class Workday(Connector):
    """Slug is ``tenant/wdN/site``, e.g. ``nvidia/wd5/NVIDIAExternalCareerSite``."""

    ats, label, example = "workday", "Workday", "nvidia/wd5/NVIDIAExternalCareerSite"
    url_patterns = (
        re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[\w-]+/)?(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"),
    )

    def slug_from_url(self, url: str) -> str | None:
        m = self.url_patterns[0].search(url)
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else None

    @staticmethod
    def _parts(slug: str) -> tuple[str, str, str]:
        parts = slug.split("/")
        if len(parts) != 3:
            raise ValueError("workday slug must be tenant/wdN/site, e.g. nvidia/wd5/NVIDIAExternalCareerSite")
        return parts[0], parts[1], parts[2]

    def board_url(self, slug: str) -> str:
        tenant, wd, site = self._parts(slug)
        return f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        known = known or {}
        tenant, wd, site = self._parts(slug)
        base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        postings: list[dict] = []
        total = None
        offset = 0
        while offset < WORKDAY_CAP:
            page = await http.json(
                f"{base}/jobs", method="POST",
                json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": ""},
            )
            if not isinstance(page, dict) or "jobPostings" not in page:
                raise BadPayload(f"workday/{slug}: no jobPostings key")
            if total is None:
                total = int(page.get("total") or 0)
            batch = page["jobPostings"]
            postings += batch
            offset += WORKDAY_PAGE
            if len(batch) < WORKDAY_PAGE or offset >= total:
                break
        def ext_of(p: dict) -> str:
            path = p.get("externalPath") or ""
            return (p.get("bulletFields") or [path])[0] or path

        infos = await _details(http, {
            ext_of(p): f"{base}{p.get('externalPath') or ''}" for p in postings if ext_of(p) not in known
        }, detail_limit)
        out = []
        seen: set[str] = set()
        for p in postings:
            path = p.get("externalPath") or ""
            ext = ext_of(p)
            if ext in seen:
                continue
            seen.add(ext)
            info = (infos.get(ext) or {}).get("jobPostingInfo")
            where = p.get("locationsText")
            if info:
                where = "; ".join([info.get("location") or ""] + list(info.get("additionalLocations") or []))
            out.append(make_job(
                self, slug, tenant,
                external_id=ext, title=p.get("title", ""),
                url=(info or {}).get("externalUrl") or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
                location=where, employment_type=(info or {}).get("timeType"),
                posted_at=(info or {}).get("startDate") or p.get("postedOn"),
                description_text=html_to_text((info or {}).get("jobDescription")) if info else None,
                skip_description=info is None,
            ))
        return out
