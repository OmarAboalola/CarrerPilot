"""Supplemental tests of user-facing recovery and account isolation.

These tests enforce fixed product behavior and account isolation.
All provider boundaries are synthetic; no Google access occurs here.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx
import pytest
from test_oauth import oauth_client as oauth_client

from app import api
from app.db import Action, Profile, Scan
from app.fixtures import emails, example_profile, jobs
from app.integrations.gmail import READ_SCOPE
from app.security import OWNER
from app.services.matching import keyword_analysis, match_job


def connect(ctx, account="account-a@example.invalid"):
    ctx.flow.credentials.granted_scopes = [READ_SCOPE]
    api.gmail_service.return_value[0].users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": account
    }
    assert ctx.client.post("/api/gmail/connect").status_code == 200
    response = ctx.client.get(
        "/api/gmail/callback", params={"state": "test-state", "code": "test-code"}, follow_redirects=False
    )
    assert response.status_code == 303


def seed_scan(ctx, account="account-a@example.invalid"):
    with ctx.app.state.store.session.begin() as db:
        profile = Profile(owner=OWNER, data=example_profile(), confirmed=True)
        db.add(profile)
        db.flush()
        scan = Scan(
            owner=OWNER,
            profile_id=profile.id,
            mode="live",
            state="succeeded",
            result={
                "gmail_account": account,
                "emails": emails(),
                "jobs": [match_job(jobs()[0], keyword_analysis(jobs()[0]), example_profile())],
                "actions": [],
                "coverage": {"gmail": {"state": "read"}},
                "processing_consent": False,
            },
        )
        db.add(scan)
        db.flush()
        return scan.id


def reply(ctx, scan_id):
    return ctx.client.post(
        "/api/drafts", json={"scan_id": scan_id, "source_kind": "email", "source_id": emails()[0]["id"]}
    )


def approve(ctx, draft):
    return ctx.client.post(
        f"/api/actions/{draft['id']}/review",
        json={"payload_hash": draft["payload_hash"], "version": draft["version"], "decision": "approve"},
    )


@pytest.mark.parametrize(
    "scenario,error",
    [
        ("deny", "oauth_denied"),
        ("incomplete", "oauth_denied"),
        ("bad_state", "oauth_state_invalid"),
        ("missing_session", "oauth_state_invalid"),
        ("failed_exchange", "oauth_failed"),
        ("missing_read", "gmail_read_scope_missing"),
    ],
)
def test_oauth_errors_are_safe(oauth_client, scenario, error):
    ctx = oauth_client
    if scenario != "missing_session":
        ctx.client.post("/api/gmail/connect")
    params = {"state": "test-state", "code": "test-code"}
    if scenario == "deny":
        params.update(error="access_denied", code="")
    if scenario == "incomplete":
        params["code"] = ""
    if scenario == "bad_state":
        params["state"] = "wrong-state"
    if scenario == "failed_exchange":
        ctx.flow.fetch_token.side_effect = httpx.ConnectError("offline")
    if scenario == "missing_read":
        ctx.flow.credentials.granted_scopes = ["unrelated-scope"]
    response = ctx.client.get("/api/gmail/callback", params=params, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?gmail_error=" + error
    assert ctx.client.get("/api/session").json()["account"] is None


def test_oauth_denial_returns_to_recoverable_app(oauth_client):
    ctx = oauth_client
    ctx.client.post("/api/gmail/connect")
    response = ctx.client.get(
        "/api/gmail/callback",
        params={"state": "test-state", "error": "access_denied"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/")


def test_account_switch_rejects_previous_account_reply(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    connect(ctx, "account-b@example.invalid")
    response = reply(ctx, sid)
    assert response.status_code == 409
    assert response.json()["error"] in {"email_account_changed", "scan_stale"}


def test_account_switch_hides_previous_account_mail(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    connect(ctx, "account-b@example.invalid")
    response = ctx.client.get("/api/scans/" + sid)
    assert not response.json()["result"].get("emails")


def test_account_switch_rejects_previous_account_gmail_questions(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    connect(ctx, "account-b@example.invalid")
    response = ctx.client.post(
        "/api/questions", json={"scan_id": sid, "question": "Which emails need a reply?"}
    )
    assert response.status_code == 409


def test_disconnect_stales_actions_and_blocks_execute(oauth_client, monkeypatch):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    assert approve(ctx, draft).status_code == 200
    provider = Mock()
    monkeypatch.setattr(api, "create_verified_draft", provider)
    real_client = httpx.AsyncClient

    def async_client(**kw):
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200)), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    result = ctx.client.post("/api/gmail/disconnect")
    assert result.json()["disconnected"] is True
    assert result.json()["revocation_confirmed"] is True
    assert ctx.client.get("/api/session").json()["account"] is None
    with ctx.app.state.store.session() as db:
        assert db.get(Action, draft["id"]).state == "stale"
    assert ctx.client.get(f"/api/actions/{draft['id']}").status_code == 409
    assert ctx.client.post(f"/api/actions/{draft['id']}/execute").status_code == 409
    provider.assert_not_called()


def test_failed_revocation_removes_local_credentials(oauth_client, monkeypatch):
    ctx = oauth_client
    connect(ctx)
    real_client = httpx.AsyncClient

    def async_client(**kw):
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(503)), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    result = ctx.client.post("/api/gmail/disconnect")
    assert result.json()["disconnected"] is True
    assert result.json()["revocation_confirmed"] is False
    assert ctx.client.get("/api/session").json()["account"] is None


def test_reconnect_invalidates_previous_approval(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    approve(ctx, draft)
    connect(ctx, "account-b@example.invalid")
    with ctx.app.state.store.session() as db:
        assert db.get(Action, draft["id"]).state == "stale"


def test_expired_approval_cannot_write(oauth_client, monkeypatch):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    draft = reply(ctx, sid).json()
    approve(ctx, draft)
    with ctx.app.state.store.session.begin() as db:
        db.get(Action, draft["id"]).expires = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    provider = Mock()
    monkeypatch.setattr(api, "create_verified_draft", provider)
    response = ctx.client.post(f"/api/actions/{draft['id']}/execute")
    assert response.status_code == 409
    assert response.json()["error"] == "approval_expired"
    provider.assert_not_called()


def test_invalid_session_csrf_requires_refresh(oauth_client):
    ctx = oauth_client
    ctx.client.cookies.clear()
    response = ctx.client.post("/gmail/connect")
    assert response.status_code == 404
    response = ctx.client.post("/api/gmail/connect")
    assert response.status_code == 403
    assert response.json()["error"] == "csrf_invalid"
    ctx.client.headers["X-CSRF-Token"] = ctx.client.get("/api/session").json()["csrf"]
    assert ctx.client.post("/api/gmail/connect").status_code == 200


def test_disconnected_job_draft_is_local_and_cannot_execute(oauth_client, monkeypatch):
    ctx = oauth_client
    sid = seed_scan(ctx)
    response = ctx.client.post(
        "/api/drafts",
        json={
            "scan_id": sid,
            "source_kind": "job",
            "source_id": jobs()[0]["id"],
            "recipient": "recruiter@example.invalid",
            "contact_confirmed": True,
            "contact_source": "QA synthetic contact",
        },
    )
    assert response.status_code == 200
    draft = response.json()
    assert draft["payload"]["account"] == ""
    assert approve(ctx, draft).status_code == 200
    provider = Mock()
    monkeypatch.setattr(api, "create_verified_draft", provider)
    result = ctx.client.post(f"/api/actions/{draft['id']}/execute")
    assert result.status_code == 409
    assert result.json()["error"] == "account_changed"
    provider.assert_not_called()


def test_double_disconnect_is_idempotent(oauth_client, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    ctx = oauth_client
    connect(ctx)
    real_client = httpx.AsyncClient

    async def delayed_revoke(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(delayed_revoke), **kw)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ctx.client.post("/api/gmail/disconnect"), range(2)))
    assert all(response.status_code == 200 for response in results)
