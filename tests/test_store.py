"""Job lifecycle, search, triage and the application pipeline."""

from datetime import timedelta

import pytest

from hub.models import Company, Job
from hub.store import Store, local_today


def job(ext: str, title: str = "AI Enablement Lead", **kw) -> Job:
    base = {"source": "greenhouse", "company_slug": "acme", "company_name": "Acme",
            "external_id": ext, "title": title, "url": f"https://x/{ext}", "location": "Remote - US",
            "remote": True, "description": f"{title} using Claude and MCP", "salary_min": 150_000,
            "salary_max": 190_000, "salary_currency": "USD", "salary_period": "year"}
    base.update(kw)
    return Job(**base)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "hub.db"))
    yield s
    s.close()


@pytest.fixture
def acme(store):
    return store.add_company(Company(ats="greenhouse", slug="acme", name="Acme", tags=["target"]))


def test_add_company_is_idempotent(store, acme):
    again = store.add_company(Company(ats="greenhouse", slug="acme", name="Acme Corp"))
    assert again["id"] == acme["id"] and again["name"] == "Acme Corp"
    assert len(store.list_companies()) == 1


def test_sync_adds_then_removes_then_reactivates(store, acme):
    assert store.apply_sync(acme["id"], [job("1"), job("2", "Data Engineer")])["added"] == 2
    result = store.apply_sync(acme["id"], [job("1")])
    assert result["removed"] == 1
    gone = store.get_job("greenhouse:acme:2")
    assert gone["is_active"] is False and gone["removed_at"] == local_today().isoformat()
    back = store.apply_sync(acme["id"], [job("1"), job("2", "Data Engineer")])
    assert back["reactivated"] == 1 and back["added"] == 0
    assert store.get_job("greenhouse:acme:2")["is_active"] is True


def test_failed_sync_removes_nothing(store, acme):
    store.apply_sync(acme["id"], [job("1")])
    store.record_sync_failure(acme["id"], "ConnectError: boom")
    assert store.get_job("greenhouse:acme:1")["is_active"] is True
    assert store.stats()["failing_companies"][0]["last_sync_error"] == "ConnectError: boom"


def test_repost_is_linked_to_the_closed_listing(store, acme):
    store.apply_sync(acme["id"], [job("1")])
    store.apply_sync(acme["id"], [])
    store.apply_sync(acme["id"], [job("9")])
    assert store.get_job("greenhouse:acme:9")["repost_of"] == "greenhouse:acme:1"


def test_a_skipped_description_keeps_the_stored_one(store, acme):
    store.apply_sync(acme["id"], [job("1")])
    store.apply_sync(acme["id"], [job("1", description=None, salary_min=None, salary_max=None)])
    kept = store.get_job("greenhouse:acme:1")
    assert "Claude" in kept["description"] and kept["salary_max"] == 190_000
    assert store.known_jobs(acme["id"]) == {"1": ""}


def test_full_text_search_and_filters(store, acme):
    store.apply_sync(acme["id"], [
        job("1"),
        job("2", "Solutions Architect", remote=False, location="New York, NY", salary_max=120_000),
        job("3", "Payroll Specialist", description="payroll", remote=False, location="Austin, TX"),
    ])
    assert {j["key"] for j in store.search_jobs("enablement, architect")} == {
        "greenhouse:acme:1", "greenhouse:acme:2"}
    assert [j["key"] for j in store.search_jobs("architect")] == ["greenhouse:acme:2"]
    # prefix match: the description of job 2 is "Solutions Architect using ..."
    assert [j["key"] for j in store.search_jobs("archit")] == ["greenhouse:acme:2"]
    assert [j["key"] for j in store.search_jobs(None, remote_only=True, min_salary=150_000)] == [
        "greenhouse:acme:1"]
    assert [j["key"] for j in store.search_jobs(None, location="new york")] == ["greenhouse:acme:2"]
    assert store.search_jobs(None, tag="target")
    assert store.search_jobs(None, tag="nope") == []


def test_fts_operators_in_user_text_are_searched_not_executed(store, acme):
    store.apply_sync(acme["id"], [job("1")])
    for hostile in ['"', "NEAR(", "AND OR", "*", "-claude", "a'b"]:
        store.search_jobs(hostile)  # must not raise sqlite3.OperationalError


def test_dismissed_jobs_drop_out_of_search(store, acme):
    store.apply_sync(acme["id"], [job("1")])
    store.set_triage("greenhouse:acme:1", "dismissed", "on-site")
    assert store.search_jobs(None) == []
    assert len(store.search_jobs(None, include_dismissed=True)) == 1
    store.set_triage("greenhouse:acme:1", None)
    assert len(store.search_jobs(None)) == 1


def test_posted_within_uses_local_dates(store, acme):
    old = (local_today() - timedelta(days=40)).isoformat()
    store.apply_sync(acme["id"], [job("1", posted_at=old), job("2")])
    assert [j["key"] for j in store.search_jobs(None, posted_within_days=30)] == ["greenhouse:acme:2"]


def test_application_upsert_and_stage_history(store, acme):
    store.apply_sync(acme["id"], [job("1")])
    app = store.upsert_application(company="Acme", title="AI Enablement Lead",
                                   job_key="greenhouse:acme:1", stage="applied")
    assert app["applied_on"] == local_today().isoformat()
    same = store.upsert_application(company="acme", title="ai enablement lead", stage="screen",
                                    note="recruiter call went well")
    assert same["id"] == app["id"] and same["stage"] == "screen"
    kinds = [(e["kind"], e["detail"]) for e in same["events"]]
    assert ("stage", "applied -> screen") in kinds and ("note", "recruiter call went well") in kinds
    assert store.search_jobs(None)[0]["application_stage"] == "screen"


def test_bad_stage_is_rejected(store):
    with pytest.raises(ValueError, match="stage must be one of"):
        store.upsert_application(company="A", title="B", stage="hired")


def test_remove_company_cascades_jobs_but_keeps_applications(store, acme):
    store.apply_sync(acme["id"], [job("1")])
    store.upsert_application(company="Acme", title="AI Enablement Lead", job_key="greenhouse:acme:1")
    assert store.remove_company("greenhouse", "acme")
    assert store.get_job("greenhouse:acme:1") is None
    assert store.search_jobs("enablement") == []
    assert len(store.list_applications()) == 1
