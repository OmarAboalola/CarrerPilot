from unittest.mock import AsyncMock, Mock

import pytest
from test_core import client as client

from app.config import Settings
from app.errors import AppError
from app.schemas import ProfileExtract, Skill
from app.services import profiles

MECH_CV = """[Page 1]
TEST CANDIDATE
Mechatronics Engineer | Workshop Operations
PROFILE
An engineer working on vehicle diagnostics.
EXPERIENCE
Workshop Engineer January 2026 - Present
Sample Auto | Cairo, Egypt
• Diagnose electrical faults and use ECU tools.
Workshop Engineer April 2025 - December 2025
Example Garage | Cairo, Egypt
• Repair CAN bus faults.
EDUCATION
B.Sc. Mechatronics Engineering 2020 – 2025
Example University, Egypt
• Graduation Project: Unmanned Surface Vehicle (USV), Grade A+ — a robot
for collecting plastic waste.
SKILLS
Automotive & Diagnostics: Mechanical & Electrical Diagnostics, ECU & Embedded Systems, CAN Bus
Communication, AUTEL, LAUNCH, ISTA (BMW & MINI), ESYS (BMW & MINI), Pluto (Land
Rover & Jaguar)
Engineering Tools: AutoCAD, SolidWorks, PLC Programming, MATLAB, ANSYS, MS Office
Languages: Arabic (Native), English (Fluent)
ACHIEVEMENTS
• Undergraduate Research Forum award
"""


@pytest.mark.asyncio
async def test_mechatronics_cv_preserves_explicit_skills_and_facts_without_model():
    result = await profiles.extract_profile(MECH_CV, Settings(_env_file=None), False)
    names = {item.name for item in result.skills}
    assert result.name == "TEST CANDIDATE"
    assert names >= {"AutoCAD", "SolidWorks", "PLC Programming", "MATLAB", "ANSYS", "CAN Bus Communication"}
    assert "Pluto (Land Rover & Jaguar)" in names
    assert len(result.experience) == 2
    assert any("Example University" in fact for fact in result.education)
    assert any(
        "Unmanned Surface Vehicle" in fact and "collecting plastic waste" in fact for fact in result.projects
    )
    assert result.languages == ["Arabic (Native)", "English (Fluent)"]
    assert "Undergraduate Research Forum award" not in names
    assert all(item.evidence in profiles.scrub_contacts(MECH_CV) for item in result.skills)
    assert result.target_roles == result.locations == []
    assert all(item.level == "declared" for item in result.skills)


def test_new_profession_and_inline_lists_do_not_need_a_fixed_catalogue():
    text = "Jane Doe\nSkills: Brand Strategy; Editorial Planning; Customer Research\nLanguages: French, Spanish\nExperience\nBrand Manager 2024 – Present\nExample Studio"
    result = profiles.local_profile(text)
    assert result.name == "Jane Doe"
    assert {s.name for s in result.skills} == {"Brand Strategy", "Editorial Planning", "Customer Research"}
    assert result.languages == ["French", "Spanish"]
    assert len(result.experience) == 1


def test_existing_keywords_deduplicate_and_keep_literal_wrapped_evidence():
    text = "Alex Smith\nTECHNICAL SKILLS\nTools: Python, python, Docker, CAN Bus\nCommunication\nEDUCATION\nExample University"
    result = profiles.local_profile(text)
    names = [s.name for s in result.skills]
    assert names.count("Python") == 1
    assert names.count("Docker") == 1
    skill = next(s for s in result.skills if s.name == "CAN Bus Communication")
    assert skill.evidence == "CAN Bus\nCommunication"
    assert skill.evidence in text
    assert "Example University" not in names


def test_plain_text_legacy_extraction_does_not_guess_a_name_or_goals():
    result = profiles.local_profile(
        "QA candidate built Python and FastAPI applications with Docker and SQLAlchemy."
    )
    assert {s.name for s in result.skills} >= {"Python", "FastAPI", "Docker", "SQLAlchemy"}
    assert result.name == ""
    assert result.target_roles == result.locations == []


def test_bullet_skills_and_repeated_page_sections():
    text = "[Page 1]\nJane Doe\nSkills\n• PLC Programming\n• AutoCAD\n[Page 2]\nSkills\n• SolidWorks\nProjects\n• Built a fixture.\n• Designed a sensor.\nEducation\nB.Sc. Engineering 2020 – 2024\nExample University"
    result = profiles.local_profile(text)
    assert {s.name for s in result.skills} == {"PLC Programming", "AutoCAD", "SolidWorks"}
    assert len(result.projects) == 2
    assert all("[Page" not in fact for fact in result.projects + result.education)


def test_unrecognized_skills_have_an_actionable_notice_and_do_not_save_profile(client):
    result = client.post(
        "/api/profile/text",
        json={"text": "Jane Doe\nPROFILE\nExperienced specialist in a field not listed here."},
    )
    assert result.status_code == 200
    assert result.json()["profile"]["skills"] == []
    assert "No skills were recognized" in result.json()["notice"]
    assert "add your skills manually" in result.json()["notice"]
    assert client.get("/api/profile").json()["confirmed"] is False


def test_text_extraction_populates_engineering_profile_without_gmail(client):
    result = client.post("/api/profile/text", json={"text": MECH_CV, "processing_consent": False})
    assert result.status_code == 200
    data = result.json()
    assert data["profile"]["name"] == "TEST CANDIDATE"
    assert {s["name"] for s in data["profile"]["skills"]} >= {"PLC Programming", "MATLAB"}
    assert "No external model was used" in data["notice"]
    assert data["confirmed"] is False


@pytest.mark.asyncio
async def test_configured_model_still_needs_consent_and_supported_evidence(monkeypatch):
    settings = Settings(_env_file=None, mode="live", llm_api_key="test-key", llm_model="test-model")
    structured = AsyncMock(
        return_value=ProfileExtract(
            name="Jane Doe",
            skills=[Skill(name="AutoCAD", evidence="AutoCAD")],
            projects=[],
            experience=[],
            education=[],
            languages=[],
        )
    )
    monkeypatch.setattr(profiles, "LLMClient", Mock(return_value=Mock(structured=structured)))
    with pytest.raises(AppError, match="Confirm external-model processing"):
        await profiles.extract_profile("Jane Doe knows AutoCAD", settings, False)
    structured.assert_not_awaited()
    result = await profiles.extract_profile("Jane Doe knows AutoCAD", settings, True)
    assert result.name == "Jane Doe"
    assert "Configured model extraction" in profiles.extraction_notice(result, settings)
    structured.return_value.skills[0].evidence = "Invented source evidence"
    with pytest.raises(AppError, match="did not match the CV"):
        await profiles.extract_profile("Jane Doe knows AutoCAD", settings, True)


def test_large_explicit_skill_list_is_bounded_without_crashing():
    result = profiles.local_profile(
        "Jane Doe\nSkills\n" + ", ".join(f"Engineering Skill {i}" for i in range(140))
    )
    assert len(result.skills) == 100
    assert all(len(s.name) <= 100 and len(s.evidence) <= 800 for s in result.skills)
