"""Explainable fit scoring against a job seeker's profile.

A score is a sum of named parts, each with a reason string, so an agent can
say *why* a role ranked where it did and a person can disagree with a part
rather than with a number. Hard blockers zero the score and say which rule
fired. Nothing here calls a model: the ranking is reproducible from the
profile and the posting alone.

Profile shape (every key optional; see profile.example.yaml):

    target_titles:   ["AI Enablement", "AI Solutions Architect"]
    title_keywords:  ["AI", "enablement", "automation"]
    must_have_any:   ["Claude", "LLM", "MCP"]       # description, any one
    nice_to_have:    ["governance", "Copilot"]       # description, each adds
    avoid_titles:    ["intern", "sales development"] # title, blocker
    avoid_keywords:  ["clearance required"]          # description, blocker
    seniority:       {exclude: ["junior"], prefer: ["senior", "lead"]}
    location:        {remote: true, remote_regions: ["US", "United States"],
                      onsite_ok: ["New York"], hybrid_ok: false}
    salary:          {floor: 140000, target: 160000}
    companies:       {prefer: ["anthropic"], avoid: ["acme"]}
    max_age_days:    45
"""

from __future__ import annotations

import re
from datetime import date

from hub.store import local_today
from hub.text import us_status

TITLE_GATE = 30
_US_WORDS = {"us", "usa", "united states", "north america", "americas"}

WEIGHTS = {"title": 35, "skills": 25, "location": 15, "salary": 15, "freshness": 5, "company": 5}

def _lower_list(values) -> list[str]:
    return [str(v).lower() for v in (values or []) if str(v).strip()]


def _word_hit(term: str, text: str) -> bool:
    """Whole-word match so "AI" does not hit "maintain" or "email"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _age_days(job: dict) -> int | None:
    stamp = (job.get("posted_at") or job.get("first_seen") or "")[:10]
    try:
        return (local_today() - date.fromisoformat(stamp)).days
    except ValueError:
        return None


def score_job(job: dict, profile: dict) -> dict:
    """Score one job dict (as stored) against a profile.

    Returns ``{score, blockers, parts, reasons, flags}``. ``score`` is 0-100.
    ``flags`` are facts worth raising that are not scored, such as an unposted
    salary or a posting open long enough to be a ghost job.
    """
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    location = (job.get("location") or "").lower()
    company = (job.get("company_name") or "").lower()
    slug = (job.get("company_slug") or "").lower()
    parts: dict[str, int] = {}
    reasons: list[str] = []
    blockers: list[str] = []
    flags: list[str] = []

    # ------------------------------------------------------------- blockers
    for term in _lower_list(profile.get("avoid_titles")):
        if _word_hit(term, title):
            blockers.append(f'title contains "{term}"')
    for term in _lower_list(profile.get("avoid_keywords")):
        if _word_hit(term, desc):
            blockers.append(f'description contains "{term}"')
    seniority = profile.get("seniority") or {}
    for term in _lower_list(seniority.get("exclude")):
        if _word_hit(term, title):
            blockers.append(f'seniority "{term}" excluded')
    companies = profile.get("companies") or {}
    if any(c in (company, slug) for c in _lower_list(companies.get("avoid"))):
        blockers.append(f"company {job.get('company_name')} on avoid list")

    # ---------------------------------------------------------------- title
    targets = _lower_list(profile.get("target_titles"))
    keywords = _lower_list(profile.get("title_keywords"))
    exact = next((t for t in targets if t in title), None)
    if exact:
        parts["title"] = WEIGHTS["title"]
        reasons.append(f'title matches target "{exact}"')
    else:
        target_words = {w for t in targets for w in t.split() if len(w) > 1}
        pool = set(keywords) | target_words
        hits = sorted(w for w in pool if _word_hit(w, title))
        if pool and hits:
            share = min(1.0, len(hits) / max(2, min(4, len(pool))))
            parts["title"] = round(WEIGHTS["title"] * 0.8 * share)
            reasons.append(f"title keywords: {', '.join(hits)}")
        else:
            parts["title"] = 0
    title_hit = parts["title"] > 0
    prefer = _lower_list(seniority.get("prefer"))
    if prefer and any(_word_hit(p, title) for p in prefer):
        parts["title"] = min(WEIGHTS["title"], parts["title"] + 5)
        reasons.append("preferred seniority")

    # --------------------------------------------------------------- skills
    must = _lower_list(profile.get("must_have_any"))
    nice = _lower_list(profile.get("nice_to_have"))
    hay = f"{title} {desc}"
    must_hits = [t for t in must if _word_hit(t, hay)]
    nice_hits = [t for t in nice if _word_hit(t, hay)]
    skills = 0
    if must:
        if must_hits:
            skills += 15
            reasons.append(f"has: {', '.join(must_hits[:6])}")
        elif desc:
            flags.append("none of the must-have skills appear in the posting")
    else:
        skills += 15
    if nice:
        skills += round(10 * min(1.0, len(nice_hits) / max(1, min(4, len(nice)))))
        if nice_hits:
            reasons.append(f"also mentions: {', '.join(nice_hits[:6])}")
    parts["skills"] = min(WEIGHTS["skills"], skills)

    # ------------------------------------------------------------- location
    loc = profile.get("location") or {}
    regions = set(_lower_list(loc.get("remote_regions")))
    onsite_ok = _lower_list(loc.get("onsite_ok"))
    wants_remote = loc.get("remote", True)
    wants_us = bool(regions & _US_WORDS)
    where = job.get("location") or ""
    status = us_status(where)
    remote = job.get("remote")
    if any(o in location for o in onsite_ok):
        parts["location"] = WEIGHTS["location"]
        reasons.append(f"location ok: {where}")
    elif remote and wants_remote:
        if not wants_us or status in ("us", "mixed"):
            parts["location"] = WEIGHTS["location"]
            reasons.append("remote" + (" (US)" if status in ("us", "mixed") else ""))
        elif status == "unknown":
            parts["location"] = 12
            reasons.append("remote")
        else:
            parts["location"] = 3
            flags.append(f"remote but region-limited: {where}")
    elif remote is None:
        if wants_remote and wants_us and status == "non_us":
            parts["location"] = 0
            blockers.append(f"outside the US: {where}")
        elif not where:
            parts["location"] = 7
            flags.append("location not stated")
        else:
            parts["location"] = 4
            flags.append(f"location {where} with no remote option stated: confirm before applying")
    else:
        parts["location"] = 0
        # onsite_ok is a whitelist: a remote-seeking profile that names Ithaca
        # still does not want Austin on-site.
        if wants_remote:
            blockers.append(f"not remote: {where or 'on-site'}")
        else:
            flags.append(f"location outside preferences: {where}")

    # --------------------------------------------------------------- salary
    pay = profile.get("salary") or {}
    floor, target = pay.get("floor"), pay.get("target")
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if hi:
        span = f"${lo:,}-${hi:,}" if lo else f"up to ${hi:,}"
        if job.get("salary_period") == "hour":
            span += " (annualised from hourly)"
        if target and hi >= target:
            parts["salary"] = WEIGHTS["salary"]
            reasons.append(f"pay {span} reaches target")
        elif floor and hi >= floor:
            parts["salary"] = 10
            reasons.append(f"pay {span} clears floor")
        elif floor:
            parts["salary"] = 0
            blockers.append(f"pay {span} tops out under floor ${floor:,}")
        else:
            parts["salary"] = 10
    else:
        parts["salary"] = 6
        if floor:
            flags.append("salary not posted: ask at the screen")

    # ------------------------------------------------------------ freshness
    age = _age_days(job)
    max_age = profile.get("max_age_days") or 45
    if age is None:
        parts["freshness"] = 2
    elif age <= 7:
        parts["freshness"] = WEIGHTS["freshness"]
        reasons.append(f"posted {age}d ago")
    elif age <= max_age:
        parts["freshness"] = 3
    else:
        parts["freshness"] = 0
        flags.append(f"open {age} days: may be filled or evergreen")
    if job.get("repost_of"):
        flags.append("reposted: a prior listing with this title closed recently")

    # -------------------------------------------------------------- company
    if any(c in (company, slug) for c in _lower_list(companies.get("prefer"))):
        parts["company"] = WEIGHTS["company"]
        reasons.append("preferred company")
    else:
        parts["company"] = 0

    score = 0 if blockers else min(100, sum(parts.values()))
    # Title is the strongest signal there is. A role whose title touches none
    # of the targets can still collect skills, pay and freshness points, which
    # is how "Senior Privacy Counsel" at an AI company scored 61 before this.
    if not title_hit and (targets or keywords) and score > TITLE_GATE:
        score = TITLE_GATE
        flags.append("title matches no target title or keyword")
    return {"score": score, "blockers": blockers, "parts": parts, "reasons": reasons, "flags": flags}


def rank(jobs: list[dict], profile: dict, min_score: int = 0, limit: int = 25) -> list[dict]:
    """Score, drop blocked and low scorers, and sort best first."""
    ranked = []
    for job in jobs:
        fit = score_job(job, profile)
        if fit["blockers"] or fit["score"] < min_score:
            continue
        slim = {k: v for k, v in job.items() if k != "description"}
        slim["fit"] = fit
        ranked.append(slim)
    ranked.sort(key=lambda j: (j["fit"]["score"], j.get("first_seen") or ""), reverse=True)
    return ranked[:limit]
