from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Skill(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    evidence: str = Field(default="", max_length=800)
    level: Literal["declared", "demonstrated", "user_confirmed", "unknown_depth"] = "declared"


class CareerProfile(StrictModel):
    name: str = Field(default="", max_length=150)
    skills: list[Skill] = Field(default_factory=list, max_length=100)
    projects: list[str] = Field(default_factory=list, max_length=30)
    experience: list[str] = Field(default_factory=list, max_length=30)
    education: list[str] = Field(default_factory=list, max_length=20)
    languages: list[str] = Field(default_factory=list, max_length=20)
    achievements: list[str] = Field(default_factory=list, max_length=30)
    summary: list[str] = Field(default_factory=list, max_length=20)
    activities: list[str] = Field(default_factory=list, max_length=30)
    source_text: str = Field(default="", max_length=50000)
    unmapped_text: list[str] = Field(default_factory=list, max_length=2000)
    extraction_method: str = Field(default="", max_length=100)
    extraction_notes: list[str] = Field(default_factory=list, max_length=20)
    target_roles: list[str] = Field(default_factory=list, max_length=10)
    locations: list[str] = Field(default_factory=list, max_length=10)
    remote: bool = True
    seniority: Literal["entry", "junior", "mid", "senior", "any"] = "entry"
    employment_type: str = Field(default="Any", max_length=100)
    salary_minimum: float | None = Field(default=None, ge=0)
    salary_currency: str = Field(default="", max_length=10)
    industries: list[str] = Field(default_factory=list, max_length=10)
    willing_to_learn: bool = True
    priorities: str = Field(default="", max_length=2000)


class ProfileExtract(StrictModel):
    name: str
    skills: list[Skill]
    projects: list[str]
    experience: list[str]
    education: list[str]
    languages: list[str]
    achievements: list[str] = Field(default_factory=list, max_length=30)


class SourceSpan(StrictModel):
    start: int = Field(ge=1, strict=True)
    end: int = Field(ge=1, strict=True)


class SourceSkill(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    source: SourceSpan


class SourceProfileExtract(StrictModel):
    name: str = Field(max_length=150)
    name_source: SourceSpan
    skills: list[SourceSkill] = Field(max_length=100)
    summary: list[SourceSpan] = Field(default_factory=list, max_length=20)
    projects: list[SourceSpan] = Field(max_length=30)
    experience: list[SourceSpan] = Field(max_length=30)
    education: list[SourceSpan] = Field(max_length=20)
    languages: list[SourceSpan] = Field(max_length=20)
    achievements: list[SourceSpan] = Field(default_factory=list, max_length=30)
    activities: list[SourceSpan] = Field(default_factory=list, max_length=30)


class Requirement(StrictModel):
    skill: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1, max_length=1000)


class JobAnalysis(StrictModel):
    required: list[Requirement] = Field(default_factory=list, max_length=40)
    preferred: list[Requirement] = Field(default_factory=list, max_length=30)
    other_required: list[Requirement] = Field(default_factory=list, max_length=40)
    other_preferred: list[Requirement] = Field(default_factory=list, max_length=30)
    experience_years: int | None = Field(default=None, ge=0, le=30)
    experience_evidence: str = ""
    country_restrictions: list[str] = Field(default_factory=list, max_length=20)
    location_evidence: str = ""
    recommendation_note: str = Field(default="", max_length=1500)


class EmailAnalysis(StrictModel):
    category: Literal[
        "interview", "recruiter", "assessment", "opportunity", "confirmation", "rejection", "update", "other"
    ]
    summary: str = Field(max_length=1000)
    requires_response: bool
    evidence: str = Field(max_length=1500)
    deadline: str | None = None
    deadline_evidence: str = ""
    uncertainty: str = Field(default="", max_length=1000)


class DraftText(StrictModel):
    subject: str = Field(min_length=1, max_length=250)
    body: str = Field(min_length=1, max_length=6000)


class SourceDraftPlan(StrictModel):
    profile_fact_ids: list[str] = Field(default_factory=list, max_length=2)
    skill_names: list[str] = Field(default_factory=list, max_length=4)
    request_goal: Literal["application_process", "next_steps", "clarify_requirements"] = "application_process"


class TextInput(StrictModel):
    text: str = Field(min_length=10, max_length=50000)
    processing_consent: bool = False


class ScanInput(StrictModel):
    processing_consent: bool = False


class DraftInput(StrictModel):
    processing_consent: bool = False
    sender_identity_confirmed: bool = False
    sender_account: str = Field(default="", max_length=254)
    scan_id: str
    source_kind: Literal["job", "email"]
    source_id: str
    recipient: str = Field(default="", max_length=254)
    contact_confirmed: bool = False
    contact_source: str = Field(default="", max_length=1000)


class EditDraft(StrictModel):
    subject: str = Field(min_length=1, max_length=250)
    body: str = Field(min_length=1, max_length=6000)
    to: str = Field(max_length=254)


class ApprovalInput(StrictModel):
    payload_hash: str
    version: int
    decision: Literal["approve", "reject"]


class Question(StrictModel):
    processing_consent: bool = False
    previous_question: str = Field(default="",max_length=1000)
    previous_source_ids: list[str] = Field(default_factory=list,max_length=50)
    scan_id: str
    question: str = Field(min_length=1, max_length=1000)


class StatusInput(StrictModel):
    job_id: str = Field(max_length=200)
    state: Literal["saved", "applied_by_user", "dismissed", "undo_applied"]
    expected_state: Literal["saved", "applied_by_user", "dismissed", "untracked"] | None = None


class ConsentInput(StrictModel):
    enabled: bool
