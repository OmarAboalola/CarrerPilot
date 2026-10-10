import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.config import Settings
from app.db import Action, Profile, Scan, Store
from app.errors import AppError
from app.fixtures import example_profile, jobs
from app.integrations.gmail import safe_address
from app.main import create_app
from app.security import OWNER, approve, claim, payload_hash
from app.services.matching import keyword_analysis, market_summary, match_job


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, mode="fixture"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["csrf"]
        client.headers["X-CSRF-Token"] = token
        yield client


def finish_scan(client):
    import time

    response = client.post("/api/scans", json={"processing_consent": False})
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    for _ in range(100):
        result = client.get("/api/scans/" + identifier).json()
        if result["state"] not in {"queued", "running"}:
            return result
        time.sleep(0.02)
    pytest.fail("Fixture scan did not finish")


def test_fixture_vertical_flow_and_tampered_approval(client):
    assert client.get("/health").json()["mode"] == "fixture"
    assert client.post("/api/profile/confirm", json=example_profile()).status_code == 200
    scan = finish_scan(client)
    assert scan["state"] == "succeeded", scan
    assert len(scan["result"]["jobs"]) == 12
    assert scan["result"]["actions"][0]["source_id"] == "fixture-interview"
    assert all(gap["skill"] != "aws" for gap in scan["result"]["market"]["learning"])
    response = client.post(
        "/api/drafts", json={"scan_id": scan["id"], "source_kind": "email", "source_id": "fixture-interview"}
    )
    assert response.status_code == 200, response.text
    draft = response.json()
    assert client.post(f"/api/actions/{draft['id']}/execute").status_code == 403
    tampered = client.post(
        f"/api/actions/{draft['id']}/review",
        json={"payload_hash": "wrong", "version": draft["version"], "decision": "approve"},
    )
    assert tampered.status_code == 409
    assert (
        client.post(
            f"/api/actions/{draft['id']}/review",
            json={"payload_hash": draft["payload_hash"], "version": draft["version"], "decision": "approve"},
        ).status_code
        == 200
    )
    executed = client.post(f"/api/actions/{draft['id']}/execute").json()
    assert executed["result"]["simulation"] is True
    assert executed["result"]["verified"] is False
    assert client.post(f"/api/actions/{draft['id']}/execute").json()["result"] == executed["result"]
    assert client.post("/api/gmail/connect").status_code == 403
    assert (
        client.post(
            "/api/questions", json={"scan_id": scan["id"], "question": "unanswered recruiters"}
        ).status_code
        == 200
    )


def test_profile_edit_invalidates_existing_action(client):
    client.post("/api/profile/confirm", json=example_profile())
    scan = finish_scan(client)
    draft = client.post(
        "/api/drafts", json={"scan_id": scan["id"], "source_kind": "email", "source_id": "fixture-interview"}
    ).json()
    changed = example_profile()
    changed["target_roles"] = ["Backend Engineer"]
    result = client.post(
        "/api/profile/confirm",
        json=changed,
        headers={"X-Profile-Version": client.get("/api/profile").json()["id"]},
    )
    assert result.status_code == 200
    assert (
        client.post(
            f"/api/actions/{draft['id']}/review",
            json={"payload_hash": draft["payload_hash"], "version": 1, "decision": "approve"},
        ).status_code
        == 409
    )


def test_csrf_and_oauth_state_rejection(client):
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/profile/confirm", json=example_profile()).status_code == 403
    denied = client.get("/api/gmail/callback?state=forged&code=forged", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/?gmail_error=oauth_state_invalid"
    assert client.get("/api/session", headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_market_dedup_geography_and_unknown_aws_depth():
    profile = example_profile()
    matched = [match_job(job, keyword_analysis(job), profile) for job in jobs()]
    remote = next(job for job in matched if job["id"] == "fixture-11")
    assert remote["blockers"] and remote["recommendation"] == "skip"
    summary = market_summary(matched, profile)
    assert summary["sample_size"] == 10
    assert all(item["skill"] != "aws" for item in summary["learning"])


def test_upload_errors_and_recipient_injection(client):
    assert (
        client.post(
            "/api/profile/upload", files={"file": ("bad.pdf", b"not pdf", "application/pdf")}
        ).status_code
        == 400
    )
    with pytest.raises(AppError):
        safe_address("valid@example.com\r\nBcc: other@example.com")
    with pytest.raises(AppError):
        safe_address("a@example.com,b@example.com")


def test_ownership_expiry_and_atomic_claim(tmp_path):
    store = Store(tmp_path)
    store.migrate()
    with store.session.begin() as db:
        profile = Profile(owner=OWNER, data=example_profile(), confirmed=True)
        db.add(profile)
        db.flush()
        scan = Scan(owner=OWNER, profile_id=profile.id, mode="fixture", state="succeeded")
        db.add(scan)
        db.flush()
        payload = {
            "account": "fixture@example.invalid",
            "to": "recipient@example.invalid",
            "subject": "test",
            "body": "body",
        }
        action = Action(
            owner=OWNER,
            profile_id=profile.id,
            scan_id=scan.id,
            payload=payload,
            payload_hash=payload_hash(payload),
        )
        db.add(action)
        db.flush()
        identifier = action.id
    with pytest.raises(AppError):
        store.owned(Action, identifier, "different-owner")
    action = store.owned(Action, identifier, OWNER)
    approve(store, identifier, OWNER, action.payload_hash, 1, "approve")
    with store.session.begin() as db:
        db.execute(
            update(Action)
            .where(Action.id == identifier)
            .values(expires=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
        )
    with pytest.raises(AppError):
        claim(store, identifier, OWNER, fixture=True)
    approve(store, identifier, OWNER, action.payload_hash, 1, "approve")

    def attempt():
        try:
            return claim(store, identifier, OWNER, fixture=True)[1]
        except AppError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(lambda _: attempt(), range(2))) == 1
    store.engine.dispose()


def test_live_without_credentials_does_not_call_model(tmp_path):
    from pydantic import BaseModel

    from app.integrations.llm import LLMClient

    class Output(BaseModel):
        category: str

    with pytest.raises(AppError, match="Set LLM_API_KEY"):
        asyncio.run(
            LLMClient(
                Settings(_env_file=None, mode="live", data_dir=tmp_path, llm_api_key="", llm_model="")
            ).structured(Output, "test", "synthetic")
        )
