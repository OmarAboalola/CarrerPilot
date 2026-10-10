from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from test_core import client as client
from test_core import finish_scan
from test_oauth import oauth_client as oauth_client
from test_recovery import compose
from test_user_journeys import approve, connect, reply, seed_scan

from app import api
from app.db import Scan
from app.errors import AppError
from app.fixtures import example_profile


@pytest.mark.parametrize("provenance", ["", "   ", "\t\n"])
def test_outreach_rejects_blank_provenance(client, provenance):
    client.post("/api/profile/confirm", json=example_profile())
    scan = finish_scan(client)
    result = client.post(
        "/api/drafts",
        json={
            "scan_id": scan["id"],
            "source_kind": "job",
            "source_id": scan["result"]["jobs"][0]["id"],
            "recipient": "recruiter@example.invalid",
            "contact_confirmed": True,
            "contact_source": provenance,
        },
    )
    assert result.status_code == 400
    assert result.json()["error"] == "contact_required"
    assert client.get("/api/actions").json() == []


def test_outreach_normalizes_valid_provenance(client):
    client.post("/api/profile/confirm", json=example_profile())
    scan = finish_scan(client)
    result = client.post(
        "/api/drafts",
        json={
            "scan_id": scan["id"],
            "source_kind": "job",
            "source_id": scan["result"]["jobs"][0]["id"],
            "recipient": "recruiter@example.invalid",
            "contact_confirmed": True,
            "contact_source": "  Synthetic company careers page  ",
        },
    )
    assert result.status_code == 200
    assert result.json()["payload"]["contact_source"] == "Synthetic company careers page"


def test_stale_profile_version_cannot_overwrite_or_stale_new_draft(client):
    original = client.post("/api/profile/confirm", json=example_profile()).json()
    updated = {**example_profile(), "target_roles": ["Backend Engineer"]}
    latest = client.post(
        "/api/profile/confirm", json=updated, headers={"X-Profile-Version": original["id"]}
    ).json()
    scan = finish_scan(client)
    draft = client.post(
        "/api/drafts", json={"scan_id": scan["id"], "source_kind": "email", "source_id": "fixture-interview"}
    ).json()
    conflict = client.post(
        "/api/profile/confirm", json=example_profile(), headers={"X-Profile-Version": original["id"]}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "profile_conflict"
    assert client.get("/api/profile").json()["id"] == latest["id"]
    assert client.get(f"/api/actions/{draft['id']}").json()["state"] == "proposed"


def test_profile_update_requires_loaded_version(client):
    client.post("/api/profile/confirm", json=example_profile())
    assert client.post("/api/profile/confirm", json=example_profile()).status_code == 409


def test_concurrent_profile_updates_only_accept_one_version(client):
    initial = client.post("/api/profile/confirm", json=example_profile()).json()
    barrier = Barrier(2)

    def save(name):
        barrier.wait(timeout=5)
        return client.post(
            "/api/profile/confirm",
            json={**example_profile(), "name": name},
            headers={"X-Profile-Version": initial["id"]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(save, ["First edit", "Second edit"]))
    assert sorted(result.status_code for result in responses) == [200, 409]
    accepted = next(result.json() for result in responses if result.status_code == 200)
    assert client.get("/api/profile").json()["id"] == accepted["id"]


def test_unchanged_profile_confirmation_keeps_scan_current(client):
    initial = client.post("/api/profile/confirm", json=example_profile()).json()
    scan = finish_scan(client)
    saved = client.post(
        "/api/profile/confirm", json=example_profile(), headers={"X-Profile-Version": initial["id"]}
    )
    assert saved.status_code == 200
    assert saved.json()["id"] == initial["id"]
    assert client.get(f"/api/scans/{scan['id']}").json()["profile_current"] is True


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("disabled", "Learning suggestions are turned off"),
        ("empty_sample", "no usable jobs"),
        ("no_gaps", "No supported missing-skill priorities"),
    ],
)
def test_empty_learning_answers_explain_next_steps(client, scenario, expected):
    profile = example_profile()
    profile["willing_to_learn"] = scenario != "disabled"
    client.post("/api/profile/confirm", json=profile)
    scan = finish_scan(client)
    with client.app.state.store.session.begin() as db:
        row = db.get(Scan, scan["id"])
        row.result = {
            **row.result,
            "market": {"learning": [], "sample_size": 0 if scenario == "empty_sample" else 2},
        }
    result = client.post(
        "/api/questions", json={"scan_id": scan["id"], "question": "What skills should I learn?"}
    )
    assert result.status_code == 200
    assert expected in result.json()["answer"]
    assert "profile" in result.json()["answer"].lower() or "scan" in result.json()["answer"].lower()


@pytest.mark.parametrize("edit_first", [False, True])
def test_recovered_draft_clears_obsolete_error_but_keeps_activity(oauth_client, monkeypatch, edit_first):
    ctx = oauth_client
    connect(ctx)
    compose(ctx)
    action = reply(ctx, seed_scan(ctx)).json()
    assert approve(ctx, action).status_code == 200
    provider = Mock(side_effect=AppError("provider_failure", "Temporary provider failure."))
    monkeypatch.setattr(api, "create_verified_draft", provider)
    failed = ctx.client.post(f"/api/actions/{action['id']}/execute").json()
    assert failed["state"] == "failed"
    if edit_first:
        failed = ctx.client.put(
            f"/api/actions/{action['id']}",
            json={"to": action["payload"]["to"], "subject": "Edited safely", "body": "Reviewed body"},
        ).json()
        assert failed["result"] == {}
    approved = approve(ctx, failed).json()
    assert approved["state"] == "approved"
    assert approved["result"] == {}
    assert any(
        "Temporary provider failure." in row["event"] for row in ctx.client.get("/api/activity").json()
    )
    assert provider.call_count == 1


def test_expired_gmail_write_requires_reconnect_before_another_provider_call(oauth_client, monkeypatch):
    ctx = oauth_client
    connect(ctx)
    compose(ctx)
    action = reply(ctx, seed_scan(ctx)).json()
    approve(ctx, action)
    provider = Mock(
        side_effect=AppError(
            "gmail_reconnect", "Gmail access expired or was revoked. Reconnect your account.", 401
        )
    )
    monkeypatch.setattr(api, "create_verified_draft", provider)
    failed = ctx.client.post(f"/api/actions/{action['id']}/execute").json()
    assert ctx.client.get("/api/session").json()["gmail_state"] == "reconnect_required"
    approve(ctx, failed)
    assert ctx.client.post(f"/api/actions/{action['id']}/execute").status_code == 401
    assert provider.call_count == 1


@pytest.mark.parametrize("state,remains", [("applied_by_user", False), ("dismissed", False), ("saved", True)])
def test_next_plan_respects_opportunity_status_without_hiding_jobs(client, state, remains):
    client.post("/api/profile/confirm", json=example_profile())
    scan = finish_scan(client)
    job_action = next(a for a in scan["result"]["actions"] if a["source_kind"] == "job")
    job_id = job_action["source_id"]
    assert (
        client.post("/api/opportunities/status", json={"job_id": job_id, "state": state}).status_code == 200
    )
    for refreshed in [
        client.get(f"/api/scans/{scan['id']}").json(),
        client.get("/api/scans/latest").json()["scan"],
    ]:
        result = refreshed["result"]
        assert any(j["id"] == job_id and j["application_status"]["state"] == state for j in result["jobs"])
        assert (
            any(a["source_id"] == job_id and a["source_kind"] == "job" for a in result["actions"]) is remains
        )
        assert any(a["source_kind"] == "email" for a in result["actions"])
    answer = client.post(
        "/api/questions", json={"scan_id": scan["id"], "question": "Which jobs should I apply to?"}
    ).json()
    assert (job_id in answer["source_ids"]) is remains


def test_all_completed_opportunities_answer_explains_status(client):
    client.post("/api/profile/confirm", json=example_profile())
    scan = finish_scan(client)
    for job in scan["result"]["jobs"]:
        client.post("/api/opportunities/status", json={"job_id": job["id"], "state": "applied_by_user"})
    result = client.post(
        "/api/questions", json={"scan_id": scan["id"], "question": "Which jobs should I apply to?"}
    ).json()
    assert "marked applied or dismissed" in result["answer"]
    assert result["source_ids"] == []
