import base64
import json
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_oauth import oauth_client as oauth_client
from test_user_journeys import approve, connect, reply, seed_scan

from app import api
from app.db import Action, Connection, Scan
from app.errors import DraftOutcomeUnknown
from app.fixtures import jobs
from app.integrations.gmail import COMPOSE_SCOPE
from app.services.inbox import analyze_threads


def compose(ctx):
    with ctx.app.state.store.session.begin() as db:
        row = db.get(Connection, "local-owner")
        row.scopes = [*row.scopes, COMPOSE_SCOPE]


def test_uncertain_draft_survives_session_and_reconciles_without_duplicate(oauth_client, monkeypatch):
    ctx = oauth_client
    connect(ctx)
    compose(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    assert approve(ctx, draft).status_code == 200
    provider = Mock(side_effect=DraftOutcomeUnknown("uncertain-draft"))
    monkeypatch.setattr(api, "create_verified_draft", provider)
    response = ctx.client.post(f"/api/actions/{draft['id']}/execute")
    assert response.json()["state"] == "unknown"
    assert ctx.client.post(f"/api/actions/{draft['id']}/execute").json()["state"] == "unknown"
    assert provider.call_count == 1
    ctx.client.cookies.clear()
    ctx.client.headers["X-CSRF-Token"] = ctx.client.get("/api/session").json()["csrf"]
    history = ctx.client.get("/api/actions").json()
    assert history[0]["id"] == draft["id"]
    assert history[0]["account_available"] is True
    assert ctx.client.get(f"/api/actions/{draft['id']}").json()["state"] == "unknown"
    credentials = json.dumps({"token": "test-token"})
    monkeypatch.setattr(
        api,
        "reconcile_draft",
        Mock(return_value=({"draft_id": "uncertain-draft", "verified": True}, credentials)),
    )
    reconciled = ctx.client.post(f"/api/actions/{draft['id']}/reconcile")
    assert reconciled.json()["state"] == "succeeded"
    assert provider.call_count == 1


def test_wrong_account_history_is_redacted_and_all_mutations_rejected(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    with ctx.app.state.store.session.begin() as db:
        db.get(Action, draft["id"]).state = "unknown"
    connect(ctx, "account-b@example.invalid")
    history = ctx.client.get("/api/actions").json()
    assert history[0]["account_available"] is False
    assert "to" not in history[0]["payload"]
    assert "body" not in history[0]["payload"]
    assert history[0]["payload"]["subject"] == ""
    for suffix in ["", "/reconcile", "/execute"]:
        response = (
            ctx.client.get(f"/api/actions/{draft['id']}")
            if not suffix
            else ctx.client.post(f"/api/actions/{draft['id']}{suffix}")
        )
        assert response.status_code == 409


def test_read_only_connection_cannot_claim_a_write(oauth_client, monkeypatch):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    approve(ctx, draft)
    provider = Mock()
    monkeypatch.setattr(api, "create_verified_draft", provider)
    response = ctx.client.post(f"/api/actions/{draft['id']}/execute")
    assert response.status_code == 403
    assert response.json()["error"] == "compose_required"
    assert ctx.client.get(f"/api/actions/{draft['id']}").json()["state"] == "approved"
    provider.assert_not_called()


def test_known_failed_draft_can_be_edited_and_reviewed_again(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    with ctx.app.state.store.session.begin() as db:
        db.get(Action, draft["id"]).state = "failed"
    assert approve(ctx, draft).status_code == 200
    with ctx.app.state.store.session.begin() as db:
        db.get(Action, draft["id"]).state = "failed"
    edited = ctx.client.put(
        f"/api/actions/{draft['id']}",
        json={"to": draft["payload"]["to"], "subject": "Updated", "body": "Reviewed replacement content"},
    )
    assert edited.json()["state"] == "proposed"
    assert edited.json()["version"] == draft["version"] + 1


def test_disconnect_clears_persisted_mail_and_retains_job_results(oauth_client, monkeypatch):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda req: httpx.Response(503)), **kw),
    )
    response = ctx.client.post("/api/gmail/disconnect")
    assert response.json()["revocation_confirmed"] is False
    with ctx.app.state.store.session() as db:
        data = db.get(Scan, sid).result
        assert not data["emails"]
        assert data["jobs"]
    assert ctx.client.get("/api/session").json()["gmail_disconnect"]["revocation_confirmed"] is False
    ctx.client.post("/api/gmail/disconnect/acknowledge")
    assert ctx.client.get("/api/session").json()["gmail_disconnect"] is None


def test_bad_state_cannot_consume_a_valid_pending_authorization(oauth_client):
    ctx = oauth_client
    ctx.client.post("/api/gmail/connect")
    cookie_name=next(cookie.name for cookie in ctx.client.cookies.jar if cookie.name.startswith("careerpilot_"))
    original_cookie = ctx.client.cookies.get(cookie_name)
    result = ctx.client.get(
        "/api/gmail/callback", params={"state": "invalid-💥", "code": "test-code"}, follow_redirects=False
    )
    assert "oauth_state_invalid" in result.headers["location"]
    result = ctx.client.get(
        "/api/gmail/callback", params={"state": "test-state", "code": "test-code"}, follow_redirects=False
    )
    assert result.headers["location"] == "/?gmail=connected"
    ctx.flow.fetch_token.assert_called_once()
    # Even a replay of the pre-callback signed cookie cannot reuse the authorization.
    ctx.client.cookies.set(cookie_name, original_cookie, domain="127.0.0.1", path="/")
    replay = ctx.client.get(
        "/api/gmail/callback", params={"state": "test-state", "code": "test-code"}, follow_redirects=False
    )
    assert "oauth_state_invalid" in replay.headers["location"]
    ctx.flow.fetch_token.assert_called_once()


def test_disconnect_cancels_pending_authorization(oauth_client):
    ctx = oauth_client
    ctx.client.post("/api/gmail/connect")
    ctx.client.post("/api/gmail/disconnect")
    result = ctx.client.get(
        "/api/gmail/callback", params={"state": "test-state", "code": "test-code"}, follow_redirects=False
    )
    assert "oauth_state_invalid" in result.headers["location"]
    ctx.flow.fetch_token.assert_not_called()


def test_local_cv_extraction_needs_no_model_or_gmail(oauth_client):
    ctx = oauth_client
    result = ctx.client.post(
        "/api/profile/text",
        json={
            "text": "QA candidate built Python and FastAPI applications with Docker and SQLAlchemy.",
            "processing_consent": False,
        },
    )
    assert result.status_code == 200
    assert {skill["name"] for skill in result.json()["profile"]["skills"]} >= {"Python", "FastAPI", "Docker"}
    assert "Local keyword extraction" in result.json()["notice"]


def test_applied_status_returns_in_current_and_latest_scan(oauth_client):
    ctx = oauth_client
    sid = seed_scan(ctx)
    ctx.client.post("/api/opportunities/status", json={"job_id": jobs()[0]["id"], "state": "applied_by_user"})
    for path in ["/api/scans/" + sid, "/api/scans/latest"]:
        result = ctx.client.get(path).json()
        result = result.get("scan", result)
        assert result["result"]["jobs"][0]["application_status"]["state"] == "applied_by_user"
    assert any(row["event"] == "Application marked applied" for row in ctx.client.get("/api/activity").json())


def mail(body, subject="Interview", sender="Recruiter <recruiter@example.invalid>", extra=None):
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    headers += extra or []
    return [
        {
            "id": "thread",
            "messages": [
                {
                    "id": "message",
                    "internalDate": "1000",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": headers,
                        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,subject,sender,extra,required",
    [
        (
            "Your interview is tomorrow. Please reply to confirm.",
            "Interview",
            "Recruiter <recruiter@example.invalid>",
            [],
            True,
        ),
        (
            "Explore interview stories and job advice. Please share feedback.",
            "Interview newsletter",
            "news@example.invalid",
            [{"name": "List-Unsubscribe", "value": "<https://example.invalid/unsubscribe>"}],
            False,
        ),
        ("Please accept your reward!", "Promotion", "offers@example.invalid", [], False),
        ("Your application was received.", "Application confirmation", "no-reply@example.invalid", [], False),
        ("We have an open role.", "Hiring update", "recruiter@example.invalid", [], False),
    ],
)
async def test_baseline_only_recommends_direct_nonautomated_requests(
    oauth_client, body, subject, sender, extra, required
):
    result = await analyze_threads(
        mail(body, subject, sender, extra), "account-a@example.invalid", oauth_client.app.state.settings
    )
    assert result[0]["requires_response"] is required


def test_reply_uses_reply_to_instead_of_sender(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    with ctx.app.state.store.session.begin() as db:
        scan = db.get(Scan, sid)
        data = json.loads(json.dumps(scan.result))
        data["emails"][0]["reply_to"] = "Hiring team <reply@example.invalid>"
        scan.result = data
    draft = reply(ctx, sid).json()
    assert draft["payload"]["to"] == "reply@example.invalid"
    assert "Reply-To" in draft["payload"]["contact_source"]


def test_upgrade_clears_legacy_mail_without_removing_jobs_or_connection(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    with TestClient(ctx.app, base_url=ctx.app.state.settings.base_url) as restarted:
        assert restarted.get("/").headers["cache-control"] == "no-store"
        result = restarted.get("/api/scans/" + sid).json()["result"]
        assert result["emails"] == []
        assert result["jobs"]
        assert result["coverage"]["gmail"]["state"] == "fresh_scan_required"
        assert restarted.get("/api/session").json()["account"] == "account-a@example.invalid"


def test_canceled_oauth_does_not_interrupt_an_existing_account_scan(oauth_client, monkeypatch):
    import time
    from threading import Event
    from unittest.mock import AsyncMock

    from app.services import workflow

    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    started, release = Event(), Event()
    connection_version = ctx.client.get("/api/session").json()["gmail_version"]

    def read(*args):
        started.set()
        assert release.wait(5)
        return "account-a@example.invalid", mail("Please reply", "Interview"), "synthetic-refreshed-token"

    monkeypatch.setattr(workflow, "fetch_jobs", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(workflow, "account_and_threads", read)
    response = ctx.client.post("/api/scans", json={"processing_consent": False})
    sid = response.json()["id"]
    try:
        assert started.wait(2)
        ctx.client.post("/api/gmail/connect")
        denied = ctx.client.get(
            "/api/gmail/callback",
            params={"state": "test-state", "error": "access_denied"},
            follow_redirects=False,
        )
        assert "oauth_denied" in denied.headers["location"]
        assert ctx.client.get("/api/session").json()["gmail_version"] == connection_version
    finally:
        release.set()
    for _ in range(100):
        result = ctx.client.get("/api/scans/" + sid).json()
        if result["state"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    assert result["state"] == "succeeded", result
    assert result["result"]["coverage"]["gmail"]["state"] == "read"
    assert result["result"]["emails"]


def test_legacy_unbound_email_draft_uses_scan_identity_for_visibility(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    with ctx.app.state.store.session.begin() as db:
        row = db.get(Action, draft["id"])
        row.payload = {**row.payload, "account": ""}
    assert ctx.client.get(f"/api/actions/{draft['id']}").status_code == 200
    connect(ctx, "account-b@example.invalid")
    assert ctx.client.get(f"/api/actions/{draft['id']}").status_code == 409
    assert ctx.client.get("/api/actions").json()[0]["account_available"] is False


def test_fixture_mode_hides_real_gmail_cache_and_drafts(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    ctx.app.state.settings.mode = "fixture"
    assert not ctx.client.get("/api/scans/" + sid).json()["result"]["emails"]
    assert ctx.client.get("/api/actions").json()[0]["account_available"] is False
    assert ctx.client.get(f"/api/actions/{draft['id']}").status_code == 403
    assert (
        ctx.client.post(
            "/api/questions", json={"scan_id": sid, "question": "Which emails need a reply?"}
        ).status_code
        == 403
    )
    assert reply(ctx, sid).status_code == 403
