"""Account binding for cached Gmail, independent of model processing consent."""

import re
from copy import deepcopy

from sqlalchemy import select

from app.db import Connection, Scan
from app.errors import AppError
from app.integrations.gmail import READ_SCOPE


def redact_gmail_errors(data):
    """Keep errors with non-mail provenance; fail closed for unknown legacy details."""
    safe = [item for item in data.get("error_sources", []) if item.get("source") in {"jobs", "model", "scan"}]
    sourced = {item.get("message") for item in data.get("error_sources", [])}
    legacy = [
        message
        for message in data.get("errors", [])
        if message not in sourced
        and (
            re.fullmatch(r"Greenhouse board [a-zA-Z0-9_-]{1,80}: unavailable or invalid response\.", message)
            or message
            in {
                "Model analysis failed for a job; labeled keyword fallback used.",
                "Scan interrupted or exceeded its time budget; partial evidence retained.",
                "Scan failed; available partial results retained.",
            }
        )
    ]
    data["errors"] = list(dict.fromkeys([item["message"] for item in safe] + legacy))
    data["error_sources"] = safe


def require_gmail_account(store, owner, account):
    with store.session() as db:
        connection = db.get(Connection, owner)
    if not connection or not account or connection.account != account:
        raise AppError(
            "email_account_changed",
            "Gmail changed or disconnected. Run a fresh scan of the connected account.",
            409,
        )
    if READ_SCOPE not in connection.scopes:
        raise AppError("gmail_read_scope_missing", "Reconnect Gmail with read access.", 403)
    return connection


def invalidate_gmail_scans(db, owner, state):
    """Keep job evidence, but remove cached mail when its authorization changes."""
    for scan in db.scalars(select(Scan).where(Scan.owner == owner, Scan.mode == "live")):
        data = deepcopy(scan.result)
        data["emails"] = []
        data["actions"] = [a for a in data.get("actions", []) if a.get("source_kind") != "email"]
        data["coverage"] = {**data.get("coverage", {}), "gmail": {"state": state}}
        data["gmail_invalidated"] = True
        data.pop("gmail_account", None)
        redact_gmail_errors(data)
        scan.result = data


def visible_scan_data(store, owner, scan):
    data = deepcopy(scan.result)
    if scan.mode != "live":
        return data
    with store.session() as db:
        connection = db.get(Connection, owner)
    account = data.get("gmail_account")
    if (
        data.get("gmail_invalidated")
        or not connection
        or (account and account != connection.account)
        or (data.get("emails") and not account)
    ):
        data["emails"] = []
        data["actions"] = [a for a in data.get("actions", []) if a.get("source_kind") != "email"]
        data["gmail_account"] = None
        redact_gmail_errors(data)
        state = "disconnected" if not connection else "fresh_scan_required"
        data["coverage"] = {**data.get("coverage", {}), "gmail": {"state": state}}
    return data


def action_account_available(store, owner, action):
    if action.payload.get("mode") != "live":
        return True
    account = action.payload.get("account")
    if not account and action.payload.get("source_kind") != "email":
        return True
    with store.session() as db:
        connection = db.get(Connection, owner)
        if not account:
            scan = db.get(Scan, action.scan_id)
            account = scan.result.get("gmail_account") if scan and scan.owner == owner else None
    return bool(account and connection and connection.account == account)
