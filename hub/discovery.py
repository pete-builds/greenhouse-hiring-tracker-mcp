"""Finding a company's board, and bulk-importing directories of boards.

No ATS publishes a directory of its customers, so "every company on the
platform" is assembled from three places:

1. a careers or job URL, which names its ATS and slug outright (`detect`)
2. a company name, turned into likely slugs and probed on every ATS (`discover`)
3. a CSV directory of ``name,slug[,url]`` rows, for example the per-ATS
   ``companies.csv`` files published by github.com/kalil0321/ats-scrapers
   (`parse_directory`). Each imported row is confirmed live on first sync;
   rows that 404 show up under failing_companies in hub_stats.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re

from hub import connectors
from hub.http import Http

_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|gmbh|ag|sa|plc|ai|labs|hq)\b\.?", re.IGNORECASE)


def candidate_slugs(name: str) -> list[str]:
    """Likely board slugs for a company name, most likely first.

    "Hugging Face, Inc." -> huggingface, hugging-face, huggingfaceinc, ...
    """
    base = re.sub(r"[^\w\s-]", "", name.lower()).strip()
    stripped = _SUFFIXES.sub("", base).strip()
    out: list[str] = []
    for variant in (base, stripped):
        words = variant.split()
        if not words:
            continue
        for slug in ("".join(words), "-".join(words), "_".join(words), words[0]):
            if slug and slug not in out:
                out.append(slug)
    for extra in (f"{out[0]}inc", f"{out[0]}hq", f"{out[0]}ai", f"{out[0]}jobs") if out else ():
        if extra not in out:
            out.append(extra)
    return out[:8]


def _name_tokens(name: str) -> list[str]:
    stripped = _SUFFIXES.sub("", re.sub(r"\(.*?\)", "", name.lower()))
    return [w for w in re.findall(r"[a-z0-9]+", stripped) if len(w) > 1]


def confirm_identity(name: str, slug: str, jobs: list) -> str:
    """Does this board belong to the company named? confirmed / mismatch / unverified.

    A slug is only a guess: "zendesk" on BambooHR and "palo" both answered
    with live boards for unrelated companies when this was built. A board
    counts as confirmed when the company name it reports, or the text of its
    postings, names the company; mismatch when it reports a different name.
    """
    tokens = _name_tokens(name)
    if not tokens or not jobs:
        return "unverified"
    phrase = " ".join(tokens)
    names = {j.company_name.lower() for j in jobs if j.company_name and j.company_name.lower() != slug.lower()}
    if names:
        # "Abnormal Security" reports itself as "Abnormal": the leading word,
        # as a whole word, is enough when the board states a name at all.
        if any(phrase in n or all(t in n for t in tokens) or tokens[0] in n.split() for n in names):
            return "confirmed"
        return "mismatch"
    sample = [j.description.lower() for j in jobs[:10] if j.description]
    if sample:
        hits = sum(1 for d in sample if phrase in d)
        return "confirmed" if hits * 2 >= len(sample) else "mismatch"
    return "unverified"


async def discover(http: Http, name: str, ats: list[str] | None = None) -> list[dict]:
    """Probe every (ats, slug) candidate concurrently; return the live boards.

    Hits are ordered confirmed first, then by open jobs. Treat anything not
    "confirmed" as a guess to check against board_url.
    """
    targets = [a for a in (ats or connectors.PROBE_ORDER) if a in connectors.CONNECTORS]
    slugs = candidate_slugs(name)
    sem = asyncio.Semaphore(12)

    async def probe(ats_name: str, slug: str) -> dict | None:
        async with sem:
            jobs = await connectors.get(ats_name).probe_jobs(http, slug)
        if jobs is None:
            return None
        return {"ats": ats_name, "slug": slug, "open_jobs": len(jobs),
                "identity": confirm_identity(name, slug, jobs),
                "board_url": connectors.get(ats_name).board_url(slug)}

    found = await asyncio.gather(*(probe(a, s) for a in targets for s in slugs))
    hits = [f for f in found if f]
    rank = {"confirmed": 0, "unverified": 1, "mismatch": 2}
    hits.sort(key=lambda h: (rank[h["identity"]], -h["open_jobs"]))
    return hits


def parse_directory(text: str, ats: str | None = None) -> list[dict]:
    """Parse a ``name,slug[,url]`` or ``ats,slug,name`` CSV into company rows.

    Accepts a header row with any of: name, company, slug, token, board,
    ats, url, careers_url, tags. Rows without a usable slug are skipped and
    counted by the caller.
    """
    reader = csv.DictReader(io.StringIO(text))
    fields = {f.lower().strip(): f for f in reader.fieldnames or []}

    def pick(row: dict, *names: str) -> str:
        for n in names:
            if n in fields and row.get(fields[n]):
                return row[fields[n]].strip()
        return ""

    rows = []
    for row in reader:
        url = pick(row, "url", "careers_url", "board_url")
        named_ats = pick(row, "ats", "platform", "source").lower()
        slug = pick(row, "slug", "token", "board", "board_token", "identifier")
        hit = connectors.detect(url) if url else None
        if hit and not slug:
            # The URL names both halves; it outranks the import's default ats.
            row_ats, slug = named_ats or hit[0], hit[1]
        else:
            row_ats = named_ats or (hit[0] if hit else "") or (ats or "").lower()
        if not slug or row_ats not in connectors.CONNECTORS:
            continue
        tags = [t.strip() for t in pick(row, "tags").replace(";", ",").split(",") if t.strip()]
        rows.append({"ats": row_ats, "slug": slug, "name": pick(row, "name", "company") or slug,
                     "careers_url": url or None, "tags": tags})
    return rows
