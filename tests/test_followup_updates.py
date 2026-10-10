import base64
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from test_core import client as client
from test_generic_matching import job, profile
from test_oauth import oauth_client as oauth_client
from test_user_journeys import connect, seed_scan

from app.config import Settings
from app.db import Connection, Scan
from app.integrations.gmail import conversation_url
from app.main import create_app
from app.security import OWNER
from app.services.inbox import analyze_threads
from app.services.matching import current_match, keyword_analysis, market_summary, match_job
from app.services.profiles import local_profile
from app.services.skills import canonical


@pytest.mark.parametrize(
    "wording",
    ["LLM", "LLMs", "Large Language Model", "Large Language Models", "Large Language Models (LLMs)"],
)
def test_equivalent_skill_extraction_is_grounded_and_declared(wording):
    text = f"Example Candidate\nSkills\n{wording}, Python\nExperience\nBuilt prototypes."
    extracted = local_profile(text)
    skills = [s for s in extracted.skills if canonical(s.name) == "llm"]
    assert len(skills) == 1
    assert skills[0].name in text and skills[0].evidence in text
    assert skills[0].level == "declared"
    assert extracted.target_roles == []


@pytest.mark.parametrize(
    "known,required",
    [
        ("LLM", "Large Language Models"),
        ("Large Language Models", "LLMs"),
        ("HuggingFace", "Hugging Face"),
        ("sklearn", "scikit-learn"),
    ],
)
def test_equivalence_reaches_matching_and_market(known, required):
    person = profile("AI Engineer", [known])
    role = job(title="AI Engineer", description=f"Required: {required}, SQL.")
    matched = match_job(role, keyword_analysis(role, person), person)
    assert canonical(required) in {canonical(s) for s in matched["matches"]}
    assert matched["gaps"] == ["SQL"]
    assert "proficiency" in " ".join(matched["unknowns"])
    assert [s["skill"] for s in market_summary([matched], person)["learning"]] == ["sql"]


def test_advanced_skill_is_not_inferred_and_restrictions_survive():
    person = profile("AI Engineer", ["LLMs"], country="Egypt")
    role = job(
        title="Senior AI Engineer",
        description="Required: LLM fine-tuning, SQL. Candidates must be based in United States. 5 years experience.",
        location="Remote (United States only)",
    )
    result = match_job(role, keyword_analysis(role, person), person)
    assert "LLM fine-tuning" in result["gaps"]
    assert result["recommendation"] == "skip" and len(result["blockers"]) >= 2
    assert "additional requirement: LLM fine-tuning" in " ".join(result["unknowns"])


def test_cached_analysis_rechecks_skill_rules_and_profile():
    person = profile("AI Engineer", ["Python"])
    role = job(title="AI Engineer", description="Required: LLMs, SQL.")
    cached = match_job(role, keyword_analysis(role, person), person)
    reviewed = profile("AI Engineer", ["Large Language Models"])
    repaired = current_match(cached, reviewed)
    assert repaired["gaps"] == ["SQL"]
    assert repaired["match_profile"] != cached["match_profile"]
    assert market_summary([repaired], reviewed)["learning"][0]["skill"] == "sql"
    legacy = dict(repaired)
    legacy.pop("skill_rules")
    legacy["gaps"] = ["LLMs", "SQL"]
    assert current_match(legacy, reviewed)["gaps"] == ["SQL"]


@pytest.mark.parametrize("previous", ["untracked", "saved", "dismissed"])
def test_applied_undo_restores_previous_state_and_survives_restart(client, previous):
    body = {"job_id": "tracking-fixture", "state": "saved" if previous == "untracked" else previous}
    if previous != "untracked":
        assert client.post("/api/opportunities/status", json=body).status_code == 200
    mark = {"job_id": body["job_id"], "state": "applied_by_user", "expected_state": previous}
    result = client.post("/api/opportunities/status", json=mark).json()
    assert result["previous_state"] == previous
    undo = {"job_id": body["job_id"], "state": "undo_applied", "expected_state": "applied_by_user"}
    assert client.post("/api/opportunities/status", json=undo).json()["state"] == previous
    assert (
        client.post(
            "/api/opportunities/status", json={"job_id": body["job_id"], "state": "undo_applied"}
        ).json()["state"]
        == previous
    )
    restarted = create_app(Settings(data_dir=client.app.state.settings.data_dir, mode="fixture"))
    with TestClient(restarted, base_url="http://127.0.0.1:8765") as other:
        assert other.get("/api/opportunities/status/tracking-fixture").json()["state"] == previous
        other.headers["X-CSRF-Token"] = other.get("/api/session").json()["csrf"]
        assert other.post("/api/opportunities/status", json=mark).json()["state"] == "applied_by_user"
        assert other.post("/api/opportunities/status", json=mark).status_code == 409


def test_legacy_applied_row_migrates_and_undo_uses_neutral_state(tmp_path):
    with sqlite3.connect(tmp_path / "careerpilot.db") as db:
        db.execute(
            "CREATE TABLE opportunity_status (id VARCHAR PRIMARY KEY, owner VARCHAR, state VARCHAR, created VARCHAR)"
        )
        db.execute(
            "INSERT INTO opportunity_status VALUES ('local-owner:old','local-owner','applied_by_user','2026-10-01')"
        )
    app = create_app(Settings(data_dir=tmp_path, mode="fixture"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as ctx:
        ctx.headers["X-CSRF-Token"] = ctx.get("/api/session").json()["csrf"]
        assert (
            ctx.post("/api/opportunities/status", json={"job_id": "old", "state": "undo_applied"}).json()[
                "state"
            ]
            == "untracked"
        )


def test_gmail_link_uses_header_identity_and_explicit_account():
    url, kind = conversation_url("second.account@example.invalid", "<request+42@example.invalid>")
    parsed = urlsplit(url)
    assert parsed.netloc == "mail.google.com" and "/u/0/" not in parsed.path
    assert parse_qs(parsed.query)["authuser"] == ["second.account@example.invalid"]
    assert unquote(parsed.fragment) == "search/rfc822msgid:request+42@example.invalid"
    assert kind == "message_search"
    assert conversation_url("account@example.invalid", "api-hex-id")[1] == "account_inbox"
    assert conversation_url("account@example.invalid", "x@example.invalid:from:other")[1] == "account_inbox"
    assert conversation_url("account@example.invalid", "<bad\n@example.invalid>")[1] == "account_inbox"


def test_gmail_open_route_rechecks_account_and_never_calls_provider(oauth_client):
    ctx = oauth_client
    connect(ctx)
    sid = seed_scan(ctx)
    with ctx.app.state.store.session.begin() as db:
        row = db.get(Scan, sid)
        value = dict(row.result)
        value["emails"] = [
            {"id": "thread-hex", "mode": "live", "reply_message_id": "<request@example.invalid>"}
        ]
        row.result = value
    route = f"/api/scans/{sid}/emails/thread-hex/open"
    response = ctx.client.get(route, follow_redirects=False)
    assert response.status_code == 302 and "rfc822msgid" in response.headers["location"]
    with ctx.app.state.store.session.begin() as db:
        db.get(Connection, OWNER).account = "another@example.invalid"
    assert ctx.client.get(route, follow_redirects=False).status_code == 409


@pytest.mark.asyncio
async def test_mail_relevance_keeps_requests_and_excludes_newsletters_and_answers(tmp_path):
    def thread(identifier, subject, body, sent=False):
        message = {
            "id": identifier,
            "internalDate": "1000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "recruiter@example.invalid"},
                    {"name": "Subject", "value": subject},
                ],
                "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
            },
        }
        result = {"id": identifier, "messages": [message]}
        if sent:
            result["messages"].append(
                {
                    **message,
                    "id": identifier + "-sent",
                    "internalDate": "2000",
                    "labelIds": ["SENT"],
                    "payload": {
                        **message["payload"],
                        "body": {
                            "data": base64.urlsafe_b64encode(b"Confirmed, Tuesday works for me.").decode()
                        },
                    },
                }
            )
        return result

    rows = await analyze_threads(
        [
            thread("wanted", "Recruiter interview", "Could you confirm your availability?"),
            thread("digest", "Weekly job digest", "Please reply to unsubscribe."),
            thread("done", "Recruiter interview", "Please confirm Tuesday.", True),
        ],
        "candidate@example.invalid",
        Settings(data_dir=tmp_path, mode="fixture"),
    )
    assert [x["id"] for x in rows if x["requires_response"]] == ["wanted"]


def test_company_age_does_not_become_candidate_experience():
    person = profile("AI Engineer", ["Python"])
    role = job(
        title="AI Engineer",
        description="Our company has 175 years of history. Required: Python. 2–3 years of experience.",
    )
    analysis = keyword_analysis(role, person)
    assert analysis.experience_years == 2
    assert analysis.experience_evidence == "2–3 years of experience"
    unrelated = job(description="We have served clients for 175 years. Required: Python.")
    assert keyword_analysis(unrelated, person).experience_years is None


def test_explicit_mid_level_title_does_not_become_an_entry_recommendation():
    person = profile("AI Engineer", ["LLM", "Python"])
    role = job(title="Mid-Level AI Engineer", description="Required: LLMs, Python.")
    assert match_job(role, keyword_analysis(role, person), person)["recommendation"] == "skip"


@pytest.mark.parametrize(
    "wording", ["Strong Python experience", "Advanced knowledge of Large Language Models"]
)
def test_proficiency_wording_is_an_unknown_depth_not_a_basic_gap(wording):
    person = profile("AI Engineer", ["Python", "LLMs"])
    role = job(title="AI Engineer", description="Required: " + wording + ", SQL.")
    matched = match_job(role, keyword_analysis(role, person), person)
    assert matched["gaps"] == ["SQL"]
    assert [item["skill"] for item in market_summary([matched], person)["learning"]] == ["sql"]
    assert any("proficiency" in unknown for unknown in matched["unknowns"])


@pytest.mark.parametrize(
    "body",
    [
        "Are you available for an interview on Thursday?",
        "Unfortunately we cannot meet Tuesday. Would Thursday work for you?",
    ],
)
def test_unseen_interview_questions_are_requests_not_rejections(body):
    from app.services.inbox import baseline_analysis

    analysis = baseline_analysis({"subject": "Interview scheduling", "body": body, "automated": False}, False)
    assert analysis.category == "interview" and analysis.requires_response
    assert analysis.evidence in body
    automated = baseline_analysis({"subject": "Interview scheduling", "body": body, "automated": True}, False)
    assert not automated.requires_response


def test_composite_headings_and_degree_fragments_do_not_become_skill_gaps():
    person = profile("AI Engineer", ["Python", "LLMs"])
    role = job(
        title="AI Engineer",
        description="Job Requirements & Qualifications:\n- B.Sc. of Computer Science or Computer Engineering\n- Python, LLMs, SQL\nKey Responsibilities\n- including task allocation\n- performance feedback",
    )
    result = match_job(role, keyword_analysis(role, person), person)
    assert result["gaps"] == ["SQL"]
    assert any("mandatory qualifications" in x for x in result["unknowns"])
