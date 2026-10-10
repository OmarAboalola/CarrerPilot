"""Equivalent skill terms; evidence and proficiency remain separate."""

import re
import unicodedata

SKILL_RULES = "literal-skills-v10"

GROUPS = {
    "llm": ("LLM", "LLMs", "Large Language Model", "Large Language Models"),
    "hugging face": ("Hugging Face", "HuggingFace"),
    "scikit-learn": ("scikit-learn", "sklearn"),
    "plc": ("PLC", "PLCs", "PLC Programming", "Programmable Logic Controller", "Programmable Logic Controllers"),
    "english": ("English", "English language", "English communication", "English communication skills"),
    "microsoft office": ("Microsoft Office", "MS Office"),
    "microsoft word": ("Microsoft Word", "MS Word", "Word"),
    "microsoft excel": ("Microsoft Excel", "MS Excel", "Excel"),
    "microsoft access": ("Microsoft Access", "MS Access", "Access"),
}
ALIASES = {term.casefold(): key for key, terms in GROUPS.items() for term in terms}


def canonical(skill: str) -> str:
    skill = "".join(char for char in skill if unicodedata.category(char)!="Cf")
    skill = skill.translate(str.maketrans({"‑":"-","–":"-","−":"-"}))
    key = re.sub(r"\s+", " ", skill.strip().casefold())
    key = re.sub(r"\s*/\s*", "/", key)
    key = re.sub(r"^(?:(?:strong|solid|good|excellent|basic|advanced|expert|hands.on|practical|working|deep|extensive|proven)\s+)+", "", key)
    key = re.sub(r"^(?:experience|proficiency|knowledge|expertise|familiarity|background)\s+(?:in|with|of)\s+", "", key)
    key = re.sub(r"\s+(?:experience|proficiency|expertise|skills|knowledge)$", "", key)
    key = re.sub(r"^(?:proficient|familiar|experienced|skilled)\s+(?:in|with|at)\s+", "", key)
    key = re.sub(r"^command of\s+", "", key)
    paired = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", key)
    if (
        paired
        and ALIASES.get(paired[1].strip()) == ALIASES.get(paired[2].strip())
        and paired[1].strip() in ALIASES
    ):
        key = paired[1].strip()
    return ALIASES.get(key, key)


def terms(skill: str) -> tuple[str, ...]:
    return GROUPS.get(canonical(skill), (skill,))


def mentions(text: str, skill: str):
    pattern = "|".join(re.escape(term) for term in sorted(terms(skill), key=len, reverse=True))
    return re.finditer(r"(?<![\w+#])(?:" + pattern + r")(?![\w+#])", text, re.IGNORECASE)
