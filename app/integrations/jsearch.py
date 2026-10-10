"""Opt-in public job search; profile facts and credentials never enter query URLs."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from app.db import JobSourceCache, JobSourceUsage
from app.integrations.jobs import job_key, plain_text
from app.services.geography import COUNTRIES, countries_in

ENDPOINT = "https://api.openwebninja.com/jsearch/search-v2"


def search_queries(profile):
    codes = {row["name"]: row["code"].lower() for row in COUNTRIES}
    result = []
    for location in list(dict.fromkeys(profile.get("locations", [])))[:2]:
        countries = countries_in(location)
        if len(countries) != 1:
            continue
        country = codes[next(iter(countries))]
        for role in list(dict.fromkeys(profile.get("target_roles", [])))[:3]:
            result.append({"query": role + " jobs in " + location, "country": country, "language": "en"})
    return result


def reserve_request(store, key_hash, budget):
    identifier = key_hash + ":" + datetime.now(UTC).strftime("%Y-%m")
    with store.session.begin() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = db.get(JobSourceUsage, identifier)
        if row is None:
            row = JobSourceUsage(id=identifier, requests=0)
            db.add(row)
        if row.requests >= budget:
            return False
        row.requests += 1
    return True


def active_jobs(jobs):
    for job in jobs:
        try:
            expires = (
                datetime.fromisoformat(job["expires_at"].replace("Z", "+00:00"))
                if job.get("expires_at")
                else None
            )
            if expires and expires.tzinfo and expires < datetime.now(UTC):
                continue
        except (ValueError, TypeError, AttributeError):
            pass
        yield job


def read_jobs(payload, params, retrieved):
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        raise ValueError("Invalid source response")
    data = payload.get("data")
    rows = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("Missing job list")
    jobs, errors = [], []
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(name), str) and row[name].strip()
            for name in ["job_id", "job_title", "employer_name", "job_description", "job_apply_link"]
        ):
            errors.append("JSearch skipped an incomplete listing; valid listings retained.")
            continue
        if row.get("job_location") is not None and not isinstance(row["job_location"], str):
            errors.append("JSearch skipped an invalid location; valid listings retained.")
            continue
        parsed = urlsplit(row["job_apply_link"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            errors.append("JSearch skipped a listing without a safe original application link.")
            continue
        expires = row.get("job_expiration_datetime_utc")
        if expires:
            try:
                stamp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if stamp.tzinfo and stamp < datetime.now(UTC):
                    continue
            except (ValueError, AttributeError):
                expires = None
        location = (
            row.get("job_location")
            or ", ".join(str(row[name]) for name in ["job_city", "job_state", "job_country"] if row.get(name))
            or "Unknown"
        )
        if row.get("job_is_remote") is True:
            location = "Remote · " + location
        jobs.append(
            {
                "id": "jsearch:" + hashlib.sha256(row["job_id"].encode()).hexdigest(),
                "title": row["job_title"][:250],
                "company": row["employer_name"][:250],
                "location": location,
                "description": plain_text(row["job_description"])[:25000],
                "url": row["job_apply_link"],
                "source": "JSearch / OpenWeb Ninja",
                "publisher": row.get("job_publisher"),
                "source_query": params["query"],
                "mode": "live",
                "retrieved_at": retrieved,
                "published_at": row.get("job_posted_at_datetime_utc"),
                "expires_at": expires,
                "eligibility_note": "Search country and remote flags do not establish work authorization or worldwide eligibility.",
            }
        )
    cursor = (
        payload.get("cursor")
        or payload.get("next_cursor")
        or (data.get("cursor") if isinstance(data, dict) else None)
    )
    return jobs, list(dict.fromkeys(errors)), cursor if isinstance(cursor, str) else None


async def fetch_search_jobs(settings, profile, store, time_budget=30):
    if not settings.jsearch_enabled:
        return [], []
    if not settings.jsearch_api_key.get_secret_value():
        return [], ["JSearch is enabled but its local API key is missing; existing job sources retained."]
    queries = search_queries(profile)
    if not queries:
        return [], [
            "JSearch needs a target location containing one explicit country; existing sources retained."
        ]
    key = settings.jsearch_api_key.get_secret_value()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    jobs, errors = [], []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, min(time_budget, settings.scan_timeout / 3))
    async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False) as client:
        for params in queries:
            if loop.time() >= deadline:
                errors.append("JSearch time budget reached; retrieved partial results retained.")
                break
            cache_id = hashlib.sha256((key_hash + json.dumps(params, sort_keys=True)).encode()).hexdigest()
            with store.session() as db:
                cached = db.get(JobSourceCache, cache_id)
            now = datetime.now(UTC)
            if (
                cached
                and cached.data.get("complete")
                and now - datetime.fromisoformat(cached.updated) < timedelta(hours=24)
            ):
                jobs.extend(
                    {**item, "source_cached": True} for item in active_jobs(cached.data.get("jobs", []))
                )
                errors.extend(cached.data.get("warnings", []))
                continue
            collected, warnings, failed, retry_at = [], [], False, None
            if cached and cached.retry_after and datetime.fromisoformat(cached.retry_after) > now:
                failed = True
                warnings.append("JSearch is cooling down after a source error; retry deferred.")
            else:
                page_params = dict(params)
                for _ in range(2):
                    if not reserve_request(store, key_hash, settings.jsearch_monthly_budget):
                        failed = True
                        warnings.append(
                            "JSearch local monthly request budget reached; no paid fallback is used."
                        )
                        break
                    try:
                        remaining = max(0.1, deadline - loop.time())
                        response = await asyncio.wait_for(
                            client.get(
                                ENDPOINT,
                                params=page_params,
                                headers={"x-api-key": key},
                                timeout=min(settings.request_timeout, remaining),
                            ),
                            timeout=remaining,
                        )
                        if response.status_code == 429:
                            retry_at = now + timedelta(hours=1)
                            delay = response.headers.get("retry-after", "")
                            try:
                                retry_at = max(
                                    retry_at,
                                    now + timedelta(seconds=int(delay))
                                    if delay.isdigit()
                                    else parsedate_to_datetime(delay),
                                )
                            except (ValueError, TypeError, OverflowError):
                                pass
                        response.raise_for_status()
                        if len(response.content) > 5_000_000:
                            raise ValueError("Source response too large")
                        parsed, notes, cursor = read_jobs(response.json(), params, now.isoformat())
                        collected.extend(parsed)
                        warnings.extend(notes)
                        if not cursor:
                            break
                        page_params = {**params, "cursor": cursor}
                    except (httpx.HTTPError, ValueError, TimeoutError):
                        failed = True
                        warnings.append(
                            "JSearch unavailable or invalid response; other source results retained."
                        )
                        retry_at = retry_at or now + timedelta(minutes=15)
                        break
            if failed and cached and now - datetime.fromisoformat(cached.updated) < timedelta(days=7):
                collected.extend(
                    {**item, "source_cached": True, "source_stale": True}
                    for item in active_jobs(cached.data.get("jobs", []))
                )
                warnings.append(
                    "JSearch retained an older cached sample; verify listing dates and availability."
                )
            jobs.extend(collected)
            errors.extend(warnings)
            if not failed or retry_at:
                with store.session.begin() as db:
                    db.merge(
                        JobSourceCache(
                            id=cache_id,
                            data={"jobs": collected, "warnings": warnings, "complete": not failed},
                            updated=cached.updated if failed and cached else now.isoformat(),
                            retry_after=retry_at.isoformat() if retry_at else None,
                        )
                    )
    return list({job_key(job): job for job in jobs}.values()), list(dict.fromkeys(errors))
