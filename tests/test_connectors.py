"""Every connector against a trimmed copy of a real response from 2026-10-20.

The fixtures are live payloads cut down to one or two postings, so these
tests pin the field names each ATS actually returns, which is where a
connector is most likely to be wrong.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from hub import connectors
from hub.http import BadPayload, Http

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str):
    raw = (FIX / name).read_text()
    return raw if name.endswith(".xml") else json.loads(raw)


@pytest.fixture
async def http():
    client = Http(retries=0, backoff=0)
    yield client
    await client.aclose()


@respx.mock
async def test_greenhouse_first_sync_reads_content_and_pay(http):
    respx.get("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true").mock(
        return_value=httpx.Response(200, json=fixture("greenhouse.json"))
    )
    jobs = await connectors.get("greenhouse").fetch(http, "anthropic", known={})
    assert len(jobs) == 2
    paid = jobs[0]
    assert paid.source == "greenhouse" and paid.company_name == "Anthropic"
    assert paid.salary_min and paid.salary_max and paid.salary_max >= paid.salary_min
    assert paid.salary_currency == "USD" and paid.salary_period == "year"
    assert paid.description and "<" not in paid.description[:50]
    assert paid.posted_at and len(paid.posted_at) == 10
    # "Location Type: On-Site" in metadata reads as a definite no, not unknown.
    assert paid.remote is False


@respx.mock
async def test_greenhouse_incremental_sync_skips_unchanged_bodies(http):
    data = fixture("greenhouse.json")
    listing = {"jobs": [{k: v for k, v in j.items() if k != "content"} for j in data["jobs"]]}
    respx.get("https://boards-api.greenhouse.io/v1/boards/anthropic/jobs").mock(
        return_value=httpx.Response(200, json=listing)
    )
    first, second = data["jobs"]
    detail = respx.get(f"https://boards-api.greenhouse.io/v1/boards/anthropic/jobs/{second['id']}").mock(
        return_value=httpx.Response(200, json=second)
    )
    known = {str(first["id"]): first["updated_at"][:10]}
    jobs = await connectors.get("greenhouse").fetch(http, "anthropic", known=known)
    by_id = {j.external_id: j for j in jobs}
    assert by_id[str(first["id"])].description is None  # unchanged: keep stored body
    assert by_id[str(second["id"])].description  # new: fetched once
    assert detail.call_count == 1


@respx.mock
async def test_lever(http):
    respx.get("https://api.lever.co/v0/postings/palantir?mode=json").mock(
        return_value=httpx.Response(200, json=fixture("lever.json"))
    )
    jobs = await connectors.get("lever").fetch(http, "palantir")
    assert jobs[0].title and jobs[0].url.startswith("https://jobs.lever.co/palantir/")
    assert jobs[0].remote is False  # workplaceType "hybrid"
    assert jobs[0].posted_at.startswith("20")


@respx.mock
async def test_lever_falls_back_to_the_eu_host(http):
    respx.get("https://api.lever.co/v0/postings/euco?mode=json").mock(return_value=httpx.Response(404))
    respx.get("https://api.eu.lever.co/v0/postings/euco?mode=json").mock(
        return_value=httpx.Response(200, json=fixture("lever.json"))
    )
    assert len(await connectors.get("lever").fetch(http, "euco")) == 2


@respx.mock
async def test_ashby_structured_compensation(http):
    respx.get("https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true").mock(
        return_value=httpx.Response(200, json=fixture("ashby.json"))
    )
    jobs = await connectors.get("ashby").fetch(http, "openai")
    assert (jobs[0].salary_min, jobs[0].salary_max) == (257000, 335000)
    assert jobs[0].salary_source == "structured"
    # Live payload: isRemote true, workplaceType "Hybrid". Hybrid is not remote,
    # and before the fix this posting ranked as a remote match.
    assert jobs[1].remote is False


@respx.mock
async def test_workable(http):
    respx.get("https://apply.workable.com/api/v1/widget/accounts/huggingface?details=true").mock(
        return_value=httpx.Response(200, json=fixture("workable.json"))
    )
    jobs = await connectors.get("workable").fetch(http, "huggingface")
    assert jobs[0].company_name == "Hugging Face"
    assert jobs[0].remote is True and "France" in jobs[0].location


@respx.mock
async def test_smartrecruiters_pages_and_details(http):
    respx.get(url__regex=r".*/companies/BoschGroup/postings\?limit=100&offset=0").mock(
        return_value=httpx.Response(200, json=fixture("smartrecruiters.json"))
    )
    detail = respx.get(url__regex=r".*/companies/BoschGroup/postings/\d+$").mock(
        return_value=httpx.Response(200, json=fixture("smartrecruiters_detail.json"))
    )
    jobs = await connectors.get("smartrecruiters").fetch(http, "BoschGroup", detail_limit=1)
    assert len(jobs) == 2
    assert detail.call_count == 1  # the limit holds
    assert jobs[0].description is not None and jobs[1].description is None
    assert jobs[0].company_name == "Bosch Group"


@respx.mock
async def test_recruitee(http):
    respx.get("https://bunq.recruitee.com/api/offers/").mock(
        return_value=httpx.Response(200, json=fixture("recruitee.json"))
    )
    jobs = await connectors.get("recruitee").fetch(http, "bunq")
    assert jobs[0].title == "Head of Product" and "Amsterdam" in jobs[0].location


@respx.mock
async def test_bamboohr(http):
    respx.get("https://acme.bamboohr.com/careers/list").mock(
        return_value=httpx.Response(200, json=fixture("bamboohr.json"))
    )
    respx.get("https://acme.bamboohr.com/careers/2/detail").mock(
        return_value=httpx.Response(200, json=fixture("bamboohr_detail.json"))
    )
    jobs = await connectors.get("bamboohr").fetch(http, "acme")
    assert jobs[0].title == "Social Media Manager"
    assert "Conshohocken" in jobs[0].location and jobs[0].description


@respx.mock
async def test_bamboohr_unknown_tenant_is_a_failure_not_an_empty_board(http):
    """An unknown tenant 302s to bamboohr.com. Following it would parse a
    marketing page; reading it as zero jobs would retire the whole board."""
    respx.get("https://nope.bamboohr.com/careers/list").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.bamboohr.com"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await connectors.get("bamboohr").fetch(http, "nope")


@respx.mock
async def test_rippling(http):
    base = "https://api.rippling.com/platform/api/ats/v1/board/rippling/jobs"
    respx.get(base).mock(return_value=httpx.Response(200, json=fixture("rippling.json")))
    respx.get(url__regex=base + r"/.+").mock(
        return_value=httpx.Response(200, json=fixture("rippling_detail.json"))
    )
    jobs = await connectors.get("rippling").fetch(http, "rippling")
    assert jobs[0].title == "Account Executive, Broker Channel (Arkansas)"  # whitespace trimmed
    assert jobs[1].remote is True  # "Remote (Dallas, Texas, US)"
    assert jobs[0].description


@respx.mock
async def test_workday_paginates_and_reads_detail(http):
    base = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
    page = fixture("workday.json")
    page["total"] = 2
    route = respx.post(f"{base}/jobs").mock(return_value=httpx.Response(200, json=page))
    respx.get(url__regex=base + r"/job/.+").mock(
        return_value=httpx.Response(200, json=fixture("workday_detail.json"))
    )
    jobs = await connectors.get("workday").fetch(http, "nvidia/wd5/NVIDIAExternalCareerSite", detail_limit=1)
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["limit"] == 20  # 21 is an HTTP 400 on Workday
    assert jobs[0].external_id == "JR2014997"
    assert jobs[0].remote is True  # "US, Remote" in additionalLocations
    assert jobs[1].description is None


def test_workday_slug_shape_is_enforced():
    with pytest.raises(ValueError, match="tenant/wdN/site"):
        connectors.get("workday").board_url("nvidia")


@respx.mock
async def test_teamtailor(http):
    respx.get("https://career.teamtailor.com/jobs.json").mock(
        return_value=httpx.Response(200, json=fixture("teamtailor.json"))
    )
    jobs = await connectors.get("teamtailor").fetch(http, "career")
    assert jobs[0].title == "UK Account Executive - SMB"


@respx.mock
async def test_breezy_parses_free_text_salary(http):
    respx.get("https://breezy.breezy.hr/json").mock(
        return_value=httpx.Response(200, json=fixture("breezy.json"))
    )
    jobs = await connectors.get("breezy").fetch(http, "breezy")
    assert jobs[0].salary_period == "hour" and jobs[0].salary_source == "posted"


@respx.mock
async def test_personio_xml(http):
    respx.get("https://personio.jobs.personio.de/xml").mock(
        return_value=httpx.Response(200, text=fixture("personio.xml"))
    )
    jobs = await connectors.get("personio").fetch(http, "personio")
    assert jobs[0].title == "Staff Software Engineer, Data Platform"
    assert jobs[0].location == "Munich; Berlin"


@respx.mock
async def test_a_200_that_is_not_json_is_named(http):
    respx.get("https://boards-api.greenhouse.io/v1/boards/x/jobs?content=true").mock(
        return_value=httpx.Response(200, text="<html>captive portal</html>")
    )
    with pytest.raises(BadPayload, match="non-JSON"):
        await connectors.get("greenhouse").fetch(http, "x", known={})


@respx.mock
async def test_retries_a_429_then_succeeds():
    client = Http(retries=1, backoff=0)
    route = respx.get("https://api.lever.co/v0/postings/palantir?mode=json").mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "0"}),
                     httpx.Response(200, json=fixture("lever.json"))]
    )
    jobs = await connectors.get("lever").fetch(client, "palantir")
    await client.aclose()
    assert route.call_count == 2 and len(jobs) == 2


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://job-boards.greenhouse.io/anthropic/jobs/123", ("greenhouse", "anthropic")),
        ("https://boards.greenhouse.io/embed/job_board?for=onestream", ("greenhouse", "onestream")),
        ("https://jobs.lever.co/palantir/abc", ("lever", "palantir")),
        ("https://jobs.ashbyhq.com/Replicant/1c65", ("ashby", "Replicant")),
        ("https://apply.workable.com/huggingface/j/81B", ("workable", "huggingface")),
        ("https://jobs.smartrecruiters.com/BoschGroup/744", ("smartrecruiters", "BoschGroup")),
        ("https://bunq.recruitee.com/o/head-of-product", ("recruitee", "bunq")),
        ("https://acme.bamboohr.com/careers/2", ("bamboohr", "acme")),
        ("https://ats.rippling.com/beazleysecurity/jobs/4ac5", ("rippling", "beazleysecurity")),
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x",
         ("workday", "nvidia/wd5/NVIDIAExternalCareerSite")),
        ("https://career.teamtailor.com/jobs/1", ("teamtailor", "career")),
        ("https://breezy.breezy.hr/p/9", ("breezy", "breezy")),
        ("https://personio.jobs.personio.de/job/1", ("personio", "personio")),
        ("https://example.com/careers", None),
    ],
)
def test_detect_board_from_url(url, expected):
    assert connectors.detect(url) == expected


def test_every_connector_is_detectable_and_has_a_board_url():
    for name, connector in connectors.CONNECTORS.items():
        assert connector.ats == name and connector.example
        assert connector.board_url(connector.example).startswith("https://")
