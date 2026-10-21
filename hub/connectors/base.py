"""The contract every ATS connector implements."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from hub.http import Http
from hub.models import Job
from hub.store import local_today
from hub.text import detect_remote, html_to_text, parse_salary

#: Per sync, at most this many per-job detail calls for ATSes whose list
#: endpoint carries no description. New and changed jobs get them first;
#: the rest fill in on later syncs. Keeps a 2,000-job Workday board from
#: turning one sync into 2,000 requests.
DEFAULT_DETAIL_LIMIT = 150


class Connector:
    """One ATS. Subclasses set the class attributes and implement `fetch`.

    ``fetch(http, slug, known)`` returns the company's complete open board.
    ``known`` maps external ids already stored to True when that row has a
    description. A connector that needs one call per job for descriptions
    skips those calls for known rows and returns ``description=None``, which
    the store reads as "unchanged, keep what you have".
    """

    ats: str = ""
    label: str = ""
    #: Regexes over a careers URL; group 1 is the slug.
    url_patterns: tuple[re.Pattern, ...] = ()
    #: Example slug, shown in tool docs and validation errors.
    example: str = ""

    def board_url(self, slug: str) -> str:
        raise NotImplementedError

    async def fetch(
        self, http: Http, slug: str, known: dict[str, bool] | None = None,
        detail_limit: int = DEFAULT_DETAIL_LIMIT,
    ) -> list[Job]:
        raise NotImplementedError

    async def probe(self, http: Http, slug: str) -> int | None:
        """Return the open-job count if ``slug`` is a live board, else None."""
        jobs = await self.probe_jobs(http, slug)
        return None if jobs is None else len(jobs)

    async def probe_jobs(self, http: Http, slug: str) -> list[Job] | None:
        """The board's jobs (descriptions where the list call carries them), or None."""
        try:
            return await self.fetch(http, slug, known={}, detail_limit=0)
        except Exception:  # noqa: BLE001 - any failure means "not this ATS"
            return None

    def slug_from_url(self, url: str) -> str | None:
        for pattern in self.url_patterns:
            m = pattern.search(url)
            if m:
                return m.group(1)
        return None


def make_job(
    connector: Connector,
    slug: str,
    company_name: str,
    *,
    external_id,
    title: str,
    url: str,
    location: str | None = None,
    remote: bool | None = None,
    remote_hints: tuple[str | None, ...] = (),
    department: str | None = None,
    employment_type: str | None = None,
    posted_at=None,
    updated_at=None,
    description_html: str | None = None,
    description_text: str | None = None,
    salary: dict | None = None,
    salary_text: str | None = None,
    skip_description: bool = False,
) -> Job:
    """Build a Job, filling the derived fields every connector needs.

    Salary precedence: a structured ``salary`` dict from the ATS, else a range
    parsed from ``salary_text``, else one parsed from the description.
    """
    description = None
    if not skip_description:
        description = description_text if description_text is not None else html_to_text(
            description_html
        )
    if remote is None:
        remote = detect_remote(location, title, *remote_hints)
    comp = salary
    source = "structured" if salary else None
    if not comp and salary_text:
        comp, source = parse_salary(salary_text), "posted"
    if not comp and description:
        comp, source = parse_salary(description), "parsed"
    return Job(
        source=connector.ats,
        company_slug=slug,
        company_name=(company_name or slug).strip(),
        external_id=str(external_id),
        title=" ".join((title or "").split()),
        url=url,
        location=(location or None) and " ".join(location.split()),
        remote=remote,
        department=department or None,
        employment_type=employment_type or None,
        posted_at=iso_date(posted_at),
        updated_at=iso_date(updated_at),
        description=description,
        salary_min=comp["min"] if comp else None,
        salary_max=comp["max"] if comp else None,
        salary_currency=comp.get("currency") if comp else None,
        salary_period=comp.get("period") if comp else None,
        salary_source=source if comp else None,
    )


_RELATIVE = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)


def iso_date(value) -> str | None:
    """Coerce epoch seconds/ms, ISO strings, or Workday's "Posted 3 Days Ago"."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, UTC).date().isoformat()
    text = str(value).strip()
    lowered = text.lower()
    if "today" in lowered:
        return local_today().isoformat()
    if "yesterday" in lowered:
        return (local_today() - timedelta(days=1)).isoformat()
    m = _RELATIVE.search(text)
    if m:
        return (local_today() - timedelta(days=int(m.group(1)))).isoformat()
    m = re.match(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else None


def annual(min_value, max_value, currency, interval: str | None) -> dict | None:
    """Normalise a structured pay range to annual whole units."""
    if not max_value and not min_value:
        return None
    lo = float(min_value or max_value)
    hi = float(max_value or min_value)
    interval = (interval or "year").lower()
    factor = 1.0
    period = "year"
    if "hour" in interval:
        factor, period = 2080.0, "hour"
    elif "month" in interval:
        factor = 12.0
    elif "week" in interval:
        factor = 52.0
    return {
        "min": int(lo * factor),
        "max": int(hi * factor),
        "currency": (currency or "USD").upper(),
        "period": period,
    }
