"""Whitespace-equivalent quotations resolve back to literal original evidence."""
import re


def literal_excerpt(source, quote):
    if quote in source:
        return quote
    words = quote.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    match = re.search(pattern,source)
    return match[0] if match else None
