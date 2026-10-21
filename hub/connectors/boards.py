"""Connectors whose public list endpoint returns the whole board in one call.

Field names below were read off live responses on 2026-10-20 (the fixtures in
tests/fixtures/ are trimmed copies), not recalled from vendor docs.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import httpx

from hub.connectors.base import HOST_SLUG, Connector, annual, make_job
from hub.http import BadPayload, Http
from hub.models import Job
from hub.text import detect_remote

log = logging.getLogger("hiring-hub.connectors")

#: Above this many new or edited Greenhouse jobs, one ``content=true`` list
#: call is cheaper than a detail call each.
GREENHOUSE_BULK_THRESHOLD = 20


def _known_unchanged(known: dict[str, str | None], ext_id, updated) -> bool:
    """True when a stored row has a description and the ATS says it has not changed."""
    key = str(ext_id)
    return key in known and (known[key] or "") == (updated or "")[:10]


class Greenhouse(Connector):
    ats, label, example = "greenhouse", "Greenhouse", "anthropic"
    url_patterns = (
        re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"),
        re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)"),
    )
    api = "https://boards-api.greenhouse.io/v1/boards"

    def board_url(self, slug: str) -> str:
        return f"https://job-boards.greenhouse.io/{slug}"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        known = known or {}
        content = not known
        data = await http.json(f"{self.api}/{slug}/jobs" + ("?content=true" if content else ""))
        if not isinstance(data, dict) or "jobs" not in data:
            raise BadPayload(f"greenhouse/{slug}: no jobs key")
        raw = data["jobs"]
        stale = [j for j in raw if not _known_unchanged(known, j.get("id"), j.get("updated_at"))]
        details: dict = {}
        if not content and stale:
            if len(stale) > GREENHOUSE_BULK_THRESHOLD:
                data = await http.json(f"{self.api}/{slug}/jobs?content=true")
                raw, content = data.get("jobs", []), True
            else:
                for j in stale[:detail_limit]:
                    try:
                        details[j["id"]] = await http.json(f"{self.api}/{slug}/jobs/{j['id']}")
                    except Exception as exc:  # noqa: BLE001 - keep the list; fill in next sync
                        log.debug("greenhouse/%s job %s detail skipped: %s", slug, j["id"], exc)
        out = []
        for j in raw:
            body = j if content else details.get(j.get("id"))
            meta = {m.get("name"): m.get("value") for m in j.get("metadata") or [] if isinstance(m, dict)}
            depts = j.get("departments") or []
            # Boards that use Greenhouse's "Location Type" custom field state
            # it outright; prefer it to guessing from the location string.
            kind = str(meta.get("Location Type") or meta.get("Workplace Type")
                       or meta.get("Work Arrangement") or "").lower()
            remote = True if "remote" in kind else (False if kind in {"on-site", "onsite", "hybrid", "in office"} else None)
            out.append(make_job(
                self, slug, j.get("company_name") or slug,
                external_id=j["id"], title=j.get("title", ""), url=j.get("absolute_url", ""),
                location=(j.get("location") or {}).get("name"), remote=remote,
                department=depts[0].get("name") if depts else None,
                posted_at=j.get("first_published"), updated_at=j.get("updated_at"),
                description_html=(body or {}).get("content"),
                skip_description=body is None,
            ))
        return out


class Lever(Connector):
    ats, label, example = "lever", "Lever", "palantir"
    url_patterns = (re.compile(r"jobs(?:\.eu)?\.lever\.co/([\w.-]+)"),
                    re.compile(r"api(?:\.eu)?\.lever\.co/v0/postings/([\w.-]+)"))

    def board_url(self, slug: str) -> str:
        return f"https://jobs.lever.co/{slug}"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        # EU tenants live on a separate host and 404 on the US one.
        try:
            raw = await http.json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        except httpx.HTTPStatusError:
            raw = await http.json(f"https://api.eu.lever.co/v0/postings/{slug}?mode=json")
        if not isinstance(raw, list):
            raise BadPayload(f"lever/{slug}: expected a list")
        out = []
        for p in raw:
            cats = p.get("categories") or {}
            sal = p.get("salaryRange") or {}
            parts = [p.get("openingPlain"), p.get("descriptionPlain"), p.get("descriptionBodyPlain")]
            for block in p.get("lists") or []:
                parts.append(f"{block.get('text', '')}: {re.sub(r'<[^>]+>', ' ', block.get('content', ''))}")
            parts.append(p.get("additionalPlain"))
            out.append(make_job(
                self, slug, slug,
                external_id=p["id"], title=p.get("text", ""), url=p.get("hostedUrl", ""),
                location=cats.get("location") or ", ".join(cats.get("allLocations") or []),
                remote_hints=(p.get("workplaceType"),),
                department=cats.get("team") or cats.get("department"),
                employment_type=cats.get("commitment"), posted_at=p.get("createdAt"),
                description_text=re.sub(r"\s+", " ", " ".join(x for x in parts if x))[:20_000],
                salary=annual(sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("interval"))
                if sal else None,
            ))
        return out


class Ashby(Connector):
    ats, label, example = "ashby", "Ashby", "openai"
    url_patterns = (re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)"),
                    re.compile(r"posting-api/job-board/([\w.%-]+)"))

    def board_url(self, slug: str) -> str:
        return f"https://jobs.ashbyhq.com/{slug}"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        data = await http.json(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        )
        if not isinstance(data, dict) or "jobs" not in data:
            raise BadPayload(f"ashby/{slug}: no jobs key")
        out = []
        for j in data["jobs"]:
            if j.get("isListed") is False:
                continue
            comp = j.get("compensation") or {}
            salary = None
            for comp_part in comp.get("summaryComponents") or []:
                if comp_part.get("compensationType") == "Salary":
                    salary = annual(comp_part.get("minValue"), comp_part.get("maxValue"),
                                    comp_part.get("currencyCode"), comp_part.get("interval"))
                    break
            locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
            # isRemote is true on "Hybrid - San Francisco" and "SF Office"
            # postings (seen 2026-10-20), so the stated workplace wins.
            remote = detect_remote(j.get("location"), j.get("workplaceType"))
            if remote is None:
                remote = j.get("isRemote")
            out.append(make_job(
                self, slug, slug,
                external_id=j["id"], title=j.get("title", ""), url=j.get("jobUrl", ""),
                location="; ".join(x for x in locs if x), remote=remote,
                department=j.get("department") or j.get("team"),
                employment_type=j.get("employmentType"), posted_at=j.get("publishedAt"),
                description_text=(j.get("descriptionPlain") or "")[:20_000],
                salary=salary, salary_text=comp.get("scrapeableCompensationSalarySummary"),
            ))
        return out


class Workable(Connector):
    ats, label, example = "workable", "Workable", "huggingface"
    url_patterns = (re.compile(r"apply\.workable\.com/(?!api|j/)([\w-]+)"),
                    re.compile(r"([\w-]+)\.workable\.com"))

    def board_url(self, slug: str) -> str:
        return f"https://apply.workable.com/{slug}/"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        data = await http.json(
            f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
        )
        if not isinstance(data, dict) or "jobs" not in data:
            raise BadPayload(f"workable/{slug}: no jobs key")
        name = data.get("name") or slug
        out = []
        for j in data["jobs"]:
            locs = j.get("locations") or []
            where = "; ".join(
                ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
                for loc in locs if not loc.get("hidden")
            ) or ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
            out.append(make_job(
                self, slug, name,
                external_id=j["shortcode"], title=j.get("title", ""), url=j.get("url", ""),
                location=where, remote=True if j.get("telecommuting") else None,
                department=j.get("department"), employment_type=j.get("employment_type"),
                posted_at=j.get("published_on") or j.get("created_at"),
                description_html=j.get("description"),
            ))
        return out


class Recruitee(Connector):
    ats, label, example = "recruitee", "Recruitee", "bunq"
    url_patterns = (re.compile(r"([\w-]+)\.recruitee\.com"),)
    slug_pattern = HOST_SLUG

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.recruitee.com/"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        data = await http.json(f"https://{slug}.recruitee.com/api/offers/")
        if not isinstance(data, dict) or "offers" not in data:
            raise BadPayload(f"recruitee/{slug}: no offers key")
        out = []
        for o in data["offers"]:
            sal = o.get("salary") or {}
            locs = [loc.get("name") for loc in o.get("locations") or [] if loc.get("name")]
            out.append(make_job(
                self, slug, o.get("company_name") or slug,
                external_id=o["id"], title=o.get("title", ""),
                url=o.get("careers_url") or o.get("careers_apply_url", ""),
                location="; ".join(locs) or o.get("location"),
                remote=True if o.get("remote") else (False if o.get("on_site") else None),
                department=o.get("department"), employment_type=o.get("employment_type_code"),
                posted_at=o.get("published_at") or o.get("created_at"),
                updated_at=o.get("updated_at"),
                description_html=f"{o.get('description', '')} {o.get('requirements', '')}",
                salary=annual(sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("period"))
                if sal.get("max") or sal.get("min") else None,
            ))
        return out


class Teamtailor(Connector):
    ats, label, example = "teamtailor", "Teamtailor", "career"
    url_patterns = (re.compile(r"([\w-]+)\.teamtailor\.com"),)
    slug_pattern = HOST_SLUG

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.teamtailor.com/jobs"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        data = await http.json(f"https://{slug}.teamtailor.com/jobs.json")
        if not isinstance(data, dict) or "items" not in data:
            raise BadPayload(f"teamtailor/{slug}: not a JSON feed")
        out = []
        for item in data["items"]:
            posting = item.get("_jobposting") or {}
            place = posting.get("jobLocation") or []
            place = place if isinstance(place, list) else [place]
            where = "; ".join(
                ", ".join(x for x in ((p.get("address") or {}).get("addressLocality"),
                                      (p.get("address") or {}).get("addressCountry")) if x)
                for p in place if isinstance(p, dict)
            )
            base = posting.get("baseSalary") or {}
            value = base.get("value") or {} if isinstance(base, dict) else {}
            out.append(make_job(
                self, slug, (posting.get("hiringOrganization") or {}).get("name") or data.get("title") or slug,
                external_id=item["id"], title=item.get("title", ""), url=item.get("url", ""),
                location=where, remote_hints=(posting.get("jobLocationType"),),
                employment_type=posting.get("employmentType") if isinstance(posting.get("employmentType"), str) else None,
                posted_at=item.get("date_published"), description_html=item.get("content_html"),
                salary=annual(value.get("minValue"), value.get("maxValue"), base.get("currency"),
                              value.get("unitText")) if value else None,
            ))
        return out


class Breezy(Connector):
    ats, label, example = "breezy", "Breezy HR", "breezy"
    url_patterns = (re.compile(r"([\w-]+)\.breezy\.hr"),)
    slug_pattern = HOST_SLUG

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.breezy.hr/"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        raw = await http.json(f"https://{slug}.breezy.hr/json")
        if not isinstance(raw, list):
            raise BadPayload(f"breezy/{slug}: expected a list")
        out = []
        for p in raw:
            loc = p.get("location") or {}
            company = (p.get("company") or {}).get("name") or slug
            out.append(make_job(
                self, slug, company,
                external_id=p["id"], title=p.get("name", ""), url=p.get("url", ""),
                location=loc.get("name"), remote=loc.get("is_remote"),
                department=p.get("department"), employment_type=(p.get("type") or {}).get("name"),
                posted_at=p.get("published_date"), salary_text=p.get("salary"),
                description_text="",
            ))
        return out


class Personio(Connector):
    ats, label, example = "personio", "Personio", "personio"
    url_patterns = (re.compile(r"([\w-]+)\.jobs\.personio\.(?:de|com)"),)
    slug_pattern = HOST_SLUG

    def board_url(self, slug: str) -> str:
        return f"https://{slug}.jobs.personio.de/"

    async def fetch(self, http: Http, slug: str, known=None, detail_limit=150) -> list[Job]:
        text = await http.text(f"https://{slug}.jobs.personio.de/xml")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise BadPayload(f"personio/{slug}: not XML") from exc
        out = []
        for pos in root.iter("position"):
            def field(name: str, node=pos) -> str:
                return (node.findtext(name) or "").strip()

            offices = [field("office")] + [
                (o.text or "").strip() for o in pos.findall("./additionalOffices/office")
            ]
            body = " ".join(
                f"{field('name', d)} {field('value', d)}" for d in pos.findall("./jobDescriptions/jobDescription")
            )
            out.append(make_job(
                self, slug, field("subcompany") or slug,
                external_id=field("id"), title=field("name"),
                url=f"https://{slug}.jobs.personio.de/job/{field('id')}",
                location="; ".join(o for o in offices if o), department=field("department"),
                employment_type=field("schedule"), posted_at=field("createdAt"),
                description_html=body,
            ))
        return out
