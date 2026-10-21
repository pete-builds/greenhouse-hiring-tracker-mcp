"""Connector registry and careers-URL detection."""

from __future__ import annotations

from hub.connectors.base import Connector
from hub.connectors.boards import (
    Ashby,
    Breezy,
    Greenhouse,
    Lever,
    Personio,
    Recruitee,
    Teamtailor,
    Workable,
)
from hub.connectors.detail_boards import BambooHR, Rippling, SmartRecruiters, Workday

CONNECTORS: dict[str, Connector] = {
    c.ats: c
    for c in (
        Greenhouse(), Lever(), Ashby(), Workable(), SmartRecruiters(), Recruitee(),
        BambooHR(), Rippling(), Workday(), Teamtailor(), Breezy(), Personio(),
    )
}

#: The order `discover_company` probes in: most common boards first, the ones
#: that need a guessed tenant shape (Workday) never, since a name alone
#: cannot produce its tenant/wdN/site slug.
PROBE_ORDER = (
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee",
    "bamboohr", "rippling", "teamtailor", "breezy", "personio",
)


def get(ats: str) -> Connector:
    try:
        return CONNECTORS[ats.lower().strip()]
    except KeyError:
        raise ValueError(f"unknown ats {ats!r}; one of: {', '.join(CONNECTORS)}") from None


def detect(url: str) -> tuple[str, str] | None:
    """Map a careers or job URL to (ats, slug), or None if unrecognised."""
    for connector in CONNECTORS.values():
        slug = connector.slug_from_url(url)
        if slug and slug.lower() not in {"www", "api", "app", "jobs", "careers", "embed"}:
            return connector.ats, slug
    return None
