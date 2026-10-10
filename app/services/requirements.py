"""Read explicit skill criteria of any profession, retaining literal job evidence."""

import re

from app.schemas import JobAnalysis, Requirement
from app.services.profiles import SKILLS
from app.services.skills import canonical, mentions

LABEL = re.compile(
    r"^(?:[-•*]\s*)?(?:(?:job|professional|technical|basic|minimum)\s+)?(required(?: skills| qualifications)?|requirements|qualifications|"
    r"essential(?: skills)?|must.have|requires?|preferred(?: skills| qualifications)?|"
    r"desirable(?: skills)?|nice.to.have|will be a plus)\b\s*:?\s*",
    re.IGNORECASE,
)
COMMON = SKILLS + ["SQL", "Kubernetes", "Spark", "Airflow", "MLOps", "PLC", "SCADA", "HMI", "MATLAB", "Simulink", "AutoCAD", "SolidWorks", "ANSYS"]


def names_in_list(body: str, vocabulary: list[str]) -> list[str]:
    pieces, start, depth = [], 0, 0
    for index,char in enumerate(body):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0,depth-1)
        elif char in ",;•" and not depth:
            pieces.append(body[start:index])
            start=index+1
    pieces.append(body[start:])
    result = []
    for piece in pieces:
        piece = piece.strip().lstrip("-* ").rstrip(". ")
        parts = re.split(r"\s+and\s+", piece)
        # Don't split skills such as 'Health and Safety'. Split list conjunctions.
        if (
            len(parts) > 1
            and (
                len(pieces) > 1
                or all(
                    part.casefold() in {name.casefold() for name in vocabulary} or part[:1].isupper()
                    for part in parts
                )
            )
            and piece.casefold() not in {name.casefold() for name in vocabulary}
        ):
            candidates = parts
        else:
            candidates = [piece]
        for candidate in candidates:
            candidate = re.sub(
                r"^(?:(?:strong |hands.on )?(?:experience|proficiency|knowledge|familiarity|competency) (?:in|with|of)\s+)",
                "",
                candidate,
                flags=re.IGNORECASE,
            ).strip()
            heading_words = {"and", "or", "skills", "skill", "set", "experience", "requirements", "qualifications", "required", "preferred", "professional", "technical", "essential", "education"}
            words = set(re.findall(r"[a-z]+", candidate.casefold()))
            if words and words <= heading_words:
                continue
            if re.search(r"\b(?:b\.?sc|m\.?sc|ph\.?d|diploma)\b|^(?:or|and)\s+(?:a\s+)?(?:closely\s+)?related.*\b(?:field|discipline)\b", candidate, re.I):
                continue
            candidate = re.sub(r"^\d+[.)]\s*", "", candidate)
            if not candidate or not re.search(r"[A-Za-z]",candidate) or len(candidate) > 100 or len(candidate.split()) > 8:
                continue
            # Non-skill constraints are handled separately, never counted as missing skills.
            if re.search(
                r"\b(?:years?|degree|bachelor|master|citizen|visa|located|salary|relocation|graduate|university|major|willingness|preferably)\b",
                candidate,
                re.IGNORECASE,
            ):
                continue
            if re.search(r"\b(?:not required|optional|no experience)\b", candidate, re.IGNORECASE):
                continue
            if re.match(r"^(?:or|and|the|a|an)\b",candidate,re.I) or candidate.casefold() in {"e.g","systems","m e nts"}:
                continue
            result.append(candidate)
    return result


def skill_requirements(text: str, profile: dict | None = None) -> tuple[list[Requirement], list[Requirement]]:
    vocabulary = list(dict.fromkeys(COMMON + [item["name"] for item in (profile or {}).get("skills", [])]))
    groups = {"required": {}, "preferred": {}}
    explicit_spans = []
    section = None
    # Newlines survive HTML conversion; inline sentences also work for pasted descriptions.
    for match in re.finditer(r"[^\n]+", text):
        for sentence in re.finditer(r".+?(?:\.(?=\s+[A-Z]|$)|$)", match[0]):
            raw = sentence[0].strip()
            if not raw:
                continue
            if re.match(r"^(?:company\b\s*[-:]|job id\b\s*:|data protection\b|application process\b)",raw,re.I):
                section=None
                continue
            label = LABEL.match(raw)
            if label:
                section = (
                    "preferred"
                    if re.match(r"preferred|desirable|nice|will be a plus", label[1], re.IGNORECASE)
                    else "required"
                )
                body = raw[label.end() :]
            elif re.match(
                r"^(?:(?:key|main|core|your)\s+)?(?:responsibilities|duties|tasks|benefits|about|what you|location|salary|compensation)\b",
                raw,
                re.IGNORECASE,
            ):
                section = None
                continue
            elif raw.endswith(":"):
                if not section or not re.search(r"\b(?:experience|knowledge|familiarity|proficiency)\b",raw,re.I):
                    section = None
                continue
            elif section and raw.startswith(("-", "•", "*")):
                body = raw
            elif (
                section
                and "\n" in text
                and len(raw.split()) <= 8
                and not re.search(
                    r"\b(?:we|our|you|your|they|their|company|join|benefits|welcome|offers|will|must|should|is|are|was)\b",
                    raw,
                    re.IGNORECASE,
                )
            ):
                body = raw
            else:
                continue
            start = match.start() + sentence.start()
            kind = "preferred" if re.search(r"\b(?:preferred|desirable|advantage|optional|a plus|not mandatory|nice[ -]to[ -]have)\b",raw,re.I) else section
            explicit_spans.append((start, start + len(sentence[0]), kind))
            for name in names_in_list(body, vocabulary):
                if name.casefold() in text.casefold():
                    excerpt = raw[:1000] if name.casefold() in raw[:1000].casefold() else name
                    groups[kind].setdefault(canonical(name), Requirement(skill=name, evidence=excerpt))
    # Unstructured prose can still support common terms or a user's declared skills.
    # Section/list classification always takes precedence over proximity guesses.
    for name in vocabulary:
        for match in mentions(text, name):
            start = max(text.rfind("\n", 0, match.start()), text.rfind(". ", 0, match.start())) + 1
            stops = [p for p in [text.find("\n", match.end()), text.find(". ", match.end())] if p >= 0]
            end = min(stops) if stops else len(text)
            context = text[start:end]
            if re.search(
                r"\b(?:not required|no (?:prior )?experience (?:in|with)|do not need)\b",
                context,
                re.IGNORECASE,
            ):
                continue
            explicit = next(
                (kind for left, right, kind in explicit_spans if left <= match.start() < right), None
            )
            group = explicit or (
                "preferred"
                if re.search(r"\b(?:preferred|desirable|nice.to.have|optional)\b", context, re.IGNORECASE)
                else "required"
            )
            excerpt = (
                context.strip()
                if len(context.strip()) <= 1000
                else text[max(start, match.start() - 150) : match.end() + 150]
            )
            if not any(canonical(r.skill)==canonical(name) for r in groups[group].values()):
                groups[group].setdefault(canonical(name), Requirement(skill=match[0], evidence=excerpt[:1000] or match[0]))
    # One requirement per declared concept; required wins if both are explicit.
    for name in groups["required"]:
        groups["preferred"].pop(name, None)
    return list(groups["required"].values())[:40], list(groups["preferred"].values())[:30]


def normalize_analysis(analysis):
    """Keep qualifications/constraints as evidence, outside skill denominators."""
    values=analysis.model_dump()
    for group in ["required","preferred"]:
        retained={}
        other=list(values["other_"+group])
        for item in values[group]:
            name=item["skill"]
            if re.match(r"^(?:added by|posted by|job poster)\b",name,re.I):
                other.append(item)
                continue
            if re.search(r"\b(?:degree|bachelor|master|b\.?sc)\b",item["evidence"],re.I) and re.search(r"\b(?:engineering|science|major|field|degree|graduate)\b",name,re.I):
                other.append(item)
                continue
            if (not re.search(r"[A-Za-z]",name) or re.search(r"\b(?:degree|bachelor|master|graduate|university|major|years?|willingness|travel|certificate)\b|\b\d[\d\s+–-]*years?\b",name,re.I)
                or re.match(r"^(?:or|and|preferably|capable|ability|work flexible)\b",name,re.I) or len(name.split())>8):
                other.append(item)
                continue
            retained.setdefault(canonical(name),item)
        values[group]=list(retained.values())
        values["other_"+group]=other[:40 if group=="required" else 30]
    required={canonical(item["skill"]) for item in values["required"]}
    values["preferred"]=[item for item in values["preferred"] if canonical(item["skill"]) not in required]
    return JobAnalysis.model_validate(values)
