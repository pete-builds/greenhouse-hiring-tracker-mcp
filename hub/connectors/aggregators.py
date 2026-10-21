"""Multi-company job boards with free public APIs, searched live.

These are not synced into the store: they are queried on demand, and their
results come back tagged with the source and its attribution so an agent can
then add the employer's own ATS board when a hit looks worth tracking.

Terms each source sets, read off the responses themselves on 2026-10-20:

* RemoteOK: element 0 of the feed is a legal notice asking for a link back and
  a mention of Remote OK as the source. Every result carries ``attribution``.
* Remotive: jobs are delayed 24h and the API asks for at most 4 calls a day,
  so results are cached for six hours per query and the source is opt-in.
* Himalayas: search pages 20 at a time, no key.
* Hacker News "Who is hiring?": Algolia's public HN API, top-level comments of
  the newest monthly thread only.
"""

from __future__ import annotations

import html as _html
import re
import time

from hub.connectors.base import iso_date
from hub.http import Http
from hub.text import detect_remote, html_to_text, parse_salary

_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _cached(key: str, ttl: float) -> list[dict] | None:
    hit = _CACHE.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < ttl else None


def _store(key: str, rows: list[dict]) -> list[dict]:
    _CACHE[key] = (time.monotonic(), rows)
    return rows


def _matches(query: str, *fields: str | None) -> bool:
    hay = " ".join(f for f in fields if f).lower()
    groups = [g.split() for g in query.lower().split(",") if g.strip()]
    return not groups or any(all(w in hay for w in g) for g in groups)


def _row(source: str, **kw) -> dict:
    kw["source"] = source
    desc = kw.pop("description", None) or ""
    if kw.get("salary_max") is None:
        comp = parse_salary(desc)
        if comp:
            kw["salary_min"], kw["salary_max"] = comp["min"], comp["max"]
            kw["salary_currency"] = comp["currency"]
    if kw.get("remote") is None:
        kw["remote"] = detect_remote(kw.get("location"), kw.get("title"))
    kw["snippet"] = desc[:400]
    kw["description"] = desc
    return kw


async def remoteok(http: Http, query: str) -> list[dict]:
    feed = _cached("remoteok", 3600)
    if feed is None:
        data = await http.json("https://remoteok.com/api")
        feed = _store("remoteok", [x for x in data if isinstance(x, dict) and x.get("id")])
    out = []
    for j in feed:
        tags = " ".join(j.get("tags") or [])
        desc = html_to_text(j.get("description"))
        if not _matches(query, j.get("position"), j.get("company"), tags, desc):
            continue
        out.append(_row(
            "remoteok", company=j.get("company"), title=j.get("position"), url=j.get("url"),
            location=j.get("location") or "Remote", remote=True, posted_at=iso_date(j.get("date")),
            salary_min=j.get("salary_min") or None, salary_max=j.get("salary_max") or None,
            salary_currency="USD" if j.get("salary_max") else None, description=desc,
            attribution="Remote OK (https://remoteok.com)",
        ))
    return out


async def himalayas(http: Http, query: str, pages: int = 2) -> list[dict]:
    out = []
    for page in range(1, pages + 1):
        data = await http.json("https://himalayas.app/jobs/api/search", params={"q": query, "page": page})
        jobs = data.get("jobs") or []
        for j in jobs:
            regions = j.get("locationRestrictions") or []
            out.append(_row(
                "himalayas", company=j.get("companyName"), title=j.get("title"),
                url=j.get("applicationLink") or j.get("guid"),
                location="Remote" + (f" ({', '.join(regions)})" if regions else ""), remote=True,
                posted_at=iso_date(j.get("pubDate")),
                salary_min=j.get("minSalary"), salary_max=j.get("maxSalary"),
                salary_currency=j.get("currency"),
                description=html_to_text(j.get("description")) or j.get("excerpt"),
                attribution="Himalayas (https://himalayas.app)",
            ))
        if len(jobs) < 20:
            break
    return out


async def remotive(http: Http, query: str) -> list[dict]:
    key = f"remotive:{query.lower()}"
    rows = _cached(key, 6 * 3600)
    if rows is not None:
        return rows
    data = await http.json("https://remotive.com/api/remote-jobs", params={"search": query})
    rows = [
        _row(
            "remotive", company=j.get("company_name"), title=j.get("title"), url=j.get("url"),
            location=j.get("candidate_required_location") or "Remote", remote=True,
            posted_at=iso_date(j.get("publication_date")),
            description=html_to_text(j.get("description")) + " " + (j.get("salary") or ""),
            attribution="Remotive (https://remotive.com)",
        )
        for j in data.get("jobs") or []
    ]
    return _store(key, rows)


async def hackernews(http: Http, query: str) -> list[dict]:
    threads = _cached("hn:threads", 6 * 3600)
    if threads is None:
        data = await http.json(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"tags": "story,author_whoishiring", "hitsPerPage": 6},
        )
        threads = _store("hn:threads", [
            h for h in data.get("hits") or [] if "who is hiring" in (h.get("title") or "").lower()
        ])
    if not threads:
        return []
    story = threads[0]
    data = await http.json(
        "https://hn.algolia.com/api/v1/search",
        params={"tags": f"comment,story_{story['objectID']}", "query": query, "hitsPerPage": 50},
    )
    out = []
    for h in data.get("hits") or []:
        if str(h.get("parent_id")) != str(story["objectID"]):
            continue  # replies, not postings
        text = html_to_text(h.get("comment_text"))
        head = _html.unescape(re.split(r"<p>", h.get("comment_text") or "", maxsplit=1)[0])
        cells = [c.strip() for c in re.sub(r"<[^>]+>", "", head).split("|")]
        out.append(_row(
            "hackernews", company=cells[0][:80] if cells else None,
            title=cells[1][:120] if len(cells) > 1 else text[:80],
            url=f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            location=next((c for c in cells[2:] if detect_remote(c) is not None or "," in c), None),
            posted_at=iso_date(h.get("created_at")), description=text,
            attribution=f"Hacker News: {story.get('title')}",
        ))
    return out


AGGREGATORS = {
    "himalayas": himalayas,
    "remoteok": remoteok,
    "hackernews": hackernews,
    "remotive": remotive,
}
#: Remotive asks for at most four calls a day, so it runs only when named.
DEFAULT_AGGREGATORS = ("himalayas", "remoteok", "hackernews")
