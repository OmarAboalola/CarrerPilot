"""Conservative title matching from the user's roles, across professions."""

import re

MODIFIERS = {
    "junior",
    "senior",
    "entry",
    "level",
    "lead",
    "principal",
    "staff",
    "intern",
    "internship",
    "trainee",
    "graduate",
    "registered",
}
FUNCTIONS = {"engineer", "engineering", "developer", "development"}
ALIASES = {
    "ai": ["ai", "artificial intelligence", "rag", "inference"],
    "ml": ["ml", "machine learning"],
    "llm": ["llm", "llms", "large language model", "large language models", "rag"],
    "nurse": ["nurse", "nursing"],
    "accountant": ["accountant", "accounting"],
}


def title_has(title: str, word: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", title, re.IGNORECASE))


def role_matches(job: dict, target_roles: list[str]) -> bool:
    if not target_roles:
        return False
    title = job.get("title", "")
    for role in target_roles:
        role = re.sub(r"\bmachine learning\b", "ml", role.casefold())
        role = re.sub(r"\bartificial intelligence\b", "ai", role)
        role = re.sub(r"\blarge language models?\b", "llm", role)
        words = re.findall(r"[\w+#]+", role)
        specific = [word for word in words if word not in MODIFIERS | FUNCTIONS | {"of", "and", "for"}]
        function_ok = not FUNCTIONS.intersection(words) or any(title_has(title, word) for word in FUNCTIONS)
        if (
            function_ok
            and specific
            and all(any(title_has(title, alias) for alias in ALIASES.get(word, [word])) for word in specific)
        ):
            return True
        if function_ok and not specific and FUNCTIONS.intersection(words):
            return True
    return False


def recommendable(job: dict) -> bool:
    return (
        not job.get("blockers")
        and job.get("recommendation") != "skip"
        and job.get("role_relevant", True)
        and (job.get("application_status") or {}).get("state") not in {"applied_by_user", "dismissed"}
    )


def automation_domain_uncertain(job: dict, target_roles: list[str]) -> bool:
    title = job.get("title", "")
    if not title_has(title, "automation") or not re.search(r"\b(?:software|qa|test|testing|data|ai|devops|network|infrastructure|python|developer)\b", title, re.I):
        return False
    relevant = [role for role in target_roles if title_has(role, "automation")]
    return bool(relevant) and not any(re.search(r"\b(?:software|qa|test|testing|data|ai|devops|network|infrastructure|python|developer)\b", role, re.I) for role in relevant)
