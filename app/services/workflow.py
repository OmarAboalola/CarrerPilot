import asyncio
from datetime import UTC, datetime

from sqlalchemy import update

from app.db import Action, Connection, Scan
from app.errors import AppError
from app.fixtures import emails as fixture_emails
from app.fixtures import jobs as fixture_jobs
from app.integrations.gmail import account_and_threads
from app.integrations.jobs import fetch_jobs, job_key
from app.integrations.jsearch import fetch_search_jobs
from app.integrations.llm import LLMClient
from app.schemas import JobAnalysis
from app.services.consent import require_gmail_processing, require_model_processing
from app.services.inbox import analyze_threads
from app.services.matching import keyword_analysis, market_summary, match_job
from app.services.roles import recommendable, role_matches


def next_actions(jobs: list, emails: list, market: dict) -> list[dict]:
    actions = []
    for item in emails:
        if item["requires_response"]:
            actions.append(
                {
                    "kind": "reply",
                    "source_kind": "email",
                    "source_id": item["id"],
                    "priority": item["priority"],
                    "title": "Review reply: " + item["subject"],
                    "why": item["summary"],
                    "evidence": item["evidence"],
                    "uncertainty": item["uncertainty"],
                }
            )
    for item in [job for job in jobs if recommendable(job)][:3]:
        if recommendable(item):
            actions.append(
                {
                    "kind": "apply_manually",
                    "source_kind": "job",
                    "source_id": item["id"],
                    "priority": 60 if item["score"] >= 75 else 35,
                    "title": "Review opportunity: " + item["title"],
                    "why": f"Heuristic fit {item['score']}/100; verify unknown eligibility and experience.",
                    "evidence": ", ".join(item["matches"]),
                    "uncertainty": "; ".join(item["unknowns"]),
                }
            )
    for item in market.get("learning", [])[:2]:
        actions.append(
            {
                "kind": "learn",
                "source_kind": "skill",
                "source_id": item["skill"],
                "priority": 25,
                "title": "Consider learning: " + item["skill"],
                    "why": f"Not demonstrated in the confirmed CV; mentioned in {item['count']} sampled jobs. Confirm whether you already have this skill.",
                "evidence": f"Sample size {market['sample_size']}",
                "uncertainty": market["caveat"],
            }
        )
    return sorted(actions, key=lambda item: -item["priority"])[:8]


async def run_scan(app, scan_id: str):
    settings, store = app.state.settings, app.state.store
    scan = store.owned(Scan, scan_id, "local-owner")
    profile = store.latest_profile(scan.owner)
    if not profile or profile.id != scan.profile_id:
        with store.session.begin() as session:
            session.execute(update(Scan).where(Scan.id == scan_id).values(state="stale", progress="Profile changed before scan started"))
        return
    data = {
        "jobs": [],
        "emails": [],
        "errors": [],
        "error_sources": [],
        "coverage": {},
        "actions": [],
        "market": {},
        "analyzed_at": datetime.now(UTC).isoformat(),
        "processing_consent": scan.result.get("processing_consent", False),
    }

    def save(state: str, progress: str):
        with store.session.begin() as session:
            session.execute(
                update(Scan).where(Scan.id == scan_id).values(state=state, progress=progress, result=data)
            )

    def add_error(message: str, source: str):
        if message in data["errors"]:
            return
        data["errors"].append(message)
        data["error_sources"].append({"source": source, "message": message})

    async def work():
        started = asyncio.get_running_loop().time()
        save("running", "Discovering opportunities")
        raw, errors = (fixture_jobs(), []) if scan.mode == "fixture" else await fetch_jobs(settings)
        if scan.mode == "live":
            try:
                remaining = settings.scan_timeout - (asyncio.get_running_loop().time() - started) - min(10, settings.scan_timeout / 5)
                extra, search_errors = await fetch_search_jobs(settings, profile.data, store, time_budget=max(0.1, remaining))
            except Exception:
                extra, search_errors = [], ["JSearch could not complete; other job-source results retained."]
            unique = {}
            for job in raw + extra:
                unique.setdefault(job_key(job), job)
            raw = list(unique.values())
            errors.extend(search_errors)
        for error in errors:
            add_error(error, "jobs")
        data["coverage"]["jobs"] = {
            "source": "Synthetic fixtures" if scan.mode == "fixture" else settings.greenhouse_boards + (" + JSearch" if settings.jsearch_enabled else ""),
            "retrieved": len(raw),
            "limit": settings.max_jobs,
            "exhaustive": False,
        }
        raw = [job for job in raw if role_matches(job, profile.data["target_roles"])]
        data["coverage"]["jobs"]["role_matches"] = len(raw)
        data["coverage"]["jobs"]["excluded_by_role"] = data["coverage"]["jobs"]["retrieved"] - len(raw)
        data["coverage"]["jobs"]["note"] = (
            "Titles are matched against your selected roles. Employer boards and configured search queries are limited; no matching roles can occur. Analysis is capped after role filtering; eligibility still needs verification."
        )
        semaphore = asyncio.Semaphore(3)

        async def analyze(job, index):
            async with semaphore:
                try:
                    analysis = keyword_analysis(job, profile.data)
                    analysis_method = "local_evidence"
                except Exception:
                    add_error("A listing could not be analyzed; other job results retained.", "jobs")
                    return
                if scan.mode == "live" and settings.model_ready and index < 20:
                    try:
                        if getattr(app.state,"processing_revoked",False):
                            raise AppError("processing_consent_required","Model-processing approval was revoked.",403)
                        analysis = await LLMClient(settings, before_request=lambda: require_model_processing(app)).structured(
                            JobAnalysis,
                            "Extract required/preferred criteria with exact description evidence. "
                            "Only explicit geography restrictions. Do not invent experience years.",
                            job["description"],
                        )
                        if any(
                            r.evidence not in job["description"]
                            for r in analysis.required + analysis.preferred + analysis.other_required + analysis.other_preferred
                        ):
                            raise AppError("unsupported_job_evidence", "Requirements were not grounded.")
                        if analysis.experience_years is not None and (not analysis.experience_evidence or analysis.experience_evidence not in job["description"]):
                            analysis.experience_years = None
                        analysis_method = "configured_model"
                    except Exception:
                        add_error(
                            "Model analysis failed for a job; labeled local evidence fallback used.", "model"
                        )
                        analysis = keyword_analysis(job, profile.data)
                try:
                    match = match_job(job, analysis, profile.data)
                    match["analysis_method"] = analysis_method
                except Exception:
                    add_error("A listing could not be matched; other job results retained.", "jobs")
                    return
                data["jobs"].append(match)
                save("running", f"Analyzed {len(data['jobs'])} opportunities")

        await asyncio.gather(*(analyze(job, i) for i, job in enumerate(raw[: settings.max_jobs])))
        data["jobs"].sort(key=lambda item: (bool(item["blockers"]), -item["score"]))
        data["market"] = market_summary(data["jobs"], profile.data)
        save("running", "Checking job-related conversations")
        if scan.mode == "fixture":
            data["emails"] = fixture_emails()
            data["coverage"]["gmail"] = {"state": "synthetic", "count": len(data["emails"])}
        else:
            with store.session() as session:
                connection = session.get(Connection, scan.owner)
            if not connection:
                data["coverage"]["gmail"] = {"state": "disconnected"}
            elif not connection.consent and settings.model_ready:
                data["coverage"]["gmail"] = {"state": "external_processing_consent_required"}
            else:
                try:
                    generation = app.state.gmail_connection_generation

                    def check_consent():
                        if not data["processing_consent"] or getattr(app.state,"processing_revoked",False):
                            raise AppError(
                                "processing_consent_required", "Model-processing consent is required.", 403
                            )
                        require_gmail_processing(store, scan.owner, connection.account)

                    if settings.model_ready:
                        check_consent()
                    account, threads, token = await asyncio.to_thread(
                        account_and_threads,
                        app.state.tokens.decrypt(connection.encrypted_token),
                        settings.max_threads,
                    )
                    if account != connection.account:
                        raise AppError("account_changed", "Account identity changed.")
                    with store.session.begin() as session:
                        current = session.get(Connection, scan.owner)
                        if (
                            not current
                            or current.account != account
                            or current.encrypted_token != connection.encrypted_token
                            or app.state.gmail_connection_generation != generation
                        ):
                            raise AppError("account_changed", "Account identity changed.")
                        current.encrypted_token = app.state.tokens.encrypt(token)
                    data["gmail_account"] = account
                    analyzed = await analyze_threads(threads, account, settings, check_consent=check_consent)
                    with store.session() as session:
                        current = session.get(Connection, scan.owner)
                        if (
                            not current
                            or current.account != account
                            or app.state.gmail_connection_generation != generation
                        ):
                            raise AppError(
                                "account_changed", "Gmail changed during the scan. Run a fresh scan.", 409
                            )
                    data["emails"] = analyzed
                    if any(item.get("model_fallback") for item in analyzed):
                        add_error("External model unavailable for some Gmail threads; labeled local analysis retained. Check Model setup or retry after the quota resets.", "gmail")
                    if any(item.get("analysis_error") for item in analyzed):
                        add_error("Some Gmail threads were incomplete; other mail and job results retained. Inspect those threads manually.", "gmail")
                    app.state.gmail_health = {"account": account, "state": "connected"}
                    data["coverage"]["gmail"] = {
                        "state": "read",
                        "threads": len(threads),
                        "limit": settings.max_threads,
                        "window": "30 days",
                        "exhaustive": False,
                        "incomplete_threads": sum(bool(item.get("analysis_error")) for item in analyzed),
                        "model_fallback_threads": sum(bool(item.get("model_fallback")) for item in analyzed),
                    }
                except AppError as error:
                    data["emails"] = []
                    data.pop("gmail_account", None)
                    if error.code == "gmail_reconnect":
                        app.state.gmail_health = {
                            "account": connection.account,
                            "state": "reconnect_required",
                        }
                    if error.code in {"email_processing_consent_required", "processing_consent_required"}:
                        data["coverage"]["gmail"] = {"state": "external_processing_consent_required"}
                    elif error.code in {"account_changed", "email_account_changed"}:
                        data["coverage"]["gmail"] = {"state": "account_changed"}
                    else:
                        data["coverage"]["gmail"] = {"state": "unavailable"}
                    add_error(error.message, "gmail")
                except Exception:
                    data["emails"] = []
                    data.pop("gmail_account", None)
                    data["coverage"]["gmail"] = {"state": "unavailable"}
                    add_error("Gmail unavailable. Reconnect or try again; job results retained.", "gmail")
        data["actions"] = next_actions(data["jobs"], data["emails"], data["market"])
        save("partial" if data["errors"] else "succeeded", "Scan complete")

    try:
        await asyncio.wait_for(work(), timeout=settings.scan_timeout)
    except (TimeoutError, asyncio.CancelledError):
        add_error("Scan interrupted or exceeded its time budget; partial evidence retained.", "scan")
        data["market"] = market_summary(data["jobs"], profile.data)
        data["actions"] = next_actions(data["jobs"], data["emails"], data["market"])
        save("interrupted", "Interrupted")
    except Exception:
        add_error("Scan failed; available partial results retained.", "scan")
        data["market"] = market_summary(data["jobs"], profile.data)
        data["actions"] = next_actions(data["jobs"], data["emails"], data["market"])
        save("failed", "Failed")
    finally:
        with store.session.begin() as session:
            if not store.latest_profile(scan.owner) or store.latest_profile(scan.owner).id != scan.profile_id:
                session.execute(
                    update(Scan).where(Scan.id == scan_id).values(state="stale", progress="Profile changed")
                )
                session.execute(
                    update(Action)
                    .where(Action.scan_id == scan_id, Action.state.in_(["proposed", "approved"]))
                    .values(state="stale")
                )
        store.activity(scan.owner, "Scan finished", scan_id)
