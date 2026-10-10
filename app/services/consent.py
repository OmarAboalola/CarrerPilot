from app.db import Connection
from app.errors import AppError


def require_model_processing(app):
    if getattr(app.state, "processing_revoked", False):
        raise AppError("processing_consent_required", "Model-processing approval was revoked.", 403)


def require_gmail_processing(store, owner: str, account: str | None):
    """Re-read permission immediately before disclosing Gmail evidence."""
    with store.session() as session:
        connection = session.get(Connection, owner)
        if not connection or not connection.consent:
            raise AppError(
                "email_processing_consent_required",
                "Enable email-processing consent for the connected Gmail account first.",
                403,
            )
        if not account or connection.account != account:
            raise AppError(
                "email_account_changed",
                "Run a new scan for the connected Gmail account before processing cached emails.",
                409,
            )
