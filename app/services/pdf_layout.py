import re
from io import BytesIO

import pdfplumber


def date_only_column(words):
    value=" ".join(word["text"] for word in words)
    tokens=re.findall(r"[a-z]+",value.casefold())
    dates={"january","february","march","april","may","june","july","august","september","october","november","december","jan","feb","mar","apr","jun","jul","aug","sep","sept","oct","nov","dec","spring","summer","autumn","fall","winter","present","current","ongoing","xx","to"}
    return bool(re.search(r"\b(?:19|20)(?:\d{2}|XX)\b",value,re.I)) and all(token in dates for token in tokens)


def column_text(words):
    intervals = sorted((w["x0"], w["x1"]) for w in words)
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps = [(a[1] + b[0]) / 2 for a, b in zip(merged, merged[1:]) if b[0] - a[1] > 12]
    if gaps:
        cut = max(gaps, key=lambda x: sum(w["x1"] < x for w in words) * sum(w["x0"] > x for w in words))
        left = [w for w in words if w["x1"] < cut]
        right = [w for w in words if w["x0"] > cut]
        if left and right and (len(left) > 1 or left[0]["text"].isupper()) and len(right) > 1 and not date_only_column(right):
            return column_text(left) + "\n" + column_text(right)
    lines = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        row = next((r for r in lines if abs(r[0]["top"] - word["top"]) < 3), None)
        if row is None:
            lines.append([word])
        else:
            row.append(word)
    return "\n".join(" ".join(w["text"] for w in sorted(row, key=lambda w: w["x0"])) for row in lines)


def page_text(page):
    words = page.extract_words(x_tolerance=2, y_tolerance=3)
    breaks = sorted(
        set(
            [0, page.height]
            + [
                e["top"]
                for e in page.edges
                if abs(e["bottom"] - e["top"]) < 2 and e["x1"] - e["x0"] > page.width * 0.55
            ]
        )
    )
    return "\n\n".join(
        filter(
            None,
            (
                column_text([w for w in words if top <= (w["top"] + w["bottom"]) / 2 < bottom])
                for top, bottom in zip(breaks, breaks[1:])
            ),
        )
    ).strip()


def layout_pages(raw):
    """Read visual bands and persistent column gutters, retaining every text word."""
    with pdfplumber.open(BytesIO(raw)) as pdf:
        return [page_text(page) for page in pdf.pages]
