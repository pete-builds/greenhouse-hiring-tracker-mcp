"""The normalised shapes every connector produces and every tool consumes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field

#: Application pipeline stages, in funnel order. The last four are terminal.
STAGES: tuple[str, ...] = (
    "shortlisted",
    "applied",
    "screen",
    "interview",
    "final",
    "offer",
    "accepted",
    "rejected",
    "withdrawn",
    "ghosted",
)
OPEN_STAGES: tuple[str, ...] = STAGES[:6]
TERMINAL_STAGES: tuple[str, ...] = STAGES[6:]

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")


def normalise_title(title: str) -> str:
    """Lower-case, strip punctuation and collapse whitespace.

    Used for repost detection and cross-source dedupe, so "Sr. AI Engineer
    (Remote)" and "sr ai engineer remote" collapse to the same string.
    """
    text = _NON_WORD.sub(" ", (title or "").lower())
    return _WS.sub(" ", text).strip()


@dataclass
class Company:
    """A company board to track: one ATS, one board slug."""

    ats: str
    slug: str
    name: str
    careers_url: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Job:
    """One posting, normalised across every ATS.

    Salaries are whole annual currency units (dollars, not cents) once they
    leave a connector, because every consumer of this shape reasons in
    dollars and the old cents convention leaked into tool output twice.
    """

    source: str
    company_slug: str
    company_name: str
    external_id: str
    title: str
    url: str
    location: str | None = None
    remote: bool | None = None
    department: str | None = None
    employment_type: str | None = None
    posted_at: str | None = None
    updated_at: str | None = None
    description: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_source: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company_slug}:{self.external_id}"

    @property
    def title_norm(self) -> str:
        return normalise_title(self.title)

    def content_hash(self) -> str:
        """Hash of the fields whose change means the posting was edited."""
        parts = [
            self.title,
            self.location or "",
            self.description or "",
            str(self.salary_min),
            str(self.salary_max),
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["key"] = self.key
        return data
