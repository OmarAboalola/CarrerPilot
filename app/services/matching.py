"""Reproducible heuristic fit; no hiring-probability claim."""

import hashlib
import json
import re

from app.integrations.jobs import job_key
from app.schemas import JobAnalysis
from app.services.geography import explicit_restrictions, location_checks
from app.services.project_evidence import project_evidence
from app.services.requirements import normalize_analysis, skill_requirements
from app.services.roles import automation_domain_uncertain, role_matches
from app.services.skills import SKILL_RULES, canonical, mentions


def profile_fingerprint(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def keyword_analysis(job: dict, profile: dict | None = None) -> JobAnalysis:
    text = job["description"]
    required, preferred = skill_requirements(text, profile)
    duration = r"(?<![\d-])(\d+)(?:\s*[-–]\s*\d+)?\s*\+?\s*years"
    years = re.search(duration + r"(?:\s+of)?\s+(?:(?:relevant|professional|hands.on)\s+)?experience\b", text, re.I)
    if not years:
        years = re.search(r"\b(?:experience|minimum|required|requires?|at least)[^\n.]{0,40}?" + duration, text, re.I)
    if not years:
        years = re.search(r"(?im)^\s*(?:[-•*]\s*)?(?:minimum\s+|at least\s+)?" + duration + r"[^.\n]{0,100}?\bexperience\b",text)
    if not years:
        years = re.search(r"(?:خبرة|يشترط)[^\n.]{0,50}?(?:لا تقل عن|على الأقل|حد أدنى)?\s*\[?(\d+)\]?\s*(?:سنوات|سنة|أعوام)",text)
    if years:
        line_start=text.rfind("\n",0,years.start())+1
        line_end=text.find("\n",years.end())
        context=text[line_start:line_end if line_end>=0 else len(text)]
        if re.search(r"\bup to\b|fresh graduates? (?:are )?acceptable|fresh graduate acceptable",context,re.I):
            years=None
    restrictions, location_evidence = explicit_restrictions(text + "\n" + job.get("location", ""))
    return JobAnalysis(
        required=required,
        preferred=preferred,
        experience_years=int(years[1]) if years and int(years[1]) <= 30 else None,
        experience_evidence=years[0] if years else "",
        country_restrictions=restrictions,
        location_evidence=location_evidence,
        recommendation_note="Local evidence baseline. Explicit skill lists and declared-skill mentions are matched literally; depth and unstructured requirements need review. Scores are fit to supported evidence, not hiring probability.",
    )


def current_match(job: dict, profile: dict) -> dict:
    if (job.get("score_version") == "heuristic-v5" and job.get("skill_rules") == SKILL_RULES
        and job.get("match_profile") == profile_fingerprint(profile)):
        unknowns = list(dict.fromkeys(job.get("unknowns", []) + verification_unknowns(profile, job.get("matches", []))))
        return {**job, "unknowns": unknowns, "recommendation": "skip" if job.get("blockers") else "verify" if unknowns else job["recommendation"]}
    analysis = keyword_analysis(job, profile)
    saved = job.get("requirements")
    if saved and not saved.get("recommendation_note", "").startswith(("Keyword baseline", "Local evidence baseline")):
        # Preserve grounded model criteria while upgrading role/geography/project scoring.
        candidate = JobAnalysis.model_validate(saved)
        if all(item.evidence in job["description"] for item in candidate.required + candidate.preferred):
            analysis = candidate
    return match_job(job, analysis, profile)


def verification_unknowns(profile: dict, matches: list[str]) -> list[str]:
    unknowns = ["Salary and currency are unknown; verify the current listing.",
                "Work authorization and mandatory qualifications need confirmation; target geography does not prove eligibility.",
                "Experience duration has not been verified against the CV."]
    declared = {canonical(s["name"]) for s in profile_known(profile).values() if s.get("level") != "demonstrated"}
    if any(canonical(name) in declared for name in matches):
        unknowns.append("Matched skills are declared; proficiency and required depth remain unverified.")
    return unknowns


def match_job(job: dict, analysis: JobAnalysis, profile: dict) -> dict:
    analysis = normalize_analysis(analysis)
    known = profile_known(profile)
    matches, gaps, proficiency_questions = [], [], []
    for item in analysis.required:
        if canonical(item.skill) in known:
            matches.append(item.skill)
        elif source_skill_mention(profile,item.skill):
            proficiency_questions.append(item.skill)
        else:
            gaps.append(item.skill)
    preferred_matches = [r.skill for r in analysis.preferred if canonical(r.skill) in known]
    blockers, unknowns = location_checks(job["location"], analysis.country_restrictions, profile["locations"])
    unknowns.extend(verification_unknowns(profile, matches))
    unknowns.extend("Related CV evidence exists for " + skill + "; confirm the required proficiency and context." for skill in proficiency_questions)
    unknowns.extend("Other mandatory criterion needs verification: " + item.skill for item in analysis.other_required)
    for gap in gaps:
        related = [skill["name"] for skill in profile["skills"] if next(mentions(gap, skill["name"]), None)]
        if related:
            unknowns.append("Declared " + ", ".join(related) + " does not establish the additional requirement: " + gap + ".")
    location = job["location"].casefold()
    remote = "remote" in location
    relevant = role_matches(job, profile.get("target_roles", []))
    if not relevant:
        blockers.append("Title does not match your selected target roles.")
    if automation_domain_uncertain(job, profile.get("target_roles", [])):
        blockers.append("Automation domain needs confirmation: this title describes software/testing automation. Select that domain explicitly before recommending it or counting its gaps.")
    if remote and not profile["remote"]:
        blockers.append("Remote work does not match your selected preference.")
    years = analysis.experience_years
    if profile["seniority"] in {"entry", "junior"} and (
        years is not None and years >= 3 or re.search(r"\b(?:senior|mid[\s-]*level)\b", job["title"], re.I)
    ):
        blockers.append("Requested seniority/experience exceeds the chosen entry/junior target.")
    if profile.get("salary_minimum") is not None:
        unknowns.append(
            "Salary/currency coverage is not implemented by this source; verify your minimum manually."
        )
    if profile.get("employment_type", "Any").casefold() != "any":
        unknowns.append("Employment type is not extracted; verify this preference manually.")
    if profile.get("industries"):
        unknowns.append("Industry preference needs manual verification.")
    if not analysis.required:
        unknowns.append("No supported required-skill list.")
    components = {}
    role_alignment, role_evidence, role_available = role_experience_alignment(job,profile)
    if role_available:
        components["cv_role_relevance"] = (role_alignment,0.45)
    if analysis.required:
        components["required_skills"] = (len(matches) / len(analysis.required), 0.45 if role_available else 0.60)
    if analysis.preferred:
        components["preferred_skills"] = (len(preferred_matches) / len(analysis.preferred), 0.05 if role_available else 0.15)
    if years is not None:
        # CV duration is not assumed. A stated requirement is disclosed, not automatically met.
        unknowns.append(f"Verify the {years}-year experience requirement against your actual experience.")
    project_matches = project_evidence(job, profile)
    projects = [item["project"] for item in project_matches]
    if projects:
        components["project_relevance"] = (1.0, 0.05 if role_available else 0.10)
    total = sum(w for _, w in components.values())
    score = round(100 * sum(v * w for v, w in components.values()) / total) if total else 0
    recommendation = (
        "skip"
        if blockers
        else ("verify" if unknowns else ("apply" if score >= 75 else "consider" if score >= 50 else "weak"))
    )
    return {
        **job,
        "score": score,
        "score_version": "heuristic-v5",
        "skill_rules": SKILL_RULES,
        "match_profile": profile_fingerprint(profile),
        "role_relevant": relevant,
        "coverage": round(total * 100),
        "matches": matches,
        "gaps": gaps,
        "proficiency_questions": proficiency_questions,
        "role_evidence": role_evidence,
        "blockers": blockers,
        "unknowns": unknowns,
        "requirements": analysis.model_dump(),
        "recommendation": recommendation,
        "projects": projects,
        "project_evidence": project_matches,
        "components": {k: {"value": round(v, 3), "weight": w} for k, (v, w) in components.items()},
        "why": analysis.recommendation_note
        or "Fit derives from supported requirements and your confirmed profile.",
    }


def market_summary(jobs: list[dict], profile: dict) -> dict:
    unique = {job_key(j): j for j in jobs}
    eligible = [
        j
        for j in unique.values()
        if not j["blockers"] and j.get("role_relevant", True) and j["requirements"]["required"]
    ]
    counts, preferred = {}, {}
    known = set(profile_known(profile))
    for job in eligible:
        for group, out in [("required", counts), ("preferred", preferred)]:
            for name in {canonical(r["skill"]) for r in job["requirements"][group]}:
                out[name] = out.get(name, 0) + 1
    size = len(eligible)
    items = [
        {
            "skill": skill,
            "count": count,
            "percent": round(count / size * 100) if size >= 10 else None,
            "known": skill in known,
            "source_mentioned": source_skill_mention(profile,skill),
            "preferred_count": preferred.get(skill, 0),
        }
        for skill, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    return {
        "sample_size": size,
        "sample_ids": [j["id"] for j in eligible],
        "skills": items,
        "learning": [item for item in items if not item["known"] and not item["source_mentioned"]][:5] if profile["willing_to_learn"] else [],
        "caveat": "Only this retrieved sample, not the whole market. Counts shown below 10 usable jobs. Declared skills may require depth verification.",
        "mode": jobs[0]["mode"] if jobs else "no data",
    }


def profile_known(profile):
    known = {canonical(s["name"]):s for s in profile["skills"]}
    if {"c", "c++"} <= known.keys():
        known["c/c++"] = {"name":"C/C++", "level":"declared"}
    for line in profile.get("languages",[]):
        for token in re.split(r"[,;\n]",line):
            name = re.sub(r"^Languages?:\s*", "", token.strip(),flags=re.I)
            name = re.sub(r"\s*\([^()]*\)\s*$", "", name)
            if name:
                known.setdefault(canonical(name),{"name":name,"level":"declared"})
    return known


def source_skill_mention(profile,name):
    # A literal source mention is a proficiency question, never an invented declared skill.
    source = profile.get("source_text") or "\n".join(profile.get("summary",[])+profile.get("experience",[])+profile.get("activities",[]))
    if next(mentions(re.sub(r"\s+", " ",source),canonical(name)),None) is not None:
        return True
    families={"leadership":r"\bteam lead(?:er)?\b|\b(?:led|supervised|managed)\s+(?:a |the )?teams?\b", "teamwork":r"\bteam collaboration\b|\bcollaborated with[^.\n]{0,60}\bteams?\b"}
    if canonical(name) in families and re.search(families[canonical(name)],source,re.I):
        return True
    # A broader/compound criterion containing a declared skill needs depth review.
    # This does not add the criterion to declared skills or award match points.
    ignored={"and","of","in","with","within","for","good","strong","technical","skills","knowledge","experience","proficiency","team","teams"}
    criterion=set(re.findall(r"[\w+#]+",canonical(name)))-ignored
    for skill in profile.get("skills",[]):
        tokens=set(re.findall(r"[\w+#]+",canonical(skill["name"])))
        if len(tokens) < 2:
            continue
        words=tokens - ignored
        if words and len(words & criterion)/len(words) >= .5:
            return True
    return False


def role_experience_alignment(job,profile):
    """Match declared CV role headings across professions; shared generic words cannot add relevance."""
    headings=[item.splitlines()[0] for item in profile.get("experience",[]) if item]
    headings += profile.get("source_text","").splitlines()[:8]
    functions={"engineer","developer","manager","technician","coordinator","assistant","nurse","designer","accountant","analyst","scientist","operator"}
    modifiers={"junior","senior","entry","level","part","time","lead","principal","intern","trainee","trained"}
    anchors=[]
    for line in headings:
        match=re.match(r"^(.{1,100}?\b(?:"+"|".join(sorted(functions))+r")\b)",line,re.I)
        if match:
            words=set(re.findall(r"[a-z]+",match[1].casefold()))-modifiers
            if words-functions:
                anchors.append((words,match[1]))
    if not anchors:
        return 0,[],False
    title=set(re.findall(r"[a-z]+",job["title"].casefold()))
    exact=[evidence for words,evidence in anchors if words<=title]
    if exact:
        return 1.0,list(dict.fromkeys(exact)),True
    related=[evidence for words,evidence in anchors if (words-functions)&(title-functions-modifiers)]
    return (0.25 if related else 0),list(dict.fromkeys(related)),True
