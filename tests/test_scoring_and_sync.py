"""Fit scoring, the collapse guard, discovery and the pipeline summary."""

from datetime import timedelta

import pytest

from hub import discovery, insights, scoring
from hub.models import Company, Job
from hub.store import Store, local_today
from hub.sync import sync_company

PROFILE = {
    "target_titles": ["AI Enablement", "AI Solutions Architect"],
    "title_keywords": ["AI", "automation"],
    "must_have_any": ["Claude", "LLM", "MCP"],
    "nice_to_have": ["governance", "Copilot"],
    "avoid_titles": ["intern", "sales development"],
    "seniority": {"exclude": ["junior"], "prefer": ["senior", "lead"]},
    "location": {"remote": True, "remote_regions": ["US", "United States"], "onsite_ok": ["Ithaca"]},
    "salary": {"floor": 140_000, "target": 160_000},
    "companies": {"prefer": ["anthropic"], "avoid": ["acme"]},
    "goal": {"target_date": (local_today() + timedelta(days=90)).isoformat(), "weekly_applications": 3},
}


def j(**kw) -> dict:
    base = {"title": "Senior AI Enablement Lead", "company_name": "Anthropic",
            "description": "Roll out Claude and MCP with governance", "location": "Remote - US",
            "remote": True, "salary_min": 150_000, "salary_max": 180_000,
            "first_seen": local_today().isoformat()}
    base.update(kw)
    return base


def test_ideal_job_scores_high_with_reasons():
    fit = scoring.score_job(j(), PROFILE)
    assert fit["score"] >= 90 and not fit["blockers"]
    assert any("AI Enablement" in r or "ai enablement" in r for r in fit["reasons"])


@pytest.mark.parametrize(
    ("override", "blocker"),
    [
        ({"title": "AI Enablement Intern"}, "intern"),
        ({"title": "Junior AI Engineer"}, "junior"),
        ({"company_name": "Acme"}, "avoid list"),
        ({"salary_min": 90_000, "salary_max": 120_000}, "under floor"),
        ({"remote": False, "location": "Austin, TX"}, "not remote"),
    ],
)
def test_blockers_zero_the_score(override, blocker):
    fit = scoring.score_job(j(**override), PROFILE)
    assert fit["score"] == 0 and any(blocker in b for b in fit["blockers"])


def test_onsite_ok_location_is_not_blocked():
    assert scoring.score_job(j(remote=False, location="Ithaca, NY"), PROFILE)["score"] > 0


def test_unposted_salary_is_a_flag_not_a_blocker():
    fit = scoring.score_job(j(salary_min=None, salary_max=None), PROFILE)
    assert fit["score"] > 0 and any("salary not posted" in f for f in fit["flags"])


def test_short_keywords_match_whole_words_only():
    """'AI' must not match 'maintain' or 'email'."""
    fit = scoring.score_job(j(title="Maintenance Coordinator", description="email"), {"title_keywords": ["ai"]})
    assert fit["parts"]["title"] == 0


@pytest.mark.parametrize(
    ("location", "remote", "expect"),
    [
        ("Remote - Sweden", True, "region-limited"),  # used to score 77 with no flag
        ("Toronto, Canada", None, "outside the US"),
        ("Hybrid - San Francisco", False, "not remote"),
        ("San Francisco, CA", None, "no remote option stated"),
    ],
)
def test_location_rules_seen_on_real_boards(location, remote, expect):
    fit = scoring.score_job(j(location=location, remote=remote), PROFILE)
    assert any(expect in x for x in fit["flags"] + fit["blockers"]), fit


def test_us_remote_by_state_counts_as_us():
    fit = scoring.score_job(j(location="Remote - California"), PROFILE)
    assert fit["parts"]["location"] == scoring.WEIGHTS["location"]


def test_title_gate_caps_off_target_roles():
    """'Senior Privacy Counsel' at an AI company scored 61 before the gate."""
    fit = scoring.score_job(j(title="Senior Privacy Counsel"), PROFILE)
    assert fit["score"] <= scoring.TITLE_GATE and any("title matches no" in f for f in fit["flags"])


def test_region_limited_remote_is_flagged():
    fit = scoring.score_job(j(location="Remote - Germany"), PROFILE)
    assert any("region-limited" in f for f in fit["flags"])


def test_old_postings_and_reposts_are_flagged():
    old = (local_today() - timedelta(days=120)).isoformat()
    fit = scoring.score_job(j(first_seen=old, repost_of="x"), PROFILE)
    assert any("open 120 days" in f for f in fit["flags"]) and any("reposted" in f for f in fit["flags"])


def test_rank_orders_and_drops_blocked():
    rows = [j(title="Payroll Specialist", description="payroll"), j(), j(title="AI Enablement Intern")]
    ranked = scoring.rank(rows, PROFILE, min_score=10)
    assert ranked[0]["title"] == "Senior AI Enablement Lead"
    assert all("Intern" not in r["title"] for r in ranked)
    assert "description" not in ranked[0]


# ------------------------------------------------------------------ sync guard


class FakeConnector:
    ats = "greenhouse"

    def __init__(self, count):
        self.count = count

    async def fetch(self, http, slug, known=None, detail_limit=0):
        return [Job(source="greenhouse", company_slug=slug, company_name=slug, external_id=str(i),
                    title=f"Role {i}", url=f"https://x/{i}") for i in range(self.count)]


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "hub.db"))
    yield s
    s.close()


async def test_a_collapsed_board_is_refused_and_force_applies_it(store, monkeypatch):
    from hub import connectors

    company = store.add_company(Company(ats="greenhouse", slug="acme", name="Acme"))
    monkeypatch.setitem(connectors.CONNECTORS, "greenhouse", FakeConnector(50))
    first = await sync_company(store, None, company)
    assert first["status"] == "ok" and first["added"] == 50
    assert store.search_jobs(None)[0]["company_name"] == "Acme"  # slug replaced by the given name

    monkeypatch.setitem(connectors.CONNECTORS, "greenhouse", FakeConnector(0))
    company = store.get_company("greenhouse", "acme")
    refused = await sync_company(store, None, company)
    assert refused["status"] == "refused"
    assert len(store.search_jobs(None, limit=100)) == 50  # nothing retired

    # Control: the same empty board with force=true does apply, so the guard
    # is what stopped it above, not a broken sync path.
    forced = await sync_company(store, None, company, force=True)
    assert forced["status"] == "ok" and forced["removed"] == 50


async def test_a_raising_connector_is_recorded_not_raised(store, monkeypatch):
    from hub import connectors

    class Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError("board moved")

    company = store.add_company(Company(ats="lever", slug="gone", name="Gone"))
    monkeypatch.setitem(connectors.CONNECTORS, "lever", Boom())
    result = await sync_company(store, None, company)
    assert result["status"] == "error" and "board moved" in result["error"]


# ------------------------------------------------------------------- discovery


def test_candidate_slugs():
    slugs = discovery.candidate_slugs("Hugging Face, Inc.")
    assert slugs[:2] == ["huggingfaceinc", "hugging-face-inc"] or "huggingface" in slugs
    assert "huggingface" in slugs and "hugging-face" in slugs


def _jobs(name, desc=""):
    return [Job(source="x", company_slug="s", company_name=name, external_id="1", title="t",
                url="u", description=desc)]


def test_identity_confirmed_mismatch_and_unverified():
    assert discovery.confirm_identity("Hugging Face", "huggingface", _jobs("Hugging Face")) == "confirmed"
    assert discovery.confirm_identity("Abnormal Security", "abnormalsecurity", _jobs("Abnormal")) == "confirmed"
    # Real collision found while building the seed: the xai board posts SpaceX jobs.
    assert discovery.confirm_identity("xAI", "xai", _jobs("SpaceXSI")) == "mismatch"
    assert discovery.confirm_identity("Zendesk", "zendesk", _jobs("zendesk", "We sell mobile tours")) == "mismatch"
    assert discovery.confirm_identity("Zendesk", "zendesk", _jobs("zendesk", "")) == "unverified"


def test_parse_directory_accepts_slug_and_url_shapes():
    text = (
        "name,slug,url,tags\n"
        "Anthropic,anthropic,https://job-boards.greenhouse.io/anthropic,ai-lab\n"
        "OpenAI,,https://jobs.ashbyhq.com/openai,\n"
        "Mystery,,https://example.com/careers,\n"
    )
    rows = discovery.parse_directory(text, ats="greenhouse")
    assert [(r["ats"], r["slug"]) for r in rows] == [("greenhouse", "anthropic"), ("ashby", "openai")]
    assert rows[0]["tags"] == ["ai-lab"]


def test_bundled_seed_parses_and_covers_several_ats():
    from hub.tools.companies import SEED_PATH

    rows = discovery.parse_directory(SEED_PATH.read_text())
    assert len(rows) >= 100
    assert {"greenhouse", "ashby", "workday", "lever"} <= {r["ats"] for r in rows}
    assert len({(r["ats"], r["slug"]) for r in rows}) == len(rows)


# -------------------------------------------------------------------- insights


def test_pipeline_summary_due_quiet_closed_and_pace(store):
    company = store.add_company(Company(ats="greenhouse", slug="acme", name="Acme"))
    store.apply_sync(company["id"], [Job(source="greenhouse", company_slug="acme", company_name="Acme",
                                         external_id="1", title="Lead", url="u", description="d")])
    a = store.upsert_application(company="Acme", title="Lead", job_key="greenhouse:acme:1",
                                 next_action="follow up", next_action_due=local_today().isoformat())
    store.upsert_application(company="Other", title="Role", stage="screen")
    store.apply_sync(company["id"], [])  # posting closes under the open application
    summary = insights.pipeline_summary(store, PROFILE)
    assert summary["counts"]["applied"] == 1 and summary["open"] == 2
    assert summary["due"][0]["id"] == a["id"]
    assert summary["posting_closed_under_open_application"][0]["id"] == a["id"]
    assert summary["goal"]["applied_this_week"] == 2 and summary["goal"]["days_left"] == 90
    assert summary["response_rate"] == 0.5


def test_pay_bands():
    rows = [{"company_name": c, "salary_min": lo, "salary_max": hi, "salary_currency": "USD", "key": c}
            for c, lo, hi in [("A", 100, 200), ("B", 200, 300), ("C", 300, 400)]]
    bands = insights.pay_bands(rows)
    assert bands["midpoint_median"] == 250 and bands["top_paying_companies"][0]["company"] == "C"
    assert insights.pay_bands([]) == {"count": 0}


def test_example_profile_uses_only_keys_scoring_reads():
    """profile.example.yaml is the documentation; a key scoring ignores is a lie in it."""
    from pathlib import Path

    import yaml

    from hub.tools.pipeline import PROFILE_KEYS

    example = yaml.safe_load((Path(__file__).parent.parent / "profile.example.yaml").read_text())
    assert set(example) <= PROFILE_KEYS
    fit = scoring.score_job(j(title="Senior Solutions Architect", description="Python and LLM work"), example)
    assert fit["score"] > 50 and not fit["blockers"]
