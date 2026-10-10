import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.db import JobSourceCache, Store
from app.integrations import jsearch
from app.integrations.jobs import job_key
from app.services.roles import role_matches


def person():
    return {"target_roles": ["AI Engineer"], "locations": ["Egypt"], "remote": True}


def listing(title="AI Engineer", identifier="source-id", url="https://employer.example.invalid/jobs/1"):
    return {
        "job_id": identifier,
        "job_title": title,
        "employer_name": "Sample Employer",
        "job_description": "Required: LLMs, Python. Must be based in Egypt.",
        "job_apply_link": url,
        "job_country": "Egypt",
        "job_is_remote": True,
        "job_posted_at_datetime_utc": "2026-10-09T10:00:00Z",
        "job_publisher": "Employer",
    }


def fixture_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        jsearch.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs)
    )


@pytest.mark.asyncio
async def test_jsearch_original_links_dates_queries_cursor_and_persistent_cache(monkeypatch, tmp_path):
    requests = []

    def provider(request):
        requests.append(request)
        assert request.headers["x-api-key"] == "synthetic-key"
        assert request.url.params["country"] == "eg"
        assert "CV" not in str(request.url)
        rows = [listing("Finance Director", "unrelated")] * 25 + [listing()]
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "data": rows if len(requests) == 1 else [listing()],
                "cursor": "opaque-next" if len(requests) == 1 else None,
            },
        )

    fixture_client(monkeypatch, provider)
    settings = Settings(data_dir=tmp_path, mode="live", jsearch_enabled=True, jsearch_api_key="synthetic-key")
    store = Store(tmp_path)
    store.migrate()
    jobs, errors = await jsearch.fetch_search_jobs(settings, person(), store)
    assert not errors and len(requests) == 2
    assert requests[1].url.params["cursor"] == "opaque-next"
    relevant = [job for job in jobs if role_matches(job, person()["target_roles"])]
    assert len(relevant) == 1 and relevant[0]["url"] == listing()["job_apply_link"]
    assert relevant[0]["published_at"] == "2026-10-09T10:00:00Z"
    store.engine.dispose()
    reopened = Store(tmp_path)
    cached, errors = await jsearch.fetch_search_jobs(settings, person(), reopened)
    assert len(requests) == 2 and all(job["source_cached"] for job in cached)
    reopened.engine.dispose()


@pytest.mark.asyncio
async def test_jsearch_429_preserves_old_results_and_respects_retry_after(monkeypatch, tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        return (
            httpx.Response(200, json={"status": "OK", "data": [listing()]})
            if len(calls) == 1
            else httpx.Response(429, headers={"Retry-After": "7200"})
        )

    fixture_client(monkeypatch, provider)
    settings = Settings(data_dir=tmp_path, mode="live", jsearch_enabled=True, jsearch_api_key="synthetic-key")
    store = Store(tmp_path)
    store.migrate()
    await jsearch.fetch_search_jobs(settings, person(), store)
    with store.session.begin() as db:
        for cache in db.query(JobSourceCache):
            cache.updated = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    rows, errors = await jsearch.fetch_search_jobs(settings, person(), store)
    assert rows[0]["source_stale"] and errors
    _, errors = await jsearch.fetch_search_jobs(settings, person(), store)
    assert len(calls) == 2 and any("cooling down" in error for error in errors)
    store.engine.dispose()


@pytest.mark.asyncio
async def test_jsearch_budget_and_missing_key_do_not_call_provider(monkeypatch, tmp_path):
    calls = []
    fixture_client(
        monkeypatch,
        lambda request: calls.append(request) or httpx.Response(200, json={"status": "OK", "data": []}),
    )
    store = Store(tmp_path)
    store.migrate()
    settings = Settings(
        data_dir=tmp_path, jsearch_enabled=True, jsearch_api_key="synthetic-key", jsearch_monthly_budget=1
    )
    p = person()
    p["target_roles"] = ["AI Engineer", "ML Engineer"]
    _, errors = await jsearch.fetch_search_jobs(settings, p, store)
    assert len(calls) == 1 and any("budget reached" in error for error in errors)
    _, errors = await jsearch.fetch_search_jobs(Settings(data_dir=tmp_path, jsearch_enabled=True), p, store)
    assert len(calls) == 1 and any("key is missing" in error for error in errors)
    store.engine.dispose()


def test_distinct_roles_are_not_merged_and_provider_ids_do_not_collide():
    rows, errors, _ = jsearch.read_jobs(
        {
            "status": "OK",
            "data": [listing(identifier="x" * 200 + "a"), listing("ML Engineer", "x" * 200 + "b"), None],
        },
        {"query": "Engineer jobs"},
        datetime.now(UTC).isoformat(),
    )
    assert len(rows) == 2 and rows[0]["id"] != rows[1]["id"]
    assert job_key(rows[0]) != job_key(rows[1]) and errors
    assert job_key(rows[0]) == job_key({**rows[0], "id": "another", "url": rows[0]["url"] + "#detail"})


def test_no_country_is_assumed_for_ambiguous_locations_and_expired_jobs_are_excluded():
    assert jsearch.search_queries({"locations": ["Springfield"], "target_roles": ["Nurse"]}) == []
    expired = {**listing(), "job_expiration_datetime_utc": "2020-01-01T00:00:00Z"}
    rows, _, _ = jsearch.read_jobs(
        {"status": "OK", "data": [expired]}, {"query": "jobs"}, datetime.now(UTC).isoformat()
    )
    assert rows == []


def test_cached_expiry_and_malformed_location_are_safe():
    expired = {**listing(), "expires_at": "2020-01-01T00:00:00Z"}
    assert list(jsearch.active_jobs([expired])) == []
    malformed = {**listing(), "job_location": {"invalid": "location"}}
    rows, errors, _ = jsearch.read_jobs(
        {"status": "OK", "data": [malformed, listing()]}, {"query": "jobs"}, datetime.now(UTC).isoformat()
    )
    assert len(rows) == 1 and errors


@pytest.mark.asyncio
async def test_source_time_budget_keeps_already_retrieved_results(monkeypatch, tmp_path):
    calls = []

    async def provider(request):
        calls.append(request)
        if len(calls) > 1:
            await asyncio.sleep(1)
        return httpx.Response(200, json={"status": "OK", "data": [listing()]})

    fixture_client(monkeypatch, provider)
    settings = Settings(data_dir=tmp_path, jsearch_enabled=True, jsearch_api_key="synthetic-key")
    store = Store(tmp_path)
    store.migrate()
    p = person()
    p["target_roles"] = ["AI Engineer", "ML Engineer", "LLM Engineer"]
    rows, errors = await jsearch.fetch_search_jobs(settings, p, store, time_budget=0.05)
    assert len(rows) == 1 and any("time budget" in error for error in errors)
    assert len(calls) == 2
    store.engine.dispose()
