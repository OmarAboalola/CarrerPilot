import json
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from test_core import client as client
from test_core import finish_scan
from test_oauth import oauth_client as oauth_client

from app import api
from app.config import Settings
from app.db import Connection, Profile, Scan
from app.errors import AppError
from app.integrations.jobs import plain_text
from app.schemas import CareerProfile, ProfileExtract
from app.security import OWNER
from app.services import workflow
from app.services.geography import countries_in, explicit_restrictions
from app.services.gmail_state import invalidate_gmail_scans, visible_scan_data
from app.services.matching import current_match, keyword_analysis, market_summary, match_job
from app.services.profiles import extract_profile, local_profile
from app.services.roles import role_matches


def profile(role="Workshop Engineer", skills=("AutoCAD", "SolidWorks"), country="Egypt", **extra):
    return CareerProfile(
        name="Sample Candidate",
        skills=[{"name": skill, "evidence": skill} for skill in skills],
        target_roles=[role],
        locations=[country],
        seniority="junior",
        **extra,
    ).model_dump()


def job(
    identifier="suitable",
    title="Workshop Engineer",
    description="Required: AutoCAD, SolidWorks.",
    location="Cairo, Egypt",
):
    return {
        "id": identifier,
        "title": title,
        "description": description,
        "location": location,
        "company": "Synthetic Employer",
        "url": "",
        "source": "Test",
        "mode": "live",
    }


@pytest.mark.parametrize(
    "target,yes,no",
    [
        ("Workshop Engineer", "Junior Workshop Engineer", "Finance Engineer"),
        ("Mechatronics Engineer", "Mechatronics Engineer", "Python Engineer"),
        ("Marketing Specialist", "Senior Marketing Specialist", "Enterprise Sales Executive"),
        ("Registered Nurse", "Nurse", "Research Engineer"),
        ("Accountant", "Senior Accounting Associate", "Account Executive"),
        ("Logistics Coordinator", "Logistics Coordinator", "Warehouse Manager"),
        ("AI Engineer", "Artificial Intelligence Developer", "Retail Engineer"),
        ("ML Engineer", "Machine Learning Engineer", "HTML Developer"),
        ("Backend Engineer", "Backend Developer", "Frontend Developer"),
        ("Data Analyst", "Data Analyst", "Data Engineer"),
        ("History Teacher", "History Teacher", "Physics Teacher"),
        ("HR Generalist", "HR Generalist", "Product Manager"),
    ],
)
def test_titles_follow_selected_profession(target, yes, no):
    assert role_matches(job(title=yes), [target])
    assert not role_matches(job(title=no), [target])
    assert not role_matches(job(title=yes), [])


@pytest.mark.parametrize(
    "role,known,missing,preferred",
    [
        ("Mechatronics Engineer", ["AutoCAD", "SolidWorks", "PLC Programming", "MATLAB"], "SCADA", "ANSYS"),
        ("Nurse", ["Patient triage", "Medication administration"], "CPR", "ECG interpretation"),
        ("Marketing Specialist", ["Campaign planning", "Copywriting"], "Market research", "HubSpot"),
        ("Accountant", ["Bookkeeping", "QuickBooks"], "IFRS", "Tax preparation"),
        ("Logistics Coordinator", ["Inventory control", "Route planning"], "SAP", "Freight forwarding"),
        ("Backend Engineer", ["Python", "FastAPI"], "Kubernetes", "AWS"),
        (
            "History Teacher",
            ["Lesson planning", "Classroom management"],
            "Assessment design",
            "Curriculum development",
        ),
    ],
)
def test_explicit_requirements_extract_matches_and_unknown_skills_for_any_profession(
    role, known, missing, preferred
):
    candidate = profile(role, known)
    description = f"Required skills: {', '.join(known)} and {missing}. Preferred skills: {preferred}."
    source = job(title=role, description=description)
    analysis = keyword_analysis(source, candidate)
    result = match_job(source, analysis, candidate)
    assert set(result["matches"]) == set(known)
    assert result["gaps"] == [missing]
    assert [item.skill for item in analysis.preferred] == [preferred]
    assert all(item.evidence in description for item in analysis.required + analysis.preferred)
    assert result["score"] > 0 and result["coverage"] > 0
    learning = market_summary([result], candidate)["learning"]
    assert [item["skill"] for item in learning] == [missing.casefold()]


def test_html_requirement_sections_keep_required_preferred_and_negation_separate():
    text = plain_text(
        "<h3>Requirements</h3><ul><li>AutoCAD</li><li>SolidWorks</li><li>SCADA</li></ul>"
        "<h3>Preferred skills</h3><ul><li>MATLAB</li></ul><h3>Benefits</h3>"
        "<p>Python is not required.</p><script>Required: invented</script>"
    )
    result = keyword_analysis(job(description=text), profile(skills=("AutoCAD", "SolidWorks", "MATLAB")))
    assert {r.skill for r in result.required} == {"AutoCAD", "SolidWorks", "SCADA"}
    assert {r.skill for r in result.preferred} == {"MATLAB"}
    assert "invented" not in text


def test_unstructured_declared_skill_and_limited_evidence_are_honest():
    candidate = profile("Dentist", ["Dental radiography"])
    source = job(title="Dentist", description="You use Dental radiography in daily patient care.")
    assert match_job(source, keyword_analysis(source, candidate), candidate)["matches"] == [
        "Dental radiography"
    ]
    empty = match_job(
        job(title="Dentist", description="Join our practice."),
        keyword_analysis(job(description="Join our practice.")),
        candidate,
    )
    assert empty["coverage"] == 0 and "No supported required-skill list." in empty["unknowns"]


@pytest.mark.parametrize(
    "target,restriction",
    [
        ("Egypt", "United Kingdom"),
        ("Canada", "United States"),
        ("Germany", "France"),
        ("India", "United Arab Emirates"),
        ("Australia", "Japan"),
        ("Brazil", "Mexico"),
    ],
)
def test_country_mismatch_is_generic_and_explicit(target, restriction):
    candidate = profile(country=target)
    source = job(
        description=f"Required: AutoCAD. Must be based in {restriction}.",
        location=f"Remote ({restriction} only)",
    )
    analysis = keyword_analysis(source, candidate)
    assert analysis.country_restrictions == [restriction]
    assert match_job(source, analysis, candidate)["recommendation"] == "skip"
    assert all(
        line in source["description"] or line in source["location"]
        for line in analysis.location_evidence.splitlines()
    )
    candidate["locations"] = [target, restriction]
    assert not match_job(source, analysis, candidate)["blockers"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Remote (UK only)", ["United Kingdom"]),
        ("Only in Canada or Germany.", ["Canada", "Germany"]),
        ("Limited to the United States and Canada.", ["Canada", "United States"]),
        ("Must reside in UAE.", ["United Arab Emirates"]),
        ("Not limited to Germany.", []),
        ("Remote worldwide; office in Egypt.", []),
    ],
)
def test_country_aliases_multi_country_and_unrestricted_sources(text, expected):
    assert explicit_restrictions(text)[0] == expected


def test_unknown_city_does_not_infer_country_or_work_authorization():
    source = job(description="Required: AutoCAD. UK only.", location="Remote")
    result = match_job(source, keyword_analysis(source), profile(country="Springfield"))
    assert not result["blockers"] and any("country mapping" in s for s in result["unknowns"])
    assert not countries_in("Remote")
    assert not countries_in("Talk to us about the role.")
    assert "authorization" not in " ".join(result["blockers"]).lower()


def test_compound_skill_names_and_unrelated_prose_do_not_generate_false_gaps():
    candidate = profile("Safety Officer", ["Health and Safety", "Risk assessment"])
    source = job(
        title="Safety Officer",
        description="Required: Health and Safety, Risk assessment.\nOur company values teamwork.\nPreferred: Incident investigation.",
    )
    result = match_job(source, keyword_analysis(source, candidate), candidate)
    assert set(result["matches"]) == {"Health and Safety", "Risk assessment"}
    assert result["gaps"] == []
    assert [r["skill"] for r in result["requirements"]["preferred"]] == ["Incident investigation"]


def test_legacy_disconnected_cache_keeps_only_known_safe_errors(oauth_client):
    ctx = oauth_client
    ctx.client.post("/api/profile/confirm", json=profile())
    saved = ctx.app.state.store.latest_profile(OWNER)
    with ctx.app.state.store.session.begin() as db:
        record = Scan(
            owner=OWNER,
            profile_id=saved.id,
            mode="live",
            state="partial",
            result={
                "jobs": [],
                "emails": [],
                "errors": [
                    "Greenhouse board sample: unavailable or invalid response.",
                    "Provider error for private@example.invalid",
                    "Scan failed; private subject",
                ],
            },
        )
        db.add(record)
        db.flush()
        identifier = record.id
    visible = ctx.client.get("/api/scans/" + identifier).json()["result"]
    assert visible["errors"] == ["Greenhouse board sample: unavailable or invalid response."]
    assert "private" not in json.dumps(visible)


@pytest.mark.parametrize(
    "project,description",
    [
        (
            "Unmanned Surface Vehicle (USV) for waste collection",
            "Preferred: Unmanned Surface Vehicle (USV) design.",
        ),
        ("Patient triage workflow improvement", "Required: Patient triage workflow assessment."),
        (
            "Brand awareness campaign for a regional retailer",
            "Preferred: Brand awareness campaign experience.",
        ),
        ("Inventory optimization dashboard", "Required: Inventory optimization reporting."),
        ("Production RAG Backend", "Required: RAG."),
    ],
)
def test_project_overlap_has_traceable_source_evidence(project, description):
    candidate = profile(projects=[project])
    source = job(description=description)
    result = match_job(source, keyword_analysis(source, candidate), candidate)
    assert result["projects"] == [project]
    assert result["project_evidence"][0]["job_evidence"] in description
    assert result["project_evidence"][0]["shared_terms"]
    assert not match_job(
        job(description="Required: Written communication."),
        keyword_analysis(job(description="Required: Written communication.")),
        candidate,
    )["projects"]


@pytest.mark.parametrize(
    "heading,award",
    [
        ("Awards", "National nursing innovation award 2024"),
        ("Achievements", "Best regional marketing campaign 2023"),
        ("Honours and Awards", "Accounting research scholarship 2025"),
        ("Certifications", "Safety training certificate 2026"),
    ],
)
def test_achievement_import_is_literal_and_profession_independent(heading, award):
    text = f"Jane Candidate\nSkills\nCommunication\n{heading}\n{award}\nEducation\nBachelor of Science 2022"
    extracted = local_profile(text)
    assert extracted.achievements == [award]
    assert all(item in text for item in extracted.achievements)
    assert extracted.education


@pytest.mark.asyncio
async def test_model_achievements_reject_invented_awards(monkeypatch, tmp_path):
    from app.services import profiles

    facts = ProfileExtract(
        name="Jane Candidate",
        skills=[],
        projects=[],
        experience=[],
        education=[],
        languages=[],
        achievements=["Invented award"],
    )
    model = AsyncMock(return_value=facts)
    monkeypatch.setattr(profiles.LLMClient, "structured", model)
    with pytest.raises(AppError, match="exact CV evidence"):
        await extract_profile(
            "Jane Candidate\nAwards\nOriginal award",
            Settings(
                _env_file=None, mode="live", data_dir=tmp_path, llm_api_key="synthetic", llm_model="test"
            ),
            True,
        )


def test_recommendations_agree_and_do_not_hide_valid_jobs_after_skipped_first_three(
    oauth_client, monkeypatch
):
    ctx = oauth_client
    ctx.client.post("/api/profile/confirm", json=profile())
    sources = [
        job("senior", "Senior Workshop Engineer"),
        job("restricted", description="Required: AutoCAD. UK only."),
        job("unrelated", "Finance Engineer"),
        job("suitable"),
    ]

    async def fetch(settings):
        return sources, []

    monkeypatch.setattr(workflow, "fetch_jobs", fetch)
    scan = finish_scan(ctx.client)
    assert {j["id"] for j in scan["result"]["jobs"]} == {"senior", "restricted", "suitable"}
    assert scan["result"]["coverage"]["jobs"]["excluded_by_role"] == 1
    assert scan["result"]["market"]["sample_ids"] == ["suitable"]
    assert [a["source_id"] for a in scan["result"]["actions"] if a["source_kind"] == "job"] == ["suitable"]
    assert ctx.client.post(
        "/api/questions", json={"scan_id": scan["id"], "question": "Which jobs should I apply to?"}
    ).json()["source_ids"] == ["suitable"]
    analyzed = [match_job(j, keyword_analysis(j), profile()) for j in sources]
    assert [a["source_id"] for a in workflow.next_actions(analyzed, [], {})] == ["suitable"]
    ctx.client.post("/api/opportunities/status", json={"job_id": "suitable", "state": "dismissed"})
    answer = ctx.client.post(
        "/api/questions", json={"scan_id": scan["id"], "question": "Which jobs should I apply to?"}
    ).json()
    assert answer["source_ids"] == [] and "eligibility blockers" in answer["answer"]


def test_model_question_cannot_receive_or_cite_blocked_jobs(oauth_client, monkeypatch):
    ctx = oauth_client
    ctx.client.post("/api/profile/confirm", json=profile())
    saved = ctx.app.state.store.latest_profile(OWNER)
    sources = [job("senior", "Senior Workshop Engineer"), job("suitable")]
    with ctx.app.state.store.session.begin() as db:
        record = Scan(
            owner=OWNER,
            profile_id=saved.id,
            mode="live",
            state="succeeded",
            result={
                "jobs": [match_job(j, keyword_analysis(j), saved.data) for j in sources],
                "emails": [],
                "processing_consent": True,
            },
        )
        db.add(record)
        db.flush()
        identifier = record.id
    ctx.app.state.settings.llm_api_key = SecretStr("synthetic")
    ctx.app.state.settings.llm_model = "test"
    model = AsyncMock(return_value=api.Answer(answer="Review suitable", source_ids=["suitable"]))
    monkeypatch.setattr(api.LLMClient, "structured", model)
    result = ctx.client.post(
        "/api/questions", json={"scan_id": identifier, "question": "Which jobs should I apply to?", "processing_consent": True}
    )
    assert result.status_code == 200
    supplied = json.loads(model.call_args.args[-1])
    assert [item["id"] for item in supplied["jobs"]] == ["suitable"]
    model.return_value = api.Answer(answer="Apply senior", source_ids=["senior"])
    assert (
        ctx.client.post(
            "/api/questions", json={"scan_id": identifier, "question": "Which jobs should I apply to?", "processing_consent": True}
        ).status_code
        == 502
    )


def test_legacy_profile_unchanged_confirm_preserves_version(client):
    client.post("/api/profile/confirm", json=profile())
    saved = client.get("/api/profile").json()
    with client.app.state.store.session.begin() as db:
        row = db.get(Profile, saved["id"])
        old = dict(row.data)
        old.pop("achievements")
        row.data = old
    result = client.post(
        "/api/profile/confirm", json=saved["profile"], headers={"X-Profile-Version": saved["id"]}
    )
    assert result.status_code == 200 and result.json()["id"] == saved["id"]


@pytest.mark.parametrize("connected", [False, True])
def test_error_provenance_preserves_jobs_model_and_scan_while_redacting_gmail(oauth_client, connected):
    ctx = oauth_client
    ctx.client.post("/api/profile/confirm", json=profile())
    saved = ctx.app.state.store.latest_profile(OWNER)
    sources = [
        {"source": kind, "message": message}
        for kind, message in [
            ("jobs", "Employer unavailable."),
            ("model", "Model fallback used."),
            ("scan", "Interrupted."),
            ("gmail", "Provider error for account-a@example.invalid with private subject"),
        ]
    ]
    with ctx.app.state.store.session.begin() as db:
        if connected:
            db.add(
                Connection(
                    owner=OWNER, account="account-b@example.invalid", encrypted_token="synthetic", scopes=[]
                )
            )
        record = Scan(
            owner=OWNER,
            profile_id=saved.id,
            mode="live",
            state="partial",
            result={
                "jobs": [],
                "emails": [{"id": "private"}],
                "gmail_account": "account-a@example.invalid",
                "errors": [s["message"] for s in sources],
                "error_sources": sources,
                "coverage": {},
            },
        )
        db.add(record)
        db.flush()
        identifier = record.id
    hidden = visible_scan_data(ctx.app.state.store, OWNER, ctx.app.state.store.owned(Scan, identifier, OWNER))
    assert hidden["errors"] == ["Employer unavailable.", "Model fallback used.", "Interrupted."]
    assert "private" not in json.dumps(hidden) and "account-a" not in json.dumps(hidden)
    with ctx.app.state.store.session.begin() as db:
        invalidate_gmail_scans(db, OWNER, "disconnected")
    persisted = ctx.app.state.store.owned(Scan, identifier, OWNER).result
    assert persisted["errors"] == hidden["errors"]
    assert "account-a" not in json.dumps(persisted)


def test_legacy_cache_upgrades_matching_without_dropping_grounded_model_criteria():
    source = job(description="Required: AutoCAD. Certification in surveying is essential.")
    old = {
        **source,
        "score_version": "heuristic-v1",
        "requirements": {
            "required": [
                {"skill": "Surveying certification", "evidence": "Certification in surveying is essential."}
            ],
            "recommendation_note": "Model-grounded extraction.",
        },
    }
    updated = current_match(old, profile(skills=("Surveying certification",)))
    assert updated["score_version"] == "heuristic-v5" and updated["matches"] == ["Surveying certification"]
