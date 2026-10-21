"""A slug can only ever address its own vendor.

Five connectors put the slug in the hostname ({slug}.recruitee.com) and
Workday puts its tenant there, so an unchecked slug such as
"192.168.86.20:8443/x?" turned https://{slug}.recruitee.com/api/offers/ into
a request to a LAN host. Slugs arrive from add_company and from CSV
directories fetched off the internet, and the background sync replays every
stored row on a timer, so the check runs both on the way in and before each
fetch.
"""

import json

import pytest
import respx

from hub import connectors, discovery
from hub.http import Http
from hub.models import Company
from hub.store import Store
from hub.sync import sync_company
from hub.tools.companies import SEED_PATH
from tests.test_tools import _load_server

HOSTILE = [
    ("recruitee", "192.168.86.20:8443/x?"),
    ("teamtailor", "evil.example#"),
    ("breezy", "user@10.0.0.1"),
    ("personio", "a.b"),
    ("bamboohr", "localhost:3713/mcp?"),
    ("greenhouse", "anthropic/../../x?y"),
    ("lever", "palantir#frag"),
    ("workday", "10.0.0.1:443#/wd5/site"),
    ("workday", "nvidia/wd5.evil.example/site"),
    ("workday", "nvidia/wd5/site?x=1"),
]


@pytest.mark.parametrize(("ats", "slug"), HOSTILE)
def test_hostile_slugs_are_refused(ats, slug):
    with pytest.raises(ValueError, match="slug"):
        connectors.get(ats).check_slug(slug)


def test_every_seed_slug_still_passes():
    rows = discovery.parse_directory(SEED_PATH.read_text())
    assert len(rows) == 139
    for row in rows:
        connectors.get(row["ats"]).check_slug(row["slug"])


def test_directory_import_drops_hostile_rows():
    text = "name,ats,slug\nGood,recruitee,bunq\nBad,recruitee,192.168.86.20:8443/x?\n"
    assert [r["slug"] for r in discovery.parse_directory(text)] == ["bunq"]


@respx.mock
async def test_a_stored_hostile_slug_is_never_fetched(tmp_path):
    """Rows already in the store are re-checked before the background sync fetches them."""
    store = Store(str(tmp_path / "hub.db"))
    company = store.add_company(Company(ats="recruitee", slug="192.168.86.20:8443/x?", name="Bad"))
    http = Http(retries=0, backoff=0)
    try:
        result = await sync_company(store, http, company)
    finally:
        await http.aclose()
    assert result["status"] == "error" and "not a board slug" in result["error"]
    assert respx.calls.call_count == 0


async def test_add_company_rejects_a_hostile_slug(monkeypatch, tmp_path):
    server = _load_server(monkeypatch, tmp_path, legacy=False)
    tool = await server.mcp.get_tool("add_company")
    payload = json.loads(await tool.fn(board="192.168.86.20:8443/x?", ats="recruitee"))
    assert payload["error"] == "Invalid input" and "not a board slug" in payload["detail"]
    assert server.hub.store.list_companies() == []
