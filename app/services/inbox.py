"""Thread evidence with outgoing messages retained; no mailbox writes."""

import base64
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import getaddresses

from app.errors import AppError
from app.integrations.jobs import TextParser, plain_text
from app.integrations.llm import LLMClient
from app.schemas import EmailAnalysis
from app.services.evidence import literal_excerpt


class MailTextParser(TextParser):
    def __init__(self):
        super().__init__()
        self.quote_stack = []

    def handle_starttag(self,tag,attrs):
        quoted = any(flag for _,flag in self.quote_stack) or tag=="blockquote" or any(name=="class" and "gmail_quote" in (value or "").split() for name,value in attrs)
        if tag not in {"br","img","hr","meta","link","input"}:
            self.quote_stack.append((tag,quoted))
        if not quoted:
            super().handle_starttag(tag,attrs)

    def handle_endtag(self,tag):
        quoted=any(flag for _,flag in self.quote_stack)
        index=next((i for i in range(len(self.quote_stack)-1,-1,-1) if self.quote_stack[i][0]==tag),None)
        if index is not None:
            self.quote_stack=self.quote_stack[:index]
        if not quoted:
            super().handle_endtag(tag)

    def handle_data(self,data):
        if not any(flag for _,flag in self.quote_stack):
            super().handle_data(data)


def decode_body(payload: dict, *, strip_quotes: bool = True) -> str:
    parts = payload.get("parts", [])
    if parts:
        text = [decode_body(part, strip_quotes=strip_quotes) for part in parts if part.get("mimeType") == "text/plain"]
        if not text:
            text = [decode_body(part, strip_quotes=strip_quotes) for part in parts if not part.get("filename")]
        return "\n".join(text)[:20000]
    raw = payload.get("body", {}).get("data", "")
    try:
        value = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", errors="replace")
        if payload.get("mimeType") == "text/html":
            if strip_quotes:
                parser=MailTextParser()
                parser.feed(value)
                value="\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())
            else:
                value = plain_text(value)
        # Full thread messages provide old context; exclude plainly quoted duplicated lines.
        if strip_quotes:
            value = re.split(r"(?im)^\s*(?:On .{1,200}wrote:|[- ]*Original Message[- ]*)\s*$",value,maxsplit=1)[0]
            value = "\n".join(line for line in value.splitlines() if not line.lstrip().startswith(">"))
        return value[:12000]
    except ValueError:
        return ""


def message_data(message: dict) -> dict:
    headers = {h["name"].casefold(): h["value"] for h in message.get("payload", {}).get("headers", [])}
    body = decode_body(message.get("payload", {}))
    content = headers.get("subject", "") + "\n" + body
    digest = bool(
        re.search(
            r"\b(?:daily|weekly|monthly)\s+(?:job\s+)?(?:recommendations|digest|roundup)\b"
            r"|\b(?:recommended|matching) jobs (?:for you|based on your profile)\b"
            r"|\bhere are[^.\n]{0,70}\b(?:recent|latest|new) jobs based on your profile\b"
            r"|\bnewsletter\b[\s\S]{0,4000}\b(?:unsubscribe|manage your subscription)\b",
            content,
            re.IGNORECASE,
        )
    )
    return {
        "id": message["id"],
        "sender": headers.get("from", ""),
        "reply_to": headers.get("reply-to", ""),
        "automated": bool(
            digest
            or re.search(r"\b(?:please\s+)?do not reply\b|\breplies (?:are )?(?:undeliverable|not monitored)\b", body, re.I)
            or headers.get("list-id")
            or headers.get("list-unsubscribe")
            or headers.get("precedence", "").casefold() in {"bulk", "list", "junk"}
            or headers.get("auto-submitted", "no").casefold() != "no"
            or re.search(
                r"(?:no[._-]?reply|do[._-]?not[._-]?reply|mailer-daemon)", headers.get("from", ""), re.I
            )
            or {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}.intersection(message.get("labelIds", []))
        ),
        "subject": headers.get("subject", ""),
        "message_id": headers.get("message-id", ""),
        "body": body,
        "labels": message.get("labelIds", []),
        "timestamp": int(message.get("internalDate", "0")),
    }


def baseline_analysis(current, later_sent):
    subject = current["subject"].casefold()
    context = subject + "\n" + current["body"]
    related = bool(
        re.search(
            r"\b(interview|recruit(?:er|ment|ing)?|assessment|application|hiring|position|role|candidate|job offer)\b",
            context,
            re.I,
        )
    )
    request = re.search(
        r"\b(?:please|could you|can you|would you)\b[^.\n]{0,65}\b"
        r"(?:reply|respond|confirm|send|provide|share|schedule|complete|let (?:me|us) know)\b"
        r"|\b(?:reply|respond|confirm your availability)\s+(?:by|before|to)\b",
        current["body"],
        re.I,
    )
    if not request:
        request = re.search(r"\b(?:are|would|will)\s+you\s+(?:be\s+)?(?:available|able|interested)\b[^?\n]{0,200}\?|\b(?:does|would)\b[^?\n]{0,100}\b(?:work|suit)\s+for\s+you\b[^?\n]{0,50}\?", current["body"], re.I)
    requested = bool(request)
    automated = current["automated"]
    category = (
        "other"
        if not related
        else "rejection"
        if re.search(r"\b(not selected|not moving forward|application (?:was |is )?unsuccessful|chosen another candidate|decided to proceed with another candidate|cannot offer you the position|unable to offer (?:you )?(?:the |a )?position)\b", context, re.I)
        else "confirmation"
        if re.search(r"application (?:has been |was |is )?submitted|thank you for (?:your application|applying)",context,re.I)
        else "other"
        if automated
        else "interview"
        if "interview" in subject
        else "assessment"
        if "assessment" in subject
        else "recruiter"
    )
    requires_response = related and requested and not automated and not later_sent and category != "rejection"
    uncertainty = (
        "Automated or bulk mail; no reply is recommended."
        if automated
        else "Local keyword analysis found a direct request. Verify the context and recipient before replying."
        if requires_response
        else "No direct reply request was established. Inspect the evidence if this conversation matters."
    )
    deadline_match = re.search(r"\b(?:by|before|deadline(?: is)?|due(?: at)?|no later than)\s*:?\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2}))\b", current["body"], re.I)
    deadline = None
    if deadline_match:
        try:
            datetime.fromisoformat(deadline_match[1])
            deadline = deadline_match[1]
        except ValueError:
            pass
    if not deadline and re.search(r"\b(?:deadline|due)\b|\b(?:by|before)\s+(?:today|tomorrow|next|end of|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d)", current["body"], re.I):
        uncertainty += " Deadline date or timezone is ambiguous; inspect the original message."
    return EmailAnalysis(
        category=category,
        summary=current["body"][:250] or current["subject"],
        requires_response=requires_response,
        evidence=current["body"][request.start():].split("\n")[0][:1500] if requires_response else current["body"][:300],
        uncertainty=uncertainty,
        deadline=deadline,
        deadline_evidence=deadline_match[0] if deadline else "",
    )


def acknowledgment(body: str) -> bool:
    """Promises and receipt-only acknowledgments do not fulfil a request."""
    return bool(re.search(
        r"\b(?:I|we)(?:\s+(?:will|shall)|['’]ll)\s+(?:send|reply|respond|confirm|provide|share|complete|get back|follow up)\b"
        r"|\b(?:will|shall)\s+(?:get back|respond|reply)\b"
        r"|^\s*(?:thanks|thank you|received|got it|acknowledged)[.!\s]*$", body, re.I))


async def analyze_threads(
    threads: list[dict], account: str, settings, check_consent: Callable[[], None] | None = None
) -> list[dict]:
    result = []
    for thread in threads:
        messages = []
        malformed = bool(thread.get("read_error"))
        for message in thread.get("messages", []):
            try:
                if "DRAFT" in message.get("labelIds",[]):
                    continue
                messages.append(message_data(message))
            except (KeyError, TypeError, ValueError):
                malformed = True
        messages.sort(key=lambda m: m["timestamp"])
        if malformed:
            result.append({"id": thread["id"], "thread_id": thread["id"], "category": "other",
                "subject": "Thread unavailable for analysis", "summary": "A thread could not be fully read; inspect Gmail manually.",
                "requires_response": False, "response_state": "needs_review", "evidence": "",
                "uncertainty": "Incomplete thread; outstanding actions and deadlines are unknown.",
                "priority": 20, "mode": "live", "automated": False, "deadline": None,
                "analysis_error": True, "sender": "", "reply_to": "", "url": ""})
            continue
        incoming = [
            m
            for m in messages
            if "SENT" not in m["labels"]
            and account.casefold() not in {a.casefold() for _, a in getaddresses([m["sender"]])}
        ]
        if not incoming:
            continue
        current = incoming[-1]
        # Later acknowledgments in either direction do not erase an earlier request.
        if not baseline_analysis(current, False).requires_response and not current["automated"] and baseline_analysis(current, False).category != "rejection":
            outstanding = [m for m in incoming if baseline_analysis(m, False).requires_response]
            if outstanding:
                current = outstanding[-1]
        sent = [m for m in messages if "SENT" in m["labels"] and m["timestamp"] >= current["timestamp"]]
        later_sent = any(not acknowledgment(m["body"]) for m in sent)
        pending = bool(sent and not later_sent)
        analysis = baseline_analysis(current, later_sent)
        model_fallback = False
        model_grounded = False
        if settings.model_ready and not current["automated"]:
            if check_consent:
                check_consent()
            evidence = "\n".join(
                f"Message {m['id']} at {datetime.fromtimestamp(m['timestamp']/1000,UTC).isoformat()} ({'outgoing' if 'SENT' in m['labels'] else 'incoming'}):\nFrom: {m['sender']}\nSubject: {m['subject']}\n{m['body']}" for m in messages
            )[-20000:]
            try:
                analysis = await LLMClient(settings, before_request=check_consent).structured(
                    EmailAnalysis,
                    "Classify the complete chronological thread. requires_response means a request remains outstanding now, considering all sent replies. "
                    "Deadline must be null unless an absolute ISO timestamp/date and timezone can be supported from source. "
                    "Use exact evidence excerpts. A sent response may not resolve every request.",
                    evidence,
                )
            except AppError:
                model_fallback = True
                analysis = baseline_analysis(current, later_sent)
                analysis.uncertainty = "External model unavailable; local evidence baseline used. " + analysis.uncertainty
            if analysis.evidence:
                literal = literal_excerpt(evidence,analysis.evidence)
                if literal is not None:
                    analysis.evidence = literal
            if analysis.evidence and analysis.evidence not in evidence:
                analysis = baseline_analysis(current, later_sent)
                model_fallback = True
                analysis.uncertainty = (
                    "Model evidence did not match source; local analysis used. Verify manually."
                )
            model_grounded = not model_fallback
            if analysis.deadline:
                if not analysis.deadline_evidence or analysis.deadline_evidence not in evidence:
                    analysis.deadline = None
                    analysis.uncertainty = "Deadline lacks exact supporting evidence."
                else:
                    try:
                        parsed = datetime.fromisoformat(analysis.deadline)
                        if parsed.tzinfo is None:
                            raise ValueError("No timezone")
                        literals = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", analysis.deadline_evidence)
                        if not any(datetime.fromisoformat(value) == parsed for value in literals):
                            raise ValueError("Deadline differs from source timestamp")
                    except ValueError:
                        analysis.deadline = None
                        analysis.uncertainty = "Deadline date/timezone is ambiguous."
        resolved = later_sent and (not model_grounded or not analysis.requires_response)
        response_state = (
            "pending_response"
            if (pending or sent and model_grounded) and analysis.requires_response
            else "responded"
            if resolved
            else "unanswered"
            if analysis.requires_response
            else "informational"
            if current["automated"] or analysis.category == "rejection"
            else "needs_review"
        )
        priority = (
            100
            if analysis.category in {"interview", "assessment"}
            and analysis.requires_response
            and not resolved
            else 85
            if analysis.requires_response and not resolved
            else 20
        )
        result.append(
            {
                **analysis.model_dump(),
                "id": thread["id"],
                "thread_id": thread["id"],
                "latest_message_id": messages[-1]["id"],
                "reply_message_id": current["message_id"],
                "sender": current["sender"],
                "reply_to": current["reply_to"],
                "automated": current["automated"],
                "model_fallback": model_fallback,
                "analysis_method": "configured_model" if model_grounded else "local_evidence",
                "subject": current["subject"],
                "response_state": response_state,
                "requires_response": analysis.requires_response and not resolved,
                "priority": priority,
                "mode": "live",
                "url": "",
                "received_at": datetime.fromtimestamp(current["timestamp"] / 1000, UTC).isoformat(),
            }
        )
    return sorted(result, key=lambda item: -item["priority"])
