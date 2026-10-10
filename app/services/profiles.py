"""Text-first PDF extraction and supported-fact review."""

import re
from io import BytesIO
from typing import NamedTuple

from pypdf import PdfReader

from app.errors import AppError
from app.integrations.llm import LLMClient
from app.schemas import CareerProfile, ProfileExtract, Skill, SourceProfileExtract
from app.services.pdf_layout import layout_pages
from app.services.skills import canonical, mentions

SKILLS = [
    "Python",
    "Java",
    "C++",
    "JavaScript",
    "HTML",
    "CSS",
    "RAG",
    "LLMs",
    "FastAPI",
    "Pydantic",
    "PyTorch",
    "scikit-learn",
    "TensorFlow",
    "Keras",
    "Hugging Face",
    "LangChain",
    "LangGraph",
    "CrewAI",
    "MCP",
    "OpenAI Agents SDK",
    "SQLAlchemy",
    "SQLite",
    "PostgreSQL",
    "MongoDB",
    "FAISS",
    "Chroma",
    "Qdrant",
    "AWS",
    "Docker",
    "Gradio",
    "Tkinter",
    "asyncio",
    "Git",
    "OCR",
]


def scrub_contacts(text: str) -> str:
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email removed]", text)
    def phone(match):
        value = match[0]
        # A year range has eight digits too; preserve dated CV evidence.
        if re.fullmatch(r"(?:19|20)\d{2}\s*-\s*(?:19|20)\d{2}", value.strip()):
            return value
        return "[phone removed]" if len(re.sub(r"\D", "", value)) >= 9 else value
    return re.sub(r"\+?\d[\d ()-]{6,}\d", phone, text)


def read_pdf(raw: bytes) -> str:
    if len(raw) > 5_000_000:
        raise AppError("file_too_large", "CV must be smaller than 5 MB.", 413)
    try:
        reader = PdfReader(BytesIO(raw))
        if reader.is_encrypted:
            raise AppError("encrypted_pdf", "Provide an unlocked PDF or paste its text.")
        if not 1 <= len(reader.pages) <= 10:
            raise AppError("page_limit", "CV must have between 1 and 10 pages.")
        # Legacy PDF symbol fonts expose bullet glyphs as private-use characters.
        pages = [body.translate(str.maketrans({"": "•", "": "•"})) for body in layout_pages(raw)]
        text = "\n".join(f"[Page {i + 1}]\n{body}" for i, body in enumerate(pages))
        if len("".join(pages).strip()) < 50:
            raise AppError("scanned_pdf", "No readable CV text. Paste text; OCR is not implemented.")
        if len(text) > 50000:
            raise AppError("text_limit", "CV text exceeded extraction limit.")
        return text
    except AppError:
        raise
    except Exception:
        raise AppError("invalid_pdf", "Cannot parse this PDF. Paste its readable text.") from None


class CVLine(NamedTuple):
    value: str
    start: int
    end: int


BULLETS = ("•", "-", "*", "", "", "▪")


HEADINGS = {
    "skills": {
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "competencies",
        "core competencies",
        "technical skills & tools",
        "tools & technologies",
        "areas of expertise",
        "expertise",
    },
    "experience": {
        "experience",
        "work experience",
        "professional experience",
        "employment history",
        "work history",
        "volunteer experience",
    },
    "education": {"education", "academic qualifications", "academic background"},
    "projects": {
        "projects",
        "project experience",
        "academic projects",
        "selected projects",
        "personal projects",
    },
    "languages": {"languages", "language skills"},
    "achievements": {
        "achievements",
        "awards",
        "honors",
        "honours",
        "recognition",
        "awards and achievements",
        "achievements and awards",
        "awards & achievements",
        "honors and awards",
        "honours and awards",
        "certifications",
        "certificates",
    },
    "summary": {"profile", "summary", "professional summary", "personal profile"},
    "activities": {"extracurricular activities", "activities", "memberships", "involvement"},
    "other": {
        "objective",
        "career objective",
        "interests",
        "publications",
        "references",
        "contact",
        "contact information",
    },
}


def cv_lines(text: str) -> list[CVLine]:
    result = []
    for match in re.finditer(r"[^\r\n]+", text):
        value = match[0].strip()
        if value and not re.match(r"(?:©|curriculum vitae\s+\S|monash\.edu/|\S+\s+Page\s+\d+)", value, re.I):
            start = match.start() + len(match[0]) - len(match[0].lstrip())
            result.append(CVLine(value, start, start + len(value)))
    return result


def heading(value: str) -> str | None:
    key = re.sub(r"\s+", " ", value.strip(" #*:")).casefold().replace("&", "and")
    direct = next((kind for kind, names in HEADINGS.items() if key in {n.replace("&", "and") for n in names}), None)
    if direct:
        return direct
    # Short section labels vary across professions and countries.
    if len(key.split()) <= 5:
        if key.endswith("experience") or key in {"employment", "employment history – general"}:
            return "experience"
        if key in {"qualifications", "education and training", "education and qualifications", "academic qualifications"}:
            return "education"
        if key.endswith("skills") or key in {"skills summary", "additional skills and courses"}:
            return "skills"
        if key in {"referees", "memberships", "involvement", "annexes", "additional information"}:
            return "other"
    return None


def cv_sections(lines: list[CVLine]) -> dict[str, list[list[CVLine]]]:
    sections = {kind: [] for kind in HEADINGS}
    current = None
    for line in lines:
        if line.value.casefold() == "annexes":
            break
        if re.fullmatch(r"\[Page \d+\]", line.value):
            if current:
                sections[current].append([])
            continue
        language_label = re.match(r"(?:languages?|mother tongue\(s\))\s*[:▪]?\s+(.+)", line.value, re.I)
        language_row = re.fullmatch(r"[^\d]+?\s+(?:[ABC][12]\s*){2,5}", line.value)
        if language_label or language_row:
            current = "languages"
            start = line.start + language_label.start(1) if language_label else line.start
            sections[current].append([CVLine(line.value[start-line.start:], start, line.end)])
            continue
        if re.match(r"other language\(s\)|listening reading|levels:|common european framework", line.value, re.I):
            current = "other"
            continue
        label, separator, rest = line.value.partition(":")
        skill_label = re.match(r"(.{1,50}?\bskills)\s+(?:-\s+)?(.+)", line.value, re.I)
        if skill_label and (line.value.startswith(BULLETS) or ":" in skill_label[1] or re.match(r"(?:I|we|my|our)\b", skill_label[1], re.I)):
            skill_label = None
        if skill_label:
            label, separator, rest = skill_label[1], " ", skill_label[2]
        kind = heading(label if separator else line.value)
        if kind:
            current = kind
            sections[kind].append([])
            if separator and rest.strip():
                start = line.start + skill_label.start(2) if skill_label else line.start + line.value.index(separator) + 1 + len(rest) - len(rest.lstrip())
                sections[kind][-1].append(CVLine(rest.strip(), start, line.end))
        elif current == "languages" and separator and not rest.strip():
            current = "other"
        elif current == "education" and line.value.isupper() and 3 <= len(line.value.split()) <= 8 and not re.search(r"\d", line.value):
            current = "other"
        elif current:
            if not sections[current]:
                sections[current].append([])
            sections[current][-1].append(line)
    return sections


def local_name(lines: list[CVLine]) -> str:
    for line in lines[:8]:
        value = line.value
        explicit = re.match(r"personal information\s+(.+)", value, re.IGNORECASE)
        if explicit:
            return explicit[1].strip()[:150]
        if re.match(r"(?:sample (?:cv|resume)|do not copy|monash\.edu/|use it to generate)", value, re.I):
            continue
        if re.fullmatch(r"\[Page \d+\]", value) or value.casefold() in {"cv", "resume", "curriculum vitae"}:
            continue
        if heading(value):
            break
        if value.casefold().startswith("name:"):
            return value.partition(":")[2].strip()[:150]
        candidate = value.split("|")[0].strip()
        words = candidate.split()
        if not 2 <= len(words) <= 7 or len(candidate) > 150:
            continue
        if re.search(
            r"\d|@|\[|https?://|www\.|linkedin|engineer|developer|manager|technician|consultant|designer|specialist|skills|summary|curriculum",
            candidate,
            re.IGNORECASE,
        ):
            continue
        if all(
            re.fullmatch(r"[^\W\d_][\w.'’\-]*", word) and (word[0].isupper() or not word[0].isascii())
            for word in words
        ):
            return candidate
    return ""


def listed_items(text: str, lines: list[CVLine], *, keep_labels=False) -> list[tuple[str, str]]:
    """Read explicit lists; names may be normalized, evidence remains literal source."""
    blocks = []
    for line in lines:
        if not blocks or ":" in line.value or line.value.startswith(BULLETS):
            blocks.append([])
        blocks[-1].append(line)
    result = []
    for block in blocks:
        raw = text[block[0].start : block[-1].end]
        if not keep_labels:
            raw = re.sub(r"^[^:\r\n]{1,60}:\s*", "", raw)
        pieces, start, depth = [], 0, 0
        for index, char in enumerate(raw):
            if char in "([":
                depth += 1
            elif char in ")]":
                depth = max(0, depth - 1)
            elif char in ",;•▪" and not depth:
                pieces.append(raw[start:index])
                start = index + 1
        pieces.append(raw[start:])
        if len(pieces) == 1:
            pieces = raw.splitlines()
        for piece in pieces:
            evidence = piece.strip().lstrip("-*•▪ ").strip()
            name = re.sub(r"\s+", " ", evidence)
            if not keep_labels:
                name = re.sub(r"^(?:competent|proficient|experienced|familiar)(?: user)?\s+(?:with|in|of)\s+(?:most\s+)?", "", name, flags=re.I)
                if re.search(r"\b(?:I|we|my|our)\b", name, re.I) or re.match(r"(?:for|at|between|whilst|during)\b", name, re.I):
                    continue
            if not name or len(name) > 100 or len(name.split()) > 14:
                continue
            if re.search(r"https?://|www\.|@|\[(?:email|phone) removed\]", name, re.IGNORECASE):
                continue
            result.append((name, evidence))
    return result


def section_facts(text: str, chunks: list[list[CVLine]], kind: str, limit: int) -> list[str]:
    facts = []
    for lines in chunks:
        groups = []
        for line in lines:
            bullet = line.value.startswith(BULLETS)
            starts_entry = (
                kind in {"projects", "achievements"}
                and bullet
                or not bullet
                and bool(re.search(r"\b(?:19|20)\d{2}\b", line.value))
                or kind == "education"
                and bool(
                    re.match(
                        r"(?:B\.?Sc|M\.?Sc|B\.?Eng|M\.?Eng|Bachelor|Master|Ph\.?D|Diploma)\b",
                        line.value,
                        re.IGNORECASE,
                    )
                )
            )
            gap = bool(groups and text[groups[-1][-1].end : line.start].count("\n") > 1)
            if not groups or starts_entry or gap:
                groups.append([])
            groups[-1].append(line)
        facts.extend(text[group[0].start : group[-1].end] for group in groups)
    return list(dict.fromkeys(facts))[:limit]


def explicit_project_facts(text, chunks):
    """Keep wrapped titles with their dated/course labels, using only literal section text."""
    facts=[]
    for chunk in chunks:
        group=[]
        for line in chunk:
            dated=bool(re.search(r"\|\s*(?:19|20)\d{2}\b",line.value))
            if group and dated and any(re.search(r"\|\s*(?:19|20)\d{2}\b",item.value) for item in group):
                facts.append(text[group[0].start:group[-1].end])
                group=[]
            group.append(line)
            if re.fullmatch(r"[\w .&/-]*(?:Course|Graduation Project|Capstone Project)",line.value,re.I):
                facts.append(text[group[0].start:group[-1].end])
                group=[]
        if group:
            facts.append(text[group[0].start:group[-1].end])
    return facts[:30]


def local_profile(text: str) -> CareerProfile:
    # Attached reference material is not a declaration of candidate qualifications.
    annex = re.search(r"(?im)^\s*ANNEXES\s*$", text)
    if annex:
        text = text[:annex.start()]
    lines = cv_lines(text)
    sections = cv_sections(lines)
    found = {}
    for skill in SKILLS:
        match = next(mentions(text, skill), None)
        if match:
            excerpt = text[max(0, match.start() - 50) : min(len(text), match.end() + 100)].strip()
            found[canonical(skill)] = Skill(name=match[0] if canonical(skill) != skill.casefold() or canonical(skill) == "llm" else skill, evidence=excerpt, level="declared")
    for chunk in sections["skills"]:
        for name, evidence in listed_items(text, chunk):
            found.setdefault(canonical(name), Skill(name=name, evidence=evidence[:800], level="declared"))
    projects = section_facts(text, sections["projects"], "projects", 30)
    for chunk in sections["education"]:
        for index, line in enumerate(chunk):
            if re.search(r"\b(?:graduation|capstone|final.year)\s+project\b", line.value, re.IGNORECASE):
                following = []
                for continuation in chunk[index + 1 :]:
                    if continuation.value.startswith(BULLETS):
                        break
                    following.append(continuation)
                projects.append(text[line.start : (following[-1].end if following else line.end)])
    languages = [name for chunk in sections["languages"] for name, _ in listed_items(text, chunk, keep_labels=True)]
    return CareerProfile(
        name=local_name(lines),
        skills=list(found.values())[:100],
        projects=list(dict.fromkeys(projects))[:30],
        experience=section_facts(text, sections["experience"], "experience", 30),
        education=section_facts(text, sections["education"], "education", 20),
        languages=list(dict.fromkeys(languages))[:20],
        achievements=section_facts(text, sections["achievements"], "achievements", 30),
        summary=section_facts(text, sections["summary"], "summary", 20),
        activities=section_facts(text, sections["activities"], "activities", 30),
        source_text=text,
        extraction_method="local_sections",
        priorities="Local CV extraction. Review all facts and declared skills, and choose your career goals.",
    )


def extraction_notice(profile: CareerProfile, settings) -> str:
    if settings.mode == "live" and settings.model_ready:
        return ("Configured model extraction with literal source evidence. "
                + ("Source ranges or explicit sections/tools were completed locally; inspect extraction notes below. " if profile.extraction_notes else "")
                + "Review all extracted facts and set your career goals. "
                + (f"{len(profile.unmapped_text)} source lines remain unmapped; inspect them below before confirming." if profile.unmapped_text else "All substantive source lines were mapped; verify their associations."))
    if not profile.skills:
        return "No skills were recognized by local extraction. Review any copied CV facts and add your skills manually, or configure model extraction. No external model was used."
    return f"Local keyword extraction with CV-section parsing found {len(profile.skills)} declared skills. Some tables, unusual headings and descriptive skills may be missed. Review copied facts, compare separate jobs and qualifications with the original, and set your career goals. No external model was used."


async def extract_profile(text: str, settings, consent: bool) -> CareerProfile:
    text = scrub_contacts(text)
    if settings.mode == "live" and settings.model_ready:
        if not consent:
            raise AppError(
                "processing_consent_required", "Confirm external-model processing before CV extraction.", 403
            )
        lines = cv_lines(text)
        numbered = "\n".join(f"{index}: {line.value}" for index, line in enumerate(lines, 1))
        extracted = await LLMClient(settings).structured(
            SourceProfileExtract,
            "Extract all substantive CV facts by selecting inclusive numbered source-line ranges. "
            "Never rewrite quotations. Each skill name must occur in its selected source range; "
            "choose the explicit skill list where available. Include every named tool, language, "
            "project, award, summary and activity. Keep every employer/title/date and ALL its duties "
            "together in one experience range. Keep each qualification with its own institution, "
            "dates and grades. Select the single literal name line in name_source; copy name literally. Do not infer goals, proficiency, duration "
            "or completed qualifications. A graduation project belongs in projects even when "
            "printed under education. Treat section labels and page markers as boundaries. "
            "Select full wrapped lines; never combine unrelated columns or employers.",
            numbered,
        )
        return project_source_profile(text, extracted)

    return local_profile(text)


def project_source_profile(text, extracted):
    """Resolve model selections to original evidence; invalid or unsupported facts fail closed."""
    if isinstance(extracted, ProfileExtract):
        for skill in extracted.skills:
            if not skill.evidence or skill.evidence not in text or not source_mentions(skill.evidence, skill.name):
                raise AppError("unsupported_profile_fact", "Extracted skill evidence did not match the CV. Review pasted text.")
            skill.level = "declared"
        for group in [extracted.projects, extracted.experience, extracted.education, extracted.languages, extracted.achievements]:
            if any(item not in text for item in group):
                raise AppError("unsupported_profile_fact", "Extracted statements were not exact CV evidence.")
        if extracted.name and extracted.name.casefold() not in text.casefold():
            raise AppError("unsupported_profile_fact", "Extracted name was not present in the CV.")
        return CareerProfile(**extracted.model_dump(), source_text=text, extraction_method="model_exact_quotes")
    lines = cv_lines(text)
    mapped = set()
    notes = []
    def quote(span):
        if span.end < span.start or span.end > len(lines):
            raise AppError("unsupported_profile_fact", "Model selected a source range outside this CV.")
        selected = lines[span.start-1:span.end]
        if any(re.fullmatch(r"\[Page \d+\]", line.value) for line in selected):
            raise AppError("unsupported_profile_fact", "A model fact crossed a page boundary. Review the source.")
        # A nearby section label is not part of the candidate's fact.
        while selected and heading(selected[0].value):
            selected = selected[1:]
        while selected and heading(selected[-1].value):
            selected = selected[:-1]
        if not selected:
            raise AppError("unsupported_profile_fact", "A model fact contained only section labels.")
        mapped.update(index for index,line in enumerate(lines,1) if line in selected)
        return text[selected[0].start:selected[-1].end]
    found = {}
    for item in extracted.skills:
        # A wrapped declaration may continue below the model's chosen final line.
        while item.source.end < len(lines):
            last=lines[item.source.end-1].value
            following=lines[item.source.end].value
            if heading(following) or ":" in following or following.startswith(BULLETS):
                break
            if (not last.endswith((".",";")) and following[:1].islower()
                or last.count("(")>last.count(")") or re.search(r"\band$",last,re.I)):
                item.source.end += 1
            else:
                break
        evidence = quote(item.source)
        if not source_mentions(evidence,item.name):
            # Correct off-by-one ranges only to a literal nearby occurrence, within the same section.
            candidates = []
            for start in range(max(1,item.source.start-2),item.source.start+1):
                for end in range(item.source.end,min(len(lines),item.source.end+2)+1):
                    window = lines[start-1:end]
                    if any(heading(line.value) or re.fullmatch(r"\[Page \d+\]",line.value) for line in window):
                        continue
                    literal = text[window[0].start:window[-1].end]
                    if len(literal)<=800 and source_mentions(literal,item.name):
                        candidates.append((end-start,start,end,literal))
            if candidates:
                _,start,end,evidence = min(candidates)
                mapped.update(range(start,end+1))
                notes.append("Source range expanded to the nearby literal declaration for " + item.name + ".")
        if len(evidence)>800 or not source_mentions(evidence,item.name):
            raise AppError("unsupported_profile_fact", "Extracted skill evidence did not match the CV. Review pasted text.")
        found.setdefault(canonical(item.name), Skill(name=item.name,evidence=evidence,level="declared"))
    for span in extracted.projects:
        if span.end<len(lines) and re.fullmatch(r"[\w .&-]*(?:Course|Graduation Project|Capstone Project)",lines[span.end].value,re.I):
            span.end += 1
            notes.append("Adjacent course or project label retained with its source project.")
    # Model ranges may separately select a course label already covered by its project.
    merged = []
    for span in sorted(extracted.projects, key=lambda item: item.start):
        if merged and span.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, span.end)
            notes.append("Overlapping project source ranges were merged to avoid duplicated labels.")
        else:
            merged.append(span)
    extracted.projects = merged
    facts = {key:list(dict.fromkeys(quote(span) for span in getattr(extracted,key)))
             for key in ["summary","projects","experience","education","languages","achievements","activities"]}
    if extracted.name_source.start != extracted.name_source.end:
        raise AppError("unsupported_profile_fact", "Select a single CV name line.")
    literal_name = quote(extracted.name_source)
    if len(literal_name) > 150 or re.search(r"@|https?://|\[(?:email|phone) removed\]",literal_name,re.I):
        raise AppError("unsupported_profile_fact", "Model selected an invalid name source.")
    if literal_name != extracted.name:
        notes.append("Name copied from the model-selected literal source line; rewritten model spelling was discarded.")
    extracted.name = literal_name
    local = local_profile(text)
    local.projects = explicit_project_facts(text,cv_sections(lines)["projects"])
    # A model can select two adjacent entries as one span. Split only when
    # explicit dated/course blocks account for the entire literal span.
    separate_projects = []
    for fact in facts["projects"]:
        start = text.find(fact)
        end = start + len(fact)
        overlaps = []
        covered = [False] * len(fact)
        for item in local.projects:
            item_start = text.find(item)
            item_end = item_start + len(item)
            if start >= 0 and item_start >= 0 and item_start < end and item_end > start:
                overlaps.append(item)
                for offset in range(max(start,item_start)-start,min(end,item_end)-start):
                    covered[offset] = True
        if overlaps and all(flag or char.isspace() for flag,char in zip(covered,fact,strict=True)):
            separate_projects.extend(overlaps)
            if overlaps != [fact]:
                notes.append("Adjacent projects separated and partial ranges completed at explicit source course boundaries.")
            continue
        complete = next((item for item in local.projects if fact in item and fact != item), None)
        if complete:
            separate_projects.append(complete)
            notes.append("Partial project source range completed from its explicit course block.")
            continue
        contained = [item for item in local.projects if item in fact]
        remainder = fact
        for item in contained:
            remainder = remainder.replace(item, "", 1)
        if len(contained) > 1 and not remainder.strip():
            separate_projects.extend(contained)
            notes.append("Adjacent projects separated at explicit source course boundaries.")
        else:
            separate_projects.append(fact)
    facts["projects"] = list(dict.fromkeys(separate_projects))
    for key in ["summary", "achievements", "activities", "projects"]:
        for fact in getattr(local, key):
            if fact not in text or any(fact in existing for existing in facts[key]):
                continue
            facts[key].append(fact)
            notes.append("Model omitted source " + key + "; the explicit source section was retained locally.")
        for index,line in enumerate(lines,1):
            if any(line.value in fact for fact in facts[key]):
                mapped.add(index)
    for key,skill in list(found.items()):
        if re.search(r"\b(?:prize|award|certificate|certification)\b",skill.name,re.I):
            if not any(skill.evidence in fact for fact in facts["achievements"]):
                facts["achievements"].append(skill.evidence)
            del found[key]
            notes.append("Credential/award source evidence was retained as an achievement, not a skill.")
    labeled_skills = []
    for chunk in cv_sections(lines)["skills"]:
        blocks = []
        for line in chunk:
            if not blocks or ":" in line.value:
                blocks.append([])
            blocks[-1].append(line)
        for block in blocks:
            literal = text[block[0].start:block[-1].end]
            if ":" not in literal or len(literal) > 800:
                continue
            label = literal.split(":", 1)[0].strip()
            if label and len(label) <= 80 and len(label.split()) <= 10 and not re.search(r"\b(?:not|no|without)\b|[?.]",label,re.I):
                labeled_skills.append((label,literal))
            body = re.sub(r"\s+", " ", literal.split(":", 1)[1]).strip()
            names = re.split(r"[,;]|\s+and\s+", body)
            if len(names) < 2 or any(not name.strip() or len(name.split()) > 3 or re.search(r"\b(?:not|no|without|skilled|used|utilized|for|to|with)\b|[().]",name,re.I) for name in names):
                continue
            for name in names:
                name = name.strip()
                if source_mentions(literal,name) and canonical(name) not in found and len(found) < 100:
                    found[canonical(name)] = Skill(name=name, evidence=literal, level="declared")
                    notes.append("Model omitted an explicit skill-list name; retained locally: " + name + ".")
        for line in chunk:
            for example in re.finditer(r"\(e\.g\.?\s*,?\s*([^()]+)\)", line.value, re.I):
                for name in re.split(r"[,;]", example[1]):
                    name = name.strip()
                    if name and len(name) <= 100 and canonical(name) not in found:
                        found[canonical(name)] = Skill(name=name, evidence=line.value, level="declared")
                        notes.append("Model omitted a literal named example in the skill section; retained locally: " + name + ".")
    for label,literal in labeled_skills:
        if canonical(label) not in found and len(found) < 100:
            found[canonical(label)] = Skill(name=label, evidence=literal, level="declared")
            notes.append("Model omitted an explicitly labeled skill; its literal source block was retained locally.")
    if extracted.name and extracted.name.casefold() not in text.casefold():
        raise AppError("unsupported_profile_fact", "Extracted name was not present in the CV.")
    unmapped = [line.value for index,line in enumerate(lines,1)
                if index not in mapped and not heading(line.value)
                and not re.fullmatch(r"\[Page \d+\]",line.value)
                and line.value.casefold()!=extracted.name.casefold()
                and "[email removed]" not in line.value and "[phone removed]" not in line.value]
    return CareerProfile(name=extracted.name,skills=list(found.values()),**facts,
                         source_text=text,unmapped_text=unmapped,extraction_method="model_source_ranges",extraction_notes=notes[:20])


def source_mentions(evidence, name):
    normalized = re.sub(r"\s+", " ", evidence)
    return next(mentions(normalized,name),None) is not None
