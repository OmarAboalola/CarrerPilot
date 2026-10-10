"""Official Gmail client boundary; read and draft only, never send."""

import base64
import json
import re
from email.message import EmailMessage
from email.utils import getaddresses
from urllib.parse import quote

import google_auth_httplib2
import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import Settings
from app.errors import AppError, DraftOutcomeUnknown

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


def oauth_flow(settings: Settings, scopes: list[str], **kwargs) -> Flow:
    if not settings.gmail_ready:
        raise AppError("gmail_not_configured", "Configure Google client credentials locally first.", 503)
    config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret.get_secret_value(),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        config, scopes=scopes, redirect_uri=settings.base_url + "/api/gmail/callback", **kwargs
    )


def gmail_service(token_json: str):
    credentials = Credentials.from_authorized_user_info(json.loads(token_json))
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception:
            raise AppError(
                "gmail_reconnect", "Gmail access expired or was revoked. Reconnect your account.", 401
            ) from None
    transport = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=20))
    return build("gmail", "v1", http=transport, cache_discovery=False), credentials


def account_and_threads(token_json: str, limit: int) -> tuple[str, list[dict], str]:
    service, credentials = gmail_service(token_json)
    try:
        account = service.users().getProfile(userId="me").execute()["emailAddress"]
        result = (
            service.users()
            .threads()
            .list(
                userId="me",
                q="newer_than:30d -in:trash -in:spam {interview recruiter recruitment assessment application hiring opportunity}",
                maxResults=limit,
            )
            .execute()
        )
        threads = []
        for item in result.get("threads", [])[:limit]:
            try:
                threads.append(service.users().threads().get(userId="me", id=item["id"], format="full").execute())
            except HttpError:
                threads.append({"id": item["id"], "messages": [], "read_error": True})
        return account, threads, credentials.to_json()
    except HttpError:
        raise AppError(
            "gmail_read_failed", "Could not read Gmail. Check access and try reconnecting.", 502
        ) from None


def conversation_url(account: str, message_id: str = "") -> tuple[str, str]:
    account = safe_address(account)
    base = "https://mail.google.com/mail/u/?authuser=" + quote(account, safe="")
    identifier = message_id.strip().removeprefix("<").removesuffix(">")
    if re.fullmatch(r"[A-Za-z0-9.!#$%&'*/+=?^_`{|}~-]{1,250}@[A-Za-z0-9.-]{1,250}", identifier):
        return base + "#search/" + quote("rfc822msgid:" + identifier, safe=""), "message_search"
    return base + "#inbox", "account_inbox"


def safe_address(address: str) -> str:
    import re

    if "\r" in address or "\n" in address or len(address) > 254:
        raise AppError("invalid_recipient", "Recipient is invalid.")
    parsed = getaddresses([address])
    if len(parsed) != 1 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", parsed[0][1]):
        raise AppError("invalid_recipient", "Use one valid recipient address.")
    return parsed[0][1]


def create_verified_draft(token_json: str, payload: dict) -> tuple[dict, str]:
    service, credentials = gmail_service(token_json)
    if COMPOSE_SCOPE not in (credentials.scopes or []):
        raise AppError(
            "compose_required", "Reconnect with compose capability before creating a Gmail draft.", 403
        )
    account = service.users().getProfile(userId="me").execute()["emailAddress"]
    if account != payload["account"]:
        raise AppError(
            "account_changed", "The connected Gmail account changed. Review the action again.", 409
        )
    if payload.get("thread_id"):
        thread = (
            service.users().threads().get(userId="me", id=payload["thread_id"], format="metadata").execute()
        )
        messages = thread.get("messages") or []
        latest = max(enumerate(messages), key=lambda item: (int(item[1].get("internalDate", "0")), item[0]))[1].get("id") if messages else None
        if latest != payload.get("latest_message_id"):
            raise AppError(
                "thread_changed", "A new thread message arrived. Generate and review a new draft.", 409
            )
    message = EmailMessage()
    message["To"] = safe_address(payload["to"])
    message["From"] = account
    message["Message-ID"] = f"<careerpilot-{payload['action_id']}@local.invalid>"
    if "\r" in payload["subject"] or "\n" in payload["subject"]:
        raise AppError("invalid_subject", "Subject cannot contain line breaks.")
    message["Subject"] = payload["subject"]
    if payload.get("reply_message_id"):
        reply_id = payload["reply_message_id"]
        if "\r" in reply_id or "\n" in reply_id:
            raise AppError("invalid_thread_header", "Invalid reply header.")
        message["In-Reply-To"] = reply_id
        message["References"] = reply_id
    message.set_content(payload["body"])
    body = {"message": {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}}
    if payload.get("thread_id"):
        body["message"]["threadId"] = payload["thread_id"]
    # Caller has already durably claimed execution. Never retry this mutation.
    created = None
    try:
        created = service.users().drafts().create(userId="me", body=body).execute(num_retries=0)
        verified = service.users().drafts().get(userId="me", id=created["id"], format="full").execute()
        if not draft_matches(verified, payload):
            raise ValueError("Provider draft did not match approved content")
    except Exception:
        raise DraftOutcomeUnknown(created["id"] if created else None) from None
    return {
        "draft_id": verified["id"],
        "message_id": verified["message"]["id"],
        "account": account,
        "verified": True,
    }, credentials.to_json()


def draft_matches(draft: dict, payload: dict) -> bool:
    from app.services.inbox import decode_body

    message = draft["message"]
    headers = {h["name"].casefold(): h["value"] for h in message.get("payload", {}).get("headers", [])}
    recipients = [address for _, address in getaddresses([headers.get("to", "")])]
    return (
        headers.get("message-id") == f"<careerpilot-{payload['action_id']}@local.invalid>"
        and recipients == [payload["to"]]
        and not headers.get("cc")
        and not headers.get("bcc")
        and headers.get("subject") == payload["subject"]
        and (not payload.get("account") or [a.casefold() for _, a in getaddresses([headers.get("from", "")])] == [payload["account"].casefold()])
        and (not payload.get("thread_id") or message.get("threadId") == payload["thread_id"])
        and decode_body(message.get("payload", {}), strip_quotes=False).replace("\r\n", "\n").rstrip("\n") == payload["body"].replace("\r\n", "\n").rstrip("\n")
    )


def reconcile_draft(token_json: str, payload: dict, draft_id: str | None):
    service, credentials = gmail_service(token_json)
    account = service.users().getProfile(userId="me").execute()["emailAddress"]
    if account != payload["account"]:
        raise AppError("account_changed", "Reconnect the account that executed this draft.", 409)
    candidates = (
        [{"id": draft_id}]
        if draft_id
        else service.users().drafts().list(userId="me", maxResults=30).execute().get("drafts", [])
    )
    matches = []
    for item in candidates:
        value = service.users().drafts().get(userId="me", id=item["id"], format="full").execute()
        if draft_matches(value, payload):
            matches.append(value)
    if len(matches) != 1:
        raise AppError(
            "draft_not_reconciled",
            "Could not identify one exact matching draft. Inspect Gmail manually; no retry is permitted.",
            409,
        )
    value = matches[0]
    return {
        "draft_id": value["id"],
        "message_id": value["message"]["id"],
        "account": account,
        "verified": True,
    }, credentials.to_json()
