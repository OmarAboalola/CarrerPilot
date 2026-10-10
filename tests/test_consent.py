import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Connection, Profile, Scan
from app.fixtures import emails, example_profile, jobs
from app.integrations.gmail import READ_SCOPE
from app.integrations.llm import LLMClient
from app.main import create_app
from app.schemas import EmailAnalysis, SourceDraftPlan
from app.security import OWNER
from app.services import workflow


@pytest.fixture
def consent_client(monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, mode="live",
        llm_api_key="synthetic-key", llm_model="synthetic-model",
        google_client_id="synthetic-client", google_client_secret="synthetic-secret",
    )
    app = create_app(settings)

    async def structured(self, schema, *args):
        if schema is SourceDraftPlan:
            return SourceDraftPlan(request_goal="next_steps")
        if schema is EmailAnalysis:
            return EmailAnalysis(
                category="interview", summary="Reply requested", requires_response=True,
                evidence="Please reply",
            )
        return schema(answer="Reply requested", source_ids=["fixture-interview"])

    model = AsyncMock(side_effect=structured)

    async def dispatch(self, *args):
        return await model(self, *args)

    monkeypatch.setattr(LLMClient, "structured", dispatch)
    with TestClient(app, base_url=settings.base_url) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf"]
        with app.state.store.session.begin() as db:
            profile = Profile(owner=OWNER, data=example_profile(), confirmed=True)
            db.add(profile)
            db.flush()
            scan = Scan(
                owner=OWNER, profile_id=profile.id, mode="live", state="succeeded",
                result={
                    "processing_consent": True, "gmail_account": "test@example.invalid",
                    "emails": emails(), "jobs": [{**jobs()[0], "matches": ["python"]}],
                    "actions": [], "coverage": {"gmail": {"state": "read"}},
                },
            )
            db.add(scan)
            db.add(Connection(
                owner=OWNER, account="test@example.invalid", consent=True, scopes=[READ_SCOPE],
                encrypted_token=app.state.tokens.encrypt("synthetic-token"),
            ))
            db.flush()
            scan_id = scan.id
        yield SimpleNamespace(client=client, app=app, model=model, scan_id=scan_id)


def request_model(ctx, operation):
    if operation == "question":
        return ctx.client.post("/api/questions", json={
            "scan_id": ctx.scan_id, "question": "Which recruiters need a reply?", "processing_consent": True
        })
    return ctx.client.post("/api/drafts", json={
        "scan_id": ctx.scan_id, "source_kind": "email", "source_id": "fixture-interview", "processing_consent": True
    })


@pytest.mark.parametrize("operation", ["question", "draft"])
def test_revocation_blocks_cached_email_model_calls_and_reenable_allows_them(consent_client, operation):
    ctx = consent_client
    assert request_model(ctx, operation).status_code == 200
    ctx.model.reset_mock()
    assert ctx.client.post("/api/gmail/consent", json={"enabled": False}).status_code == 200
    response = request_model(ctx, operation)
    assert response.status_code == 403
    assert response.json()["error"] == "email_processing_consent_required"
    ctx.model.assert_not_awaited()
    assert ctx.client.post("/api/gmail/consent", json={"enabled": True}).status_code == 200
    assert request_model(ctx, operation).status_code == 200
    ctx.model.assert_awaited_once()


@pytest.mark.parametrize("operation", ["question", "draft"])
@pytest.mark.parametrize("scenario", ["disconnected", "changed_account", "legacy_scan", "scan_consent"])
def test_cached_email_cannot_bypass_current_permission(consent_client, operation, scenario):
    ctx = consent_client
    with ctx.app.state.store.session.begin() as db:
        connection = db.get(Connection, OWNER)
        scan = db.get(Scan, ctx.scan_id)
        if scenario == "disconnected":
            db.delete(connection)
        elif scenario == "changed_account":
            connection.account = "different@example.invalid"
        elif scenario == "legacy_scan":
            scan.result = {k: v for k, v in scan.result.items() if k != "gmail_account"}
        else:
            scan.result = {**scan.result, "processing_consent": False}
    response = request_model(ctx, operation)
    assert response.status_code in {403, 409}
    ctx.model.assert_not_awaited()


def test_job_only_drafting_and_questions_do_not_require_gmail_consent(consent_client):
    ctx = consent_client
    with ctx.app.state.store.session.begin() as db:
        db.get(Connection, OWNER).consent = False
        scan = db.get(Scan, ctx.scan_id)
        scan.result = {
            **scan.result, "emails": [], "actions": [], "gmail_account": None,
        }
    ctx.model.side_effect = lambda self, schema, *args: (
        SourceDraftPlan() if schema is SourceDraftPlan
        else schema(answer="Job evidence only", source_ids=[])
    )
    assert request_model(ctx, "question").status_code == 200
    job_id = ctx.app.state.store.owned(Scan, ctx.scan_id, OWNER).result["jobs"][0]["id"]
    response = ctx.client.post("/api/drafts", json={
        "scan_id": ctx.scan_id, "source_kind": "job", "source_id": job_id,
        "processing_consent": True,
        "recipient": "recruiter@example.invalid", "contact_confirmed": True,
        "contact_source": "User confirmed contact",
    })
    assert response.status_code == 200, response.text
    assert ctx.model.await_count == 2


def threads():
    return [{"id": f"thread-{i}", "messages": [{
        "id": f"message-{i}", "internalDate": "1000", "payload": {
            "mimeType": "text/plain", "headers": [
                {"name": "From", "value": "recruiter@example.invalid"},
                {"name": "Subject", "value": "Interview"},
            ], "body": {"data": base64.urlsafe_b64encode(b"Please reply").decode()},
        },
    }]} for i in range(2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_at", ["before_scan", "after_fetch", "after_first_model"])
async def test_scan_rechecks_consent_before_each_email_disclosure(consent_client, monkeypatch, revoke_at):
    ctx = consent_client

    def revoke():
        with ctx.app.state.store.session.begin() as db:
            db.get(Connection, OWNER).consent = False

    fetch = AsyncMock(return_value=([], []))
    monkeypatch.setattr(workflow, "fetch_jobs", fetch)

    def read(*args):
        if revoke_at == "after_fetch":
            revoke()
        return "test@example.invalid", threads(), "synthetic-token"

    monkeypatch.setattr(workflow, "account_and_threads", read)
    if revoke_at == "before_scan":
        revoke()
    original = ctx.model.side_effect

    async def model(self, schema, *args):
        result = await original(self, schema, *args)
        if revoke_at == "after_first_model":
            revoke()
        return result

    ctx.model.side_effect = model
    await workflow.run_scan(ctx.app, ctx.scan_id)
    scan = ctx.app.state.store.owned(Scan, ctx.scan_id, OWNER)
    assert scan.result["coverage"]["gmail"]["state"] == "external_processing_consent_required"
    assert ctx.model.await_count == (1 if revoke_at == "after_first_model" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_model", [True, False])
async def test_scan_records_account_and_supports_local_analysis_without_model(consent_client, monkeypatch, use_model):
    ctx = consent_client
    if not use_model:
        ctx.app.state.settings.llm_model = ""
        with ctx.app.state.store.session.begin() as db:
            db.get(Connection, OWNER).consent = False
    monkeypatch.setattr(workflow, "fetch_jobs", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(
        workflow, "account_and_threads",
        lambda *args: ("test@example.invalid", threads(), "synthetic-token"),
    )
    await workflow.run_scan(ctx.app, ctx.scan_id)
    scan = ctx.app.state.store.owned(Scan, ctx.scan_id, OWNER)
    assert scan.state == "succeeded", scan.result
    assert scan.result["gmail_account"] == "test@example.invalid"
    assert scan.result["coverage"]["gmail"]["state"] == "read"
    assert len(scan.result["emails"]) == 2
    assert ctx.model.await_count == (2 if use_model else 0)
