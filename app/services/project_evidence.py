"""Literal project/job overlap; relevance is evidence, not proficiency."""

import re

STOP = {
    "project",
    "projects",
    "experience",
    "engineer",
    "engineering",
    "developed",
    "development",
    "built",
    "using",
    "with",
    "and",
    "the",
    "for",
    "our",
    "your",
    "required",
    "preferred",
    "work",
    "skills",
    "design",
    "team",
    "system",
    "systems",
    "application",
    "applications",
    "graduation",
    "candidate",
    "candidates",
}


def terms(text: str) -> set[str]:
    return {
        word.casefold()
        for word in re.findall(r"[\w+#]+", text)
        if len(word) >= 3 and word.casefold() not in STOP and not word.isdigit()
    }


def project_evidence(job: dict, profile: dict) -> list[dict]:
    description = job.get("description", "")
    job_terms = terms(description)
    declared = {skill["name"].casefold() for skill in profile.get("skills", [])}
    result = []
    for project in profile.get("projects", []):
        overlap = terms(project).intersection(job_terms)
        acronyms = {word.casefold() for word in re.findall(r"\b[A-Z][A-Z0-9]{1,7}\b", project)}
        distinctive = overlap.intersection(acronyms | declared)
        if len(overlap) < 2 and not distinctive:
            continue
        # Keep both literal source facts and the concrete overlap for review.
        matched = sorted(overlap)
        evidence = next(
            (
                line.strip()
                for line in re.split(r"(?<=[.!?])\s+|\n", description)
                if terms(line).intersection(overlap)
            ),
            description[:1000],
        )
        result.append({"project": project, "job_evidence": evidence[:1000], "shared_terms": matched})
    return result
