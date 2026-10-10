"""Local application API; source content never controls authorization."""

import asyncio
import json
import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update

from app.db import Action, Activity, Connection, OpportunityStatus, Profile, Scan
from app.errors import AppError, DraftOutcomeUnknown
from app.fixtures import example_profile
from app.integrations.gmail import (
    COMPOSE_SCOPE,
    READ_SCOPE,
    conversation_url,
    create_verified_draft,
    gmail_service,
    oauth_flow,
    reconcile_draft,
)
from app.integrations.llm import LLMClient
from app.schemas import (
    ApprovalInput,
    CareerProfile,
    ConsentInput,
    DraftInput,
    EditDraft,
    Question,
    ScanInput,
    StatusInput,
    TextInput,
)
from app.security import OWNER, approve, claim
from app.services.consent import require_gmail_processing
from app.services.gmail_state import (
    action_account_available,
    invalidate_gmail_scans,
    require_gmail_account,
    visible_scan_data,
)
from app.services.matching import current_match, market_summary, profile_fingerprint
from app.services.model_settings import (
    apply_values,
    configure_model,
    model_status,
    persist_model,
    require_idle,
    reset_processing_approvals,
)
from app.services.outreach import edit, generate
from app.services.profile_scope import tracking_prefix
from app.services.profiles import extract_profile, extraction_notice, read_pdf
from app.services.question_context import compact_context
from app.services.roles import recommendable, role_matches
from app.services.skills import SKILL_RULES
from app.services.workflow import next_actions, run_scan

router = APIRouter(prefix="/api")


def store(request):
    return request.app.state.store


def settings(request):
    return request.app.state.settings


def action_view(action, request=None):
    available = request is None or (
        (settings(request).mode == "live" or action.payload.get("mode") != "live")
        and action_account_available(store(request), OWNER, action)
    )
    value = {
        "id": action.id,
        "version": action.version,
        "payload": action.payload,
        "payload_hash": action.payload_hash,
        "state": action.state,
        "expires": action.expires,
        "result": action.result,
        "scan_id": action.scan_id,
        "created": action.created,
        "account_available": available,
    }
    if not available:
        value["payload"] = {"mode": action.payload.get("mode"), "account": "", "subject": ""}
        value["result"] = {"message": "Connect the original Gmail account to review or reconcile this draft."}
    return value


def require_action_account(request, action):
    if settings(request).mode != "live" and action.payload.get("mode") == "live":
        raise AppError("fixture_isolation", "Real Gmail draft content is hidden in the synthetic demo.", 403)
    if not action_account_available(store(request), OWNER, action):
        raise AppError("email_account_changed", "Connect the original Gmail account to open this draft.", 409)


def scan_view(request, value):
    profile = store(request).latest_profile(OWNER)
    data = visible_scan_data(store(request), OWNER, value)
    if not profile or profile.id != value.profile_id:
        # Preserve the historical record, but never present another CV's suggestions as current.
        data = {**data,"jobs":[],"emails":[],"market":{},"actions":[],"profile_stale":True}
    # Upgrade old cached local analyses in the view, without changing the saved profile or mail.
    if profile and profile.id == value.profile_id:
        old_jobs = data.get("jobs", [])
        upgraded = any(job.get("skill_rules") != SKILL_RULES or job.get("match_profile") != profile_fingerprint(profile.data) for job in old_jobs)
        relevant = [job for job in old_jobs if role_matches(job, profile.data.get("target_roles", []))]
        data["jobs"] = sorted([current_match(job, profile.data) for job in relevant],key=lambda j:(bool(j["blockers"]),-j["score"]))
        if upgraded or len(relevant) != len(old_jobs):
            data["market"] = market_summary(data["jobs"], profile.data)
            coverage = data.get("coverage", {}).get("jobs", {})
            data["coverage"] = {**data.get("coverage", {}), "jobs": {
                **coverage, "role_matches": len(relevant),
                "excluded_by_role": max(0, coverage.get("retrieved", len(old_jobs)) - len(relevant)),
                "note": "Cached job results were rechecked against your roles with the current local matching rules. Run a fresh scan for current source coverage.",
            }}
    if settings(request).mode != "live" and value.mode == "live":
        data["emails"] = []
        data["gmail_account"] = None
        data["actions"] = [item for item in data.get("actions", []) if item.get("source_kind") != "email"]
        data["coverage"] = {**data.get("coverage", {}), "gmail": {"state": "disconnected"}}
    with store(request).session() as db:
        statuses = {
            row.id.removeprefix(tracking_prefix(OWNER,profile)): {"state": row.state, "updated": row.created, "previous_state": row.previous_state}
            for row in db.scalars(select(OpportunityStatus).where(OpportunityStatus.owner == OWNER))
            if row.id.startswith(tracking_prefix(OWNER,profile)) and (":cv:" in row.id)==(":cv:" in tracking_prefix(OWNER,profile))
        }
    for job in data.get("jobs", []):
        job["application_status"] = statuses.get(job["id"])
    pending_jobs = [
        job
        for job in data.get("jobs", [])
        if statuses.get(job["id"], {}).get("state") not in {"applied_by_user", "dismissed"}
    ]
    data["actions"] = next_actions(pending_jobs, data.get("emails", []), data.get("market", {}))
    return {
        "id": value.id,
        "state": value.state,
        "progress": value.progress,
        "mode": value.mode,
        "profile_current": bool(profile and profile.id == value.profile_id),
        "result": data,
        "created": value.created,
    }


@router.get("/session")
def session(request: Request):
    request.session.setdefault("csrf", secrets.token_urlsafe(32))
    value = settings(request)
    with store(request).session() as db:
        connection = db.get(Connection, OWNER) if value.mode == "live" else None
    if connection:
        request.session.pop("gmail_disconnect", None)
    return {
        "csrf": request.session["csrf"],
        "mode": value.mode,
        "model_configured": value.model_ready,
        "model_provider": model_status(request.app)["provider"],
        "model_name": value.llm_model if value.model_ready else "",
        "model_version": value._model_version,
        "gmail_configured": value.gmail_ready,
        "account": connection.account if connection else None,
        "compose": bool(connection and COMPOSE_SCOPE in connection.scopes),
        "email_processing_consent": bool(connection and connection.consent),
        "job_coverage": value.greenhouse_boards,
        "jsearch_state": "configured" if value.jsearch_enabled and value.jsearch_api_key.get_secret_value() else "key_missing" if value.jsearch_enabled else "disabled",
        "public_service": False,
        "gmail_disconnect": request.session.get("gmail_disconnect"),
        "gmail_busy": request.app.state.gmail_revoking,
        "gmail_version": request.app.state.gmail_connection_generation,
        "gmail_state": (
            request.app.state.gmail_health.get("state", "connected")
            if connection
            and request.app.state.gmail_health.get("account", connection.account) == connection.account
            else "connected"
            if connection
            else "disconnected"
        ),
    }


@router.get("/model")
def get_model(request: Request):
    return model_status(request.app)


@router.post("/model/setup")
async def setup_model(request: Request):
    if settings(request).mode != "live":
        raise AppError("fixture_model_disabled", "Switch to live mode to configure an external model.", 403)
    # Manual validation keeps passwords out of FastAPI's echoed validation inputs.
    if len(await request.body()) > 4096:
        raise AppError("invalid_model_settings", "Model settings exceeded the request limit.")
    try:
        raw = await request.json()
    except ValueError:
        raise AppError("invalid_model_settings", "Enter a Groq key and a supported model.") from None
    async with request.app.state.model_setup_lock:
        await configure_model(request.app, OWNER, raw)
    return {**model_status(request.app), "message": "Groq connection verified. Key saved encrypted locally. Review processing approvals and run a fresh scan."}


@router.post("/model/disable")
async def disable_model(request: Request):
    if settings(request).mode != "live":
        raise AppError("fixture_model_disabled", "The synthetic demo has no external model.", 403)
    async with request.app.state.model_setup_lock:
        require_idle(request.app)
        reset_processing_approvals(request.app, OWNER)
        persist_model(settings(request), request.app.state.tokens, {"enabled": False})
        apply_values(settings(request), {"enabled": False})
        request.app.state.model_setup_error = ""
    return {**model_status(request.app), "message": "External model disabled. Local extraction, scans and drafts remain available."}


@router.get("/profile")
def current_profile(request: Request):
    profile = store(request).latest_profile(OWNER)
    return (
        {"id": profile.id, "profile": profile.data, "confirmed": True}
        if profile
        else {"profile": None, "confirmed": False}
    )


@router.get("/profile/example")
def example():
    return {
        "profile": example_profile(),
        "confirmed": False,
        "notice": "CV-derived example facts; suggested goals require review.",
    }


@router.post("/profile/text")
async def text_profile(request: Request, value: TextInput):
    result = await extract_profile(value.text, settings(request), value.processing_consent)
    return {
        "profile": result.model_dump(),
        "confirmed": False,
        "notice": extraction_notice(result, settings(request)),
    }


@router.post("/profile/upload")
async def upload_profile(
    request: Request, file: UploadFile = File(...), processing_consent: bool = Form(False)
):
    try:
        raw = await file.read(5_000_001)
    finally:
        await file.close()
    text = await asyncio.wait_for(asyncio.to_thread(read_pdf, raw), timeout=10)
    result = await extract_profile(text, settings(request), processing_consent)
    return {
        "profile": result.model_dump(),
        "confirmed": False,
        "notice": extraction_notice(result, settings(request)),
    }


@router.post("/profile/confirm")
def confirm(request: Request, value: CareerProfile):
    if not value.skills or not value.target_roles or not value.locations:
        raise AppError("profile_incomplete", "Confirm at least one skill, target role and target location.")
    with store(request).session.begin() as db:
        # Lock the read/compare/write together, including across local workers.
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        latest = db.scalar(
            select(Profile)
            .where(Profile.owner == OWNER, Profile.confirmed.is_(True))
            .order_by(Profile.created.desc())
            .limit(1)
        )
        expected = request.headers.get("X-Profile-Version") or None
        if expected != (latest.id if latest else None):
            raise AppError(
                "profile_conflict",
                "The saved profile changed. Your edits are retained; load the latest saved profile before confirming.",
                409,
            )
        if latest and CareerProfile.model_validate(latest.data).model_dump() == value.model_dump():
            return {"id": latest.id, "profile": latest.data, "confirmed": True}
        profile = Profile(owner=OWNER, data=value.model_dump(), confirmed=True)
        db.add(profile)
        db.execute(
            update(Action)
            .where(Action.owner == OWNER, Action.state.in_(["proposed", "approved"]))
            .values(state="stale")
        )
        db.flush()
        identifier = profile.id
    store(request).activity(OWNER, "New profile confirmed", identifier)
    return {"id": identifier, "profile": value.model_dump(), "confirmed": True}


@router.post("/scans")
async def start_scan(request: Request, value: ScanInput):
    app = request.app
    profile = store(request).latest_profile(OWNER)
    if not profile:
        raise AppError("profile_required", "Review and confirm a career profile first.")
    if settings(request).mode == "live" and settings(request).model_ready and not value.processing_consent:
        raise AppError(
            "processing_consent_required", "Confirm model-processing consent before a live scan.", 403
        )
    if any(not task.done() for task in app.state.tasks.values()):
        raise AppError("scan_running", "One local scan is already running.", 409)
    if settings(request).mode == "live" and settings(request).model_ready and getattr(app.state, "processing_revoked", False):
        raise AppError("processing_consent_required", "Model approval was revoked. Select current approval before starting a new scan.", 403)
    with store(request).session.begin() as db:
        scan = Scan(
            owner=OWNER,
            profile_id=profile.id,
            mode=settings(request).mode,
            result={"processing_consent": value.processing_consent},
        )
        db.add(scan)
        db.flush()
        identifier = scan.id
    task = asyncio.create_task(run_scan(app, identifier))
    app.state.tasks[identifier] = task
    store(request).activity(OWNER, "Scan started", identifier)
    return {"id": identifier, "state": "queued"}


@router.get("/scans/latest")
def latest_scan(request: Request):
    with store(request).session() as db:
        value = db.scalar(select(Scan).where(Scan.owner == OWNER).order_by(Scan.created.desc()).limit(1))
    return {"scan": scan_view(request, value) if value else None}


@router.get("/scans/{scan_id}")
def scan(request: Request, scan_id: str):
    return scan_view(request, store(request).owned(Scan, scan_id, OWNER))


@router.post("/scans/{scan_id}/cancel")
def cancel(request: Request, scan_id: str):
    store(request).owned(Scan, scan_id, OWNER)
    task = request.app.state.tasks.get(scan_id)
    if task and not task.done():
        task.cancel()
    return {"state": "cancellation_requested"}


@router.get("/scans/{scan_id}/emails/{email_id}/open")
def open_gmail_conversation(request: Request, scan_id: str, email_id: str):
    record = store(request).owned(Scan, scan_id, OWNER)
    if settings(request).mode != "live" or record.mode != "live":
        raise AppError("synthetic_mail", "Synthetic conversations cannot be opened in Gmail.", 403)
    data = record.result
    require_gmail_account(store(request), OWNER, data.get("gmail_account"))
    if data.get("gmail_invalidated"):
        raise AppError("email_account_changed", "Run a fresh Gmail scan before opening this conversation.", 409)
    email = next((item for item in data.get("emails", []) if item["id"] == email_id and item.get("mode") == "live"), None)
    if email is None:
        raise AppError("not_found", "This conversation is unavailable. Run a fresh scan.", 404)
    url, _ = conversation_url(data["gmail_account"], email.get("reply_message_id", ""))
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/opportunities/status")
def opportunity_status(request: Request, value: StatusInput):
    key = tracking_prefix(OWNER,store(request).latest_profile(OWNER)) + value.job_id
    with store(request).session.begin() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = db.get(OpportunityStatus, key)
        current = row.state if row else "untracked"
        if value.expected_state is not None and current != value.expected_state:
            raise AppError("status_conflict", "Application tracking changed. Refresh its saved status before retrying.", 409)
        target = (row.previous_state or "untracked") if value.state == "undo_applied" and current == "applied_by_user" else current if value.state == "undo_applied" else value.state
        if target != current:
            previous = current if target == "applied_by_user" else None
            if row is None:
                row = OpportunityStatus(id=key, owner=OWNER, state=target)
                db.add(row)
            row.previous_state, row.state, row.created = previous, target, datetime.now(UTC).isoformat()
            event = "Applied mark undone; tracking restored to " + target if value.state == "undo_applied" else "Application marked " + target.replace("_by_user", "")
            db.add(Activity(owner=OWNER, event=event, reference=value.job_id))
        return {"state": target, "updated": row.created if row else None, "previous_state": row.previous_state if row else None}


@router.get("/opportunities/status/{job_id}")
def saved_opportunity_status(request: Request, job_id: str):
    with store(request).session() as db:
        row = db.get(OpportunityStatus, tracking_prefix(OWNER,store(request).latest_profile(OWNER)) + job_id)
    return {"state": row.state if row else "untracked", "updated": row.created if row else None, "previous_state": row.previous_state if row else None}


@router.post("/drafts")
async def drafts(request: Request, value: DraftInput):
    return action_view(await generate(request.app, OWNER, value), request)


@router.get("/actions")
def action_history(request: Request):
    with store(request).session() as db:
        unresolved = list(
            db.scalars(
                select(Action)
                .where(Action.owner == OWNER, Action.state.in_(["unknown", "executing"]))
                .order_by(Action.created.desc())
            )
        )
        other = list(
            db.scalars(
                select(Action)
                .where(Action.owner == OWNER, Action.state.not_in(["unknown", "executing"]))
                .order_by(Action.created.desc())
                .limit(100)
            )
        )
    return [action_view(action, request) for action in unresolved + other]


@router.get("/actions/{action_id}")
def get_action(request: Request, action_id: str):
    action = store(request).owned(Action, action_id, OWNER)
    require_action_account(request, action)
    return action_view(action, request)


@router.put("/actions/{action_id}")
def edit_action(request: Request, action_id: str, value: EditDraft):
    require_action_account(request, store(request).owned(Action, action_id, OWNER))
    return action_view(edit(store(request), OWNER, action_id, value), request)


@router.post("/actions/{action_id}/review")
def review(request: Request, action_id: str, value: ApprovalInput):
    require_action_account(request, store(request).owned(Action, action_id, OWNER))
    approve(store(request), action_id, OWNER, value.payload_hash, value.version, value.decision)
    return action_view(store(request).owned(Action, action_id, OWNER), request)


@router.post("/actions/{action_id}/execute")
async def execute(request: Request, action_id: str):
    app = request.app
    action = store(request).owned(Action, action_id, OWNER)
    require_action_account(request, action)
    scan_record = store(request).owned(Scan, action.scan_id, OWNER)
    synthetic = action.payload["mode"] == "synthetic" and scan_record.mode == "fixture"
    if settings(request).mode == "fixture" and not synthetic:
        raise AppError("fixture_isolation", "Fixture mode cannot execute a real Gmail action.", 403)
    if action.payload["type"] != "create_gmail_draft":
        raise AppError("action_disabled", "Only reviewed Gmail draft creation is supported.", 403)
    health = app.state.gmail_health
    if (
        not synthetic
        and action.state == "approved"
        and health.get("account") == action.payload.get("account")
        and health.get("state") == "reconnect_required"
    ):
        raise AppError("gmail_reconnect", "Reconnect Gmail, then prepare and review a fresh draft.", 401)
    action, claimed = claim(store(request), action_id, OWNER, fixture=synthetic)
    if not claimed:
        return action_view(action, request)
    if synthetic:
        result, state = (
            {
                "simulation": True,
                "verified": False,
                "message": "Synthetic draft simulation only; Gmail was not accessed.",
            },
            "succeeded",
        )
    else:
        with store(request).session() as db:
            connection = db.get(Connection, OWNER)
        try:
            result, token = await asyncio.to_thread(
                create_verified_draft,
                app.state.tokens.decrypt(connection.encrypted_token),
                {**action.payload, "action_id": action.id},
            )
            with store(request).session.begin() as db:
                current = db.get(Connection, OWNER)
                if (
                    current
                    and current.account == connection.account
                    and current.encrypted_token == connection.encrypted_token
                ):
                    current.encrypted_token = app.state.tokens.encrypt(token)
            state = "succeeded"
        except DraftOutcomeUnknown as error:
            result, state = (
                {
                    "draft_id": error.draft_id,
                    "verified": False,
                    "message": "Gmail outcome uncertain. Reconcile before further action.",
                },
                "unknown",
            )
        except AppError as error:
            result, state = {"error": error.code, "message": error.message}, "failed"
            if error.code == "gmail_reconnect":
                with store(request).session() as db:
                    current = db.get(Connection, OWNER)
                    if (
                        current
                        and current.account == connection.account
                        and current.encrypted_token == connection.encrypted_token
                    ):
                        app.state.gmail_health = {
                            "account": connection.account,
                            "state": "reconnect_required",
                        }
        except Exception:
            result, state = (
                {
                    "message": "Provider outcome uncertain. Do not retry; inspect Gmail drafts and reconcile.",
                    "verified": False,
                },
                "unknown",
            )
    with store(request).session.begin() as db:
        db.execute(
            update(Action)
            .where(Action.id == action_id, Action.state == "executing")
            .values(state=state, result=result)
        )
    event = "Draft execution " + state
    if state == "failed" and result.get("message"):
        event += ": " + result["message"]
    store(request).activity(OWNER, event, action_id)
    return action_view(store(request).owned(Action, action_id, OWNER), request)


@router.post("/actions/{action_id}/reconcile")
async def reconcile(request: Request, action_id: str):
    action = store(request).owned(Action, action_id, OWNER)
    require_action_account(request, action)
    if action.state != "unknown" or action.payload["mode"] != "live":
        raise AppError(
            "reconciliation_unavailable", "Only uncertain live Gmail writes can be reconciled.", 409
        )
    if settings(request).mode != "live":
        raise AppError("fixture_isolation", "Fixture mode cannot access Gmail.", 403)
    with store(request).session() as db:
        connection = db.get(Connection, OWNER)
    if not connection:
        raise AppError("gmail_disconnected", "Reconnect the action's Gmail account.", 409)
    try:
        result, token = await asyncio.to_thread(
            reconcile_draft,
            request.app.state.tokens.decrypt(connection.encrypted_token),
            {**action.payload, "action_id": action.id},
            action.result.get("draft_id"),
        )
    except AppError:
        raise
    except Exception:
        raise AppError(
            "reconciliation_failed", "Could not read Gmail drafts. Outcome remains uncertain.", 502
        ) from None
    with store(request).session.begin() as db:
        db.execute(
            update(Action)
            .where(Action.id == action_id, Action.state == "unknown")
            .values(state="succeeded", result=result)
        )
        current = db.get(Connection, OWNER)
        if (
            current
            and current.account == connection.account
            and current.encrypted_token == connection.encrypted_token
        ):
            current.encrypted_token = request.app.state.tokens.encrypt(token)
    store(request).activity(OWNER, "Draft reconciled", action_id)
    return action_view(store(request).owned(Action, action_id, OWNER), request)


@router.get("/activity")
def activity(request: Request):
    with store(request).session() as db:
        rows = db.scalars(
            select(Activity).where(Activity.owner == OWNER).order_by(Activity.created.desc()).limit(50)
        ).all()
    return [{"event": row.event, "reference": row.reference, "created": row.created} for row in rows]


class Answer(BaseModel):
    answer: str
    source_ids: list[str]


@router.post("/questions")
async def questions(request: Request, value: Question):
    record = store(request).owned(Scan, value.scan_id, OWNER)
    profile = store(request).latest_profile(OWNER)
    if not profile or profile.id != record.profile_id:
        raise AppError("profile_changed", "Question results require a scan of the current profile.", 409)
    data = record.result
    text = value.question.casefold()
    followup=bool(re.search(r"\b(?:they|them|this|that|it|those|same)\b",text))
    if value.previous_question and followup:
        text += " " + value.previous_question.casefold()
    gmail_question = any(word in text for word in ["email", "mail", "recruiter", "interview", "unanswered", "assessment", "deadline", "reply", "respond", "response", "conversation", "waiting", "first", "priority", "important", "provide", "answered", "they", "them"])
    if record.mode == "live" and gmail_question and settings(request).mode != "live":
        raise AppError(
            "fixture_isolation", "Real Gmail questions are unavailable in the synthetic demo.", 403
        )
    if (
        record.mode == "live"
        and gmail_question
        and (data.get("gmail_account") or data.get("emails") or data.get("gmail_invalidated"))
    ):
        require_gmail_account(store(request), OWNER, data.get("gmail_account"))
        if data.get("gmail_invalidated"):
            raise AppError("scan_stale", "Run a fresh Gmail scan before asking about conversations.", 409)
    if not gmail_question:
        data = {**scan_view(request, record)["result"], "emails": [], "gmail_account": None}
        data["actions"] = [a for a in data.get("actions", []) if a.get("source_kind") != "email"]
    else:
        data = scan_view(request, record)["result"]
    all_jobs = data.get("jobs", [])
    data = {**data, "jobs": [job for job in all_jobs if recommendable(job)]}
    if record.mode == "live" and settings(request).model_ready:
        if not data.get("processing_consent") or not value.processing_consent or getattr(request.app.state,"processing_revoked",False):
            raise AppError("processing_consent_required", "Approve current model processing before asking this question.", 403)
        if (
            data.get("gmail_account")
            or data.get("emails")
            or any(item.get("source_kind") == "email" for item in data.get("actions", []))
        ):
            require_gmail_processing(store(request), OWNER, data.get("gmail_account"))
        valid_previous={item["id"] for item in data.get("jobs",[])+data.get("emails",[])}
        previous_ids=value.previous_source_ids if followup else []
        if any(identifier not in valid_previous for identifier in previous_ids):
            raise AppError("question_context_changed","Prior citations are incompatible with this scan. Ask a fresh question.",409)
        context=compact_context(data|{"profile_preferences":{"willing_to_learn":profile.data.get("willing_to_learn",True),"target_roles":profile.data.get("target_roles",[]),"locations":profile.data.get("locations",[]),"seniority":profile.data.get("seniority")}},value.question,value.previous_question if followup else "",previous_ids)
        def check_permission():
            if getattr(request.app.state, "processing_revoked", False):
                raise AppError("processing_consent_required", "Model-processing approval was revoked.", 403)
            if data.get("gmail_account"):
                require_gmail_processing(store(request), OWNER, data.get("gmail_account"))

        response = await LLMClient(settings(request), before_request=check_permission).structured(
            Answer,
            "Answer only from scan evidence. No action execution. Cite source IDs from supplied items; "
            "if unsupported, explicitly abstain. State coverage is limited. "
            "Job items are filtered for target relevance, blockers and completed status. "
            "Unknown eligibility requires verification; never claim it is confirmed. "
            "Answer the requested action/status, not just subjects. Prior questions are conversation context, not evidence. "
            "Use now and received_at for this-week/upcoming questions. Counts describe retrieved_counts only. "
            "Use this_week_utc totals/categories for weekly counts. Distinguish important transactional messages from all job-related mail and bulk notifications. Never claim a partial selection is the entire week. "
            "Respect profile_preferences. If willing_to_learn is false, do not propose learning priorities unless the user explicitly changes that preference. "
            "Context may omit sources: disclose context_coverage and never conclude mailbox-wide absence. "
            "If a reference has multiple plausible sources, ask for clarification. Never invent deadlines or replies.",
            json.dumps(context, ensure_ascii=False),
        )
        check_permission()
        latest = store(request).latest_profile(OWNER)
        if not latest or latest.id != record.profile_id:
            raise AppError("profile_changed", "Profile changed during this answer. Ask a fresh question.", 409)
        valid = {item["id"] for item in context.get("jobs", []) + context.get("emails", [])}
        if any(identifier not in valid for identifier in response.source_ids):
            raise AppError("unsupported_answer", "Model referenced evidence outside this scan.", 502)
        coverage = data.get("coverage",{}).get("gmail",{})
        note = (f"This answer uses a limited saved scan: {len(data.get('jobs',[]))} job records and {len(data.get('emails',[]))} analyzed conversations. "
                + (f"Gmail retrieval is capped at {coverage.get('limit','the configured limit')} threads from a {coverage.get('window','limited')} query. " if gmail_question else "")
                + f"The model received details for {len(context['jobs'])} jobs and {len(context['emails'])} conversations; additional records were omitted to fit the request budget. This is not a mailbox-wide or market-wide conclusion."
                + (" Weekly counts use Monday 00:00 UTC." if re.search(r"\bweek\b",value.question,re.I) else ""))
        return {**response.model_dump(),"coverage_note":note}
    if gmail_question:
        rows = [item for item in data.get("emails", []) if item["requires_response"]]
        deadline_question = any(word in text for word in ["deadline", "approaching", "due", "before", "when"])
        if deadline_question:
            rows = [item for item in data.get("emails", []) if item.get("deadline") or item["requires_response"] and item.get("category") in {"interview", "assessment"}]
            if any(word in text for word in ["approaching", "upcoming", "soon"]):
                upcoming = []
                for item in rows:
                    try:
                        due = datetime.fromisoformat(item.get("deadline") or "")
                        if due.tzinfo and datetime.now(UTC) <= due <= datetime.now(UTC) + timedelta(days=7):
                            upcoming.append(item)
                    except ValueError:
                        upcoming.append(item)
                rows = upcoming
        details = []
        for item in rows:
            evidence = item.get("evidence") or item.get("summary") or "Requested action is unclear; inspect the original thread."
            detail = f"{item['subject']}: {evidence}"
            if deadline_question:
                detail += " Deadline: " + (item.get("deadline") or "unknown or ambiguous; verify the original date and timezone") + "."
            if item.get("response_state") == "pending_response":
                detail += " Acknowledged, but the requested response is still outstanding."
            details.append(detail)
        answer = "Within this limited scan sample: " + ("; ".join(details) if details else "no supported deadline items were identified." if deadline_question else "no response-required items were identified.")
        answer += " Verify each original thread; recent search coverage is limited and is not the whole mailbox."
        if record.mode == "live" and data.get("coverage", {}).get("gmail", {}).get("state") != "read":
            answer = "Gmail was not successfully read; outstanding replies are unknown."
    elif any(word in text for word in ["skill", "learn", "gap"]):
        rows = []
        market = data.get("market", {})
        learning = market.get("learning", [])
        if not profile.data.get("willing_to_learn", True):
            answer = "Learning suggestions are turned off in your profile. Choose Review profile, enable Suggest learning priorities, and run a fresh scan."
        elif learning:
            answer = "Skills not demonstrated in the confirmed CV: " + "; ".join(f"{item['skill']} ({item['count']} of {market['sample_size']} relevant, deduplicated sampled jobs)" for item in learning) + ". This limited sample does not establish that you lack these skills; review requirements and skill depth."
            rows = [job for job in data.get("jobs", []) if job["id"] in market.get("sample_ids", [])]
        elif not market.get("sample_size"):
            answer = "There are no usable jobs for learning analysis in this retrieved sample. Check source coverage, review your profile, or run a fresh scan. This does not mean there are no skills to learn."
        else:
            answer = "No supported missing-skill priorities were found in this limited sample. Review the job evidence and skill depth, or run a fresh scan for more coverage."
    elif any(word in text for word in ["job", "opportunit", "apply"]):
        rows = data.get("jobs", [])[:3]
        answer = (
            ("Top items in this limited scan for " + ", ".join(profile.data["target_roles"]) + ": " + "; ".join(item["title"] + " — matched CV skills: " + (", ".join(item.get("matches", [])) or "none established") + ". " + item.get("why", "") + " Unknowns to verify: " + ("; ".join(item.get("unknowns", [])) or "confirm eligibility and current application details") for item in rows))
            if rows
            else "All opportunities in this scan were marked applied or dismissed. Review their status or run a fresh scan for new opportunities."
            if all_jobs and all((job.get("application_status") or {}).get("state") in {"applied_by_user", "dismissed"} for job in all_jobs)
            else "No recommendable opportunities remain in this sample. Review eligibility blockers, target roles and source coverage, or run a fresh scan."
            if all_jobs
            else "No opportunities were retrieved in this limited scan. Check source coverage, review your profile, or retry a fresh scan."
        )
    else:
        rows, answer = (
            [],
            "This deterministic demo answers jobs, skill gaps and recruiter/email questions only. Other answers need configured model evidence.",
        )
    return {
        "answer": answer + " Fixture outputs are synthetic." if record.mode == "fixture" else answer,
        "source_ids": [item["id"] for item in rows],
    }


@router.post("/gmail/connect")
async def connect(request: Request, compose: bool = False):
    if settings(request).mode != "live":
        raise AppError("fixture_gmail_disabled", "Switch to live mode to connect Gmail.", 403)
    async with request.app.state.gmail_lock:
        if request.app.state.gmail_revoking:
            raise AppError(
                "gmail_disconnect_pending",
                "Gmail disconnect is finishing. Try connecting again shortly.",
                409,
            )
        scopes = [READ_SCOPE] + ([COMPOSE_SCOPE] if compose else [])
        flow = oauth_flow(settings(request), scopes, autogenerate_code_verifier=True)
        url, state = flow.authorization_url(access_type="offline", prompt="consent")
        request.app.state.gmail_generation += 1
        request.session["oauth"] = {
            "state": state,
            "scopes": scopes,
            "verifier": flow.code_verifier,
            "expires": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "generation": request.app.state.gmail_generation,
        }
    return {"url": url}


def oauth_error(code):
    allowed = {
        "oauth_state_invalid",
        "oauth_denied",
        "gmail_read_scope_missing",
        "oauth_failed",
        "fixture_gmail_disabled",
    }
    return RedirectResponse("/?gmail_error=" + (code if code in allowed else "oauth_failed"), status_code=303)


@router.get("/gmail/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    pending = request.session.get("oauth")
    if (
        not pending
        or not secrets.compare_digest(state.encode(), pending["state"].encode())
        or pending["expires"] < datetime.now(UTC).isoformat()
        or pending.get("generation") != request.app.state.gmail_generation
    ):
        return oauth_error("oauth_state_invalid")
    request.session.pop("oauth", None)
    if error or not code:
        return oauth_error("oauth_denied")
    if settings(request).mode != "live":
        return oauth_error("fixture_gmail_disabled")
    try:
        flow = oauth_flow(
            settings(request), pending["scopes"], state=state, code_verifier=pending["verifier"]
        )
        await asyncio.to_thread(flow.fetch_token, code=code, timeout=20)
        credentials = flow.credentials
        scopes = list(credentials.granted_scopes or credentials.scopes or [])
        if READ_SCOPE not in scopes:
            raise AppError("gmail_read_scope_missing", "Read capability was not granted.", 403)
        token = credentials.to_json()
        parsed = json.loads(token)
        parsed["scopes"] = scopes
        token = json.dumps(parsed)

        def get_account():
            service, refreshed = gmail_service(token)
            return service.users().getProfile(userId="me").execute()["emailAddress"], refreshed.to_json()

        account, token = await asyncio.to_thread(get_account)
        async with request.app.state.gmail_lock:
            if pending["generation"] != request.app.state.gmail_generation:
                return oauth_error("oauth_state_invalid")
            with store(request).session.begin() as db:
                db.merge(
                    Connection(
                        owner=OWNER,
                        account=account,
                        scopes=scopes,
                        encrypted_token=request.app.state.tokens.encrypt(token),
                        consent=False,
                    )
                )
                invalidate_gmail_scans(db, OWNER, "fresh_scan_required")
                request.app.state.gmail_connection_generation += 1
                request.app.state.gmail_generation += 1
                db.execute(
                    update(Action)
                    .where(Action.owner == OWNER, Action.state.in_(["proposed", "approved", "failed"]))
                    .values(state="stale")
                )
        request.session.pop("gmail_disconnect", None)
        request.app.state.gmail_health = {"account": account, "state": "connected"}
        store(request).activity(OWNER, "Gmail connected")
        return RedirectResponse("/?gmail=connected", status_code=303)
    except AppError as error:
        return oauth_error(error.code)
    except Exception:
        return oauth_error("oauth_failed")


@router.post("/gmail/consent")
def consent(request: Request, value: ConsentInput):
    with store(request).session.begin() as db:
        connection = db.get(Connection, OWNER)
        if not connection:
            raise AppError("gmail_disconnected", "Connect Gmail first.")
        connection.consent = value.enabled
    return {"enabled": value.enabled}


@router.post("/gmail/disconnect")
async def disconnect(request: Request):
    if settings(request).mode != "live":
        raise AppError(
            "fixture_gmail_disabled", "Switch to live mode to manage a real Gmail connection.", 403
        )
    async with request.app.state.gmail_lock:
        request.app.state.gmail_generation += 1
        request.app.state.gmail_connection_generation += 1
        request.session.pop("oauth", None)
        with store(request).session.begin() as db:
            connection = db.get(Connection, OWNER)
            encrypted_token = connection.encrypted_token if connection else None
            if connection:
                request.app.state.gmail_revoking = True
            # Remove authority before awaiting Google. Concurrent disconnects are harmless.
            db.execute(delete(Connection).where(Connection.owner == OWNER))
            db.execute(
                update(Action)
                .where(Action.owner == OWNER, Action.state.in_(["proposed", "approved", "failed"]))
                .values(state="stale")
            )
            invalidate_gmail_scans(db, OWNER, "disconnected")
    revoked = False if connection else None
    if connection:
        import httpx

        try:
            token = json.loads(request.app.state.tokens.decrypt(encrypted_token))
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    "https://oauth2.googleapis.com/revoke",
                    data={"token": token.get("refresh_token") or token["token"]},
                )
                revoked = response.status_code == 200
        except Exception:
            pass
        finally:
            request.app.state.gmail_revoking = False
        store(request).activity(OWNER, "Gmail disconnected")
    request.session["gmail_disconnect"] = {
        "revocation_confirmed": revoked,
        "had_connection": bool(connection),
    }
    message = (
        "Gmail disconnected. Google access revocation confirmed."
        if revoked
        else "Gmail disconnected locally. Google revocation could not be confirmed; remove CareerPilot access in your Google account."
        if connection
        else "Gmail is already disconnected."
    )
    return {
        "disconnected": True,
        "revocation_confirmed": revoked,
        "message": message,
        "retention": "Cached scan mail was cleared. Local draft records remain for recovery and are hidden from other Gmail accounts.",
    }


@router.post("/gmail/disconnect/acknowledge")
def acknowledge_disconnect(request: Request):
    request.session.pop("gmail_disconnect", None)
    return {"acknowledged": True}


@router.post("/processing/consent")
def processing_consent(request: Request,value:ConsentInput):
    request.app.state.processing_revoked = not value.enabled
    return {"enabled":value.enabled}
