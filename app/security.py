"""Loopback session, CSRF and exact-action authorization."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update

from app.db import Action, Connection
from app.errors import AppError
from app.integrations.gmail import COMPOSE_SCOPE

OWNER = "local-owner"


def persistent_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(48))
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError:
        pass
    return path.read_text(encoding="utf-8").strip()


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def approve(store, action_id: str, owner: str, digest: str, version: int, decision: str):
    action = store.owned(Action, action_id, owner)
    if action.state not in {"proposed", "approved", "failed"}:
        raise AppError("action_not_reviewable", "This action is not available for review.", 409)
    if digest != action.payload_hash or version != action.version:
        raise AppError("stale_review", "Draft changed. Review the current content.", 409)
    profile = store.latest_profile(owner)
    if not profile or action.profile_id != profile.id:
        raise AppError("profile_changed", "Profile changed. Generate a new draft.", 409)
    with store.session.begin() as session:
        result = session.execute(
            update(Action)
            .where(
                Action.id == action_id,
                Action.owner == owner,
                Action.version == version,
                Action.payload_hash == digest,
                Action.state.in_(["proposed", "approved", "failed"]),
            )
            .values(
                state="approved" if decision == "approve" else "rejected",
                expires=(datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                result={},
            )
        )
        if not result.rowcount:
            raise AppError("stale_review", "Action changed during review.", 409)
    store.activity(owner, "Action " + decision, action_id)


def claim(store, action_id: str, owner: str, fixture: bool = False):
    action = store.owned(Action, action_id, owner)
    if action.state in {"executing", "succeeded", "unknown"}:
        return action, False
    if action.state != "approved":
        raise AppError("approval_required", "Review and approve this exact draft first.", 403)
    profile = store.latest_profile(owner)
    if not profile or profile.id != action.profile_id:
        raise AppError("profile_changed", "Profile changed. Generate a new draft.", 409)
    if payload_hash(action.payload) != action.payload_hash:
        raise AppError("payload_changed", "Approved content no longer matches.", 409)
    if not action.expires or action.expires <= datetime.now(UTC).isoformat():
        raise AppError(
            "approval_expired", "Approval expired. Review and approve the current draft again.", 409
        )
    with store.session.begin() as session:
        connection = session.get(Connection, owner)
        if not fixture and (not connection or connection.account != action.payload["account"]):
            raise AppError("account_changed", "Gmail account changed or disconnected.", 409)
        if not fixture and COMPOSE_SCOPE not in connection.scopes:
            raise AppError("compose_required", "Enable Gmail drafts before creating a Gmail draft.", 403)
        result = session.execute(
            update(Action)
            .where(
                Action.id == action_id,
                Action.owner == owner,
                Action.state == "approved",
                Action.payload_hash == action.payload_hash,
                Action.expires > datetime.now(UTC).isoformat(),
            )
            .values(state="executing")
        )
        if not result.rowcount:
            raise AppError("approval_expired", "Approval expired or was consumed. Review again.", 409)
    return store.owned(Action, action_id, owner), True
