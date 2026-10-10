import json
from email.utils import getaddresses

from sqlalchemy import select, update

from app.db import Action, Connection, Scan
from app.errors import AppError
from app.integrations.gmail import safe_address
from app.integrations.llm import LLMClient
from app.schemas import SourceDraftPlan
from app.security import payload_hash
from app.services.consent import require_gmail_processing, require_model_processing
from app.services.gmail_state import require_gmail_account
from app.services.matching import current_match
from app.services.roles import role_matches
from app.services.skills import canonical


async def generate(app, owner: str, request):
    store, settings = app.state.store, app.state.settings
    scan = store.owned(Scan, request.scan_id, owner)
    profile = store.latest_profile(owner)
    if (
        not profile
        or profile.id != scan.profile_id
        or scan.state not in {"succeeded", "partial", "interrupted"}
    ):
        raise AppError("scan_stale", "Use a finished scan from your current profile.", 409)
    if scan.mode == "live" and request.source_kind == "email":
        if settings.mode != "live":
            raise AppError(
                "fixture_isolation", "Real Gmail replies are unavailable in the synthetic demo.", 403
            )
        require_gmail_account(store, owner, scan.result.get("gmail_account"))
        if scan.result.get("gmail_invalidated"):
            raise AppError("scan_stale", "Run a fresh Gmail scan before preparing a reply.", 409)
    group = scan.result.get("jobs" if request.source_kind == "job" else "emails", [])
    source = next((item for item in group if item["id"] == request.source_id), None)
    if not source:
        raise AppError("source_missing", "Evidence source not found.", 404)
    if request.source_kind == "job":
        if not role_matches(source, profile.data.get("target_roles", [])):
            raise AppError(
                "role_changed",
                "This source does not match your target roles. Review your profile or run a fresh scan.",
                409,
            )
        source = current_match(source, profile.data)
    with store.session() as session:
        pending = session.scalars(
            select(Action).where(Action.owner == owner, Action.state.in_(["executing", "unknown"]))
        ).all()
    if any(action.payload.get("source_id") == source["id"] for action in pending):
        raise AppError(
            "pending_execution",
            "Open Draft history and reconcile the previous write before creating another draft.",
            409,
        )
    if request.source_kind == "job":
        contact_source = request.contact_source.strip()
        if not request.recipient or not request.contact_confirmed or not contact_source:
            raise AppError(
                "contact_required", "Enter a public or user-confirmed recruitment contact and its provenance."
            )
        recipient = safe_address(request.recipient)
        subject = "Interest in " + source["title"]
        relevant = source["matches"][:4]
        body = (
            f"Hello,\n\nI am {profile.data['name'] or 'a candidate'} and am interested in the "
            f"{source['title']} role at {source['company']}. "
            f"My confirmed CV lists {', '.join(relevant) if relevant else 'the background in my reviewed profile'}. "
            "Would you be able to share the appropriate application process?\n\nThank you."
        )
        thread = {}
    else:
        if source.get("automated"):
            raise AppError(
                "automated_sender", "This is automated mail; inspect its evidence instead of replying."
            )
        addresses = getaddresses([source.get("reply_to") or source["sender"]])
        recipient = safe_address(addresses[0][1] if addresses else "")
        subject = source["subject"]
        body = (
            f"Hello,\n\nThank you for your message about {source['subject']}. "
            "I would appreciate details about the next steps.\n\n"
            "Best regards"
        )
        thread = {
            "thread_id": source["thread_id"],
            "latest_message_id": source["latest_message_id"],
            "reply_message_id": source["reply_message_id"],
        }
    preparation_method="synthetic_template" if scan.mode=="fixture" else "local_template"
    if scan.mode == "live" and settings.model_ready:
        if not scan.result.get("processing_consent") or not request.processing_consent or getattr(app.state,"processing_revoked",False):
            raise AppError(
                "processing_consent_required",
                "External processing consent is required for personalized model drafting.",
                403,
            )
        if request.source_kind == "email":
            require_gmail_processing(store, owner, scan.result.get("gmail_account"))
        def check_permission():
            require_model_processing(app)
            if request.source_kind == "email":
                require_gmail_processing(store, owner, scan.result.get("gmail_account"))

        facts={f"{kind}:{index}":fact for kind in ["summary","experience","education","projects","achievements","activities"] for index,fact in enumerate(profile.data.get(kind,[]))} if request.source_kind == "job" else {}
        skills={canonical(skill["name"]):skill["name"] for skill in profile.data.get("skills",[])} if request.source_kind == "job" else {}
        plan = await LLMClient(settings, before_request=check_permission).structured(
            SourceDraftPlan,
            "Choose at most two relevant CV fact IDs and four declared skill names from the supplied lists. "
            "Return selections only, no rewritten claims. Skill depth and qualification completion are unverified. "
            "For mailbox replies, leave both lists empty: the mailbox owner has not been confirmed to be this CV candidate. "
            "Choose an appropriate neutral request goal; no commitments, availability, referrals or attachments.",
            json.dumps({"profile":{},"profile_facts":facts,"declared_skills":list(skills.values()),"source":{"title":source.get("title"),"company":source.get("company"),"summary":source.get("summary"),"evidence":source.get("evidence"),"description":source.get("description","")[:6000]},"sender_identity":"The mailbox owner has not been confirmed to be this CV candidate."},ensure_ascii=False),
        )
        if any(identifier not in facts for identifier in plan.profile_fact_ids) or any(canonical(name) not in skills for name in plan.skill_names):
            raise AppError("unsupported_draft_fact", "Model selected unsupported draft facts. Prepare a new draft or review source evidence.", 502)
        selected=[facts[identifier] for identifier in dict.fromkeys(plan.profile_fact_ids)]
        selected_skills=[skills[canonical(name)] for name in plan.skill_names]
        goal={"application_process":"Would you share the appropriate application process?","next_steps":"Would you share the next steps?","clarify_requirements":"Would you clarify the requirements and next steps?"}[plan.request_goal]
        if request.source_kind == "job":
            body=f"Hello,\n\nI am {profile.data['name'] or 'a candidate'} and am interested in the {source['title']} role at {source['company']}."
            if selected:
                body += "\n\nMy reviewed CV records these declared facts; qualifications and proficiency remain subject to verification:\n\n" + "\n\n".join(selected)
            if selected_skills:
                body += "\n\nMy CV lists these skills (declared; required depth unverified): " + ", ".join(dict.fromkeys(selected_skills)) + "."
            body += "\n\n" + goal + "\n\nThank you."
        else:
            body="Hello,\n\nThank you for your message. " + goal + "\n\nBest regards"
        if len(body)>6000:
            raise AppError("draft_too_large", "Selected evidence exceeds the draft budget; choose shorter excerpts.",413)
        preparation_method="configured_model_evidence_selection"
    with store.session() as session:
        connection = session.get(Connection, owner)
    if scan.mode == "live" and not connection:
        account = ""
    else:
        account = connection.account if connection and scan.mode == "live" else "fixture@example.invalid"
    if scan.mode == "live" and request.source_kind == "job":
        if request.sender_identity_confirmed:
            if not account or request.sender_account != account:
                raise AppError("sender_unconfirmed", "Confirm the current Gmail sender account before preparing bound outreach.", 409)
        else:
            account = ""
    payload = {
        "preparation_method":preparation_method,
        "type": "create_gmail_draft",
        "mode": "synthetic" if scan.mode == "fixture" else "live",
        "account": account,
        "to": recipient,
        "subject": subject[:250],
        "body": body,
        "contact_source": contact_source
        if request.source_kind == "job"
        else "Gmail Reply-To header; review recruitment context."
        if source.get("reply_to")
        else "Gmail sender header; review recruitment context.",
        "source_id": source["id"],
        "source_kind": request.source_kind,
        **thread,
    }
    with store.session.begin() as session:
        latest = store.latest_profile(owner)
        if not latest or latest.id != profile.id:
            raise AppError("profile_changed", "Profile changed while drafting. Prepare a fresh draft.", 409)
        if scan.mode == "live" and request.source_kind == "job" and account:
            current = session.get(Connection, owner)
            if not current or current.account != account:
                raise AppError("email_account_changed", "Gmail changed while drafting. Confirm the current sender.", 409)
        if scan.mode == "live" and request.source_kind == "email":
            current = session.get(Connection, owner)
            if not current or current.account != scan.result.get("gmail_account"):
                raise AppError(
                    "email_account_changed", "Gmail changed while drafting. Run a fresh scan.", 409
                )
        action = Action(
            owner=owner,
            profile_id=profile.id,
            scan_id=scan.id,
            payload=payload,
            payload_hash=payload_hash(payload),
        )
        session.add(action)
        session.flush()
        action_id = action.id
    store.activity(owner, "Local draft prepared", action_id)
    return store.owned(Action, action_id, owner)


def edit(store, owner, action_id, value):
    action = store.owned(Action, action_id, owner)
    if action.state not in {"proposed", "approved", "failed"}:
        raise AppError("action_locked", "This draft cannot be edited after execution or rejection.", 409)
    payload = {**action.payload, "to": safe_address(value.to), "subject": value.subject, "body": value.body}
    if "\n" in value.subject or "\r" in value.subject:
        raise AppError("invalid_subject", "Subject cannot contain line breaks.")
    with store.session.begin() as session:
        changed = session.execute(
            update(Action)
            .where(
                Action.id == action_id,
                Action.owner == owner,
                Action.version == action.version,
                Action.state.in_(["proposed", "approved", "failed"]),
            )
            .values(
                payload=payload,
                payload_hash=payload_hash(payload),
                version=action.version + 1,
                state="proposed",
                expires=None,
                result={},
            )
        )
        if not changed.rowcount:
            raise AppError("action_changed", "Action changed while editing.", 409)
    return store.owned(Action, action_id, owner)
