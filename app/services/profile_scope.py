"""Keep candidate tracking separate while retaining earlier local data."""
import hashlib


def tracking_prefix(owner, profile):
    source=profile.data.get("source_text","") if profile else ""
    if not source:
        return owner+":"
    fingerprint=hashlib.sha256(" ".join(source.split()).encode()).hexdigest()[:32]
    return owner+":cv:"+fingerprint+":"
