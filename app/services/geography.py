"""Explicit geographic constraints; never infer citizenship or work authorization.

countries.json contains English region names/ISO codes from Windows/.NET RegionInfo.
Ambiguous city names and two-letter abbreviations are deliberately left unknown.
"""

import json
import re
from pathlib import Path

COUNTRIES = json.loads(Path(__file__).with_name("countries.json").read_text(encoding="utf-8-sig"))
NAMES = {row["name"].casefold(): row["name"] for row in COUNTRIES}
NAMES.update(
    {
        "usa": "United States",
        "u.s.a.": "United States",
        "us": "United States",
        "u.s.": "United States",
        "united states of america": "United States",
        "uk": "United Kingdom",
        "u.k.": "United Kingdom",
        "great britain": "United Kingdom",
        "uae": "United Arab Emirates",
        "u.a.e.": "United Arab Emirates",
        "turkey": "Türkiye",
        "south korea": "South Korea",
        "republic of korea": "South Korea",
    }
)
COUNTRY_PATTERN = "(?:" + "|".join(re.escape(n) for n in sorted(NAMES, key=len, reverse=True)) + ")"


def countries_in(text: str) -> set[str]:
    return {
        NAMES[m[0].casefold()]
        for m in re.finditer(r"(?<!\w)" + COUNTRY_PATTERN + r"(?!\w)", text, re.IGNORECASE)
        if m[0].casefold() != "us" or m[0] == "US"
    }


def explicit_restrictions(text: str) -> tuple[list[str], str]:
    areas, evidence = set(), []
    patterns = [
        r"\b(?:must (?:be )?(?:located|based|reside|residing|live|living)|"
        r"(?:candidates|applicants) must (?:be )?(?:located|based|reside|live))\s+in\s+(?:the\s+)?[^;\n]+",
        r"\b(?:restricted|limited) to\s+(?:the\s+)?[^;\n]+",
        r"\bonly (?:in|within|for residents of)\s+(?:the\s+)?[^;\n]+",
        r"(?<!\w)"
        + COUNTRY_PATTERN
        + r"(?:\s*(?:,|/|\band\b|\bor\b)\s*"
        + COUNTRY_PATTERN
        + r")*\s+(?:residents?\s+)?only\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            # Stop at a sentence boundary without breaking country abbreviations.
            span = re.split(r"\.\s+(?=[A-Z])", match[0], maxsplit=1)[0]
            prefix = text[max(0, match.start() - 12) : match.start()].casefold()
            if re.search(r"\b(?:not|isn't|aren't)\s*$", prefix):
                continue
            found = countries_in(span)
            if found:
                areas.update(found)
                evidence.append(span)
    return sorted(areas), "\n".join(dict.fromkeys(evidence))


def location_checks(
    location: str, restrictions: list[str], targets: list[str]
) -> tuple[list[str], list[str]]:
    blockers, unknowns = [], []
    wanted = set().union(*(countries_in(target) for target in targets))
    allowed = set().union(*(countries_in(country) for country in restrictions))
    unresolved = [target for target in targets if not countries_in(target)]
    if restrictions:
        if wanted and allowed and not wanted.intersection(allowed) and not unresolved:
            blockers.append(
                "Explicit country restriction does not include your target location(s): "
                + ", ".join(targets)
                + "."
            )
        elif unresolved or not allowed:
            unknowns.append(
                "Verify the explicit country restriction against your target locations; country mapping is incomplete."
            )
    elif "remote" in location.casefold():
        unknowns.append("Remote geographic eligibility is not confirmed.")
    elif targets and not (
        wanted.intersection(countries_in(location))
        or any(
            re.search(r"(?<!\w)" + re.escape(target.strip()) + r"(?!\w)", location, re.IGNORECASE)
            for target in targets
        )
    ):
        unknowns.append("Location does not confirm your target geography.")
    return blockers, unknowns
