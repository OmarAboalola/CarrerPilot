"""Local encrypted credentials; connection tests use synthetic data only."""

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.db import Connection, Scan
from app.errors import AppError
from app.integrations.llm import LLMClient
from app.schemas import StrictModel

GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-120b"


class ModelSetup(StrictModel):
    api_key: SecretStr
    model: Literal["openai/gpt-oss-120b", "openai/gpt-oss-20b"] = GROQ_MODEL


class ModelCheck(StrictModel):
    status: Literal["ok"]


def apply_values(settings, payload):
    settings._model_version = str(payload.get("version", "saved"))
    if payload.get("enabled"):
        value = ModelSetup.model_validate(payload["config"])
        settings.llm_base_url = GROQ_URL
        settings.llm_model = value.model
        settings.llm_api_key = value.api_key
    else:
        settings.llm_api_key = SecretStr("")
        settings.llm_model = ""
    settings._model_cooldown_until = 0


def load_saved_model(settings, tokens):
    if settings.mode != "live":
        return ""
    path = settings.data_dir / "model-settings.enc"
    if not path.exists():
        return ""
    try:
        payload = json.loads(tokens.decrypt(path.read_text(encoding="utf-8")))
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            raise ValueError("Invalid saved configuration")
        apply_values(settings, payload)
        return ""
    except (AppError, OSError, ValueError, TypeError, KeyError, ValidationError):
        settings.llm_api_key = SecretStr("")
        settings.llm_model = ""
        return "Saved model settings could not be loaded. Re-enter your key in Model setup."


def persist_model(settings, tokens, payload):
    encrypted = tokens.encrypt(json.dumps(payload))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=settings.data_dir,
            prefix="model-settings-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, settings.data_dir / "model-settings.enc")
    except OSError:
        raise AppError(
            "model_save_failed",
            "Could not save model settings locally. Previous configuration is retained.",
            500,
        ) from None
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def require_idle(app):
    if any(not task.done() for task in app.state.tasks.values()):
        raise AppError(
            "model_setup_busy", "Finish or cancel the current scan before changing the model.", 409
        )


def reset_processing_approvals(app, owner):
    with app.state.store.session.begin() as db:
        connection = db.get(Connection, owner)
        if connection:
            connection.consent = False
        for scan in db.scalars(select(Scan).where(Scan.owner == owner, Scan.mode == "live")):
            value = deepcopy(scan.result)
            value["processing_consent"] = False
            scan.result = value


async def configure_model(app, owner, raw):
    require_idle(app)
    try:
        value = ModelSetup.model_validate(raw)
        key = value.api_key.get_secret_value().strip()
        if not 8 <= len(key) <= 512 or any(char.isspace() for char in key):
            raise ValueError("Invalid key")
    except (ValidationError, ValueError, TypeError):
        raise AppError(
            "invalid_model_settings",
            "Enter a Groq API key and choose a supported model. The key is never returned.",
        ) from None
    candidate = app.state.settings.model_copy(
        update={"llm_base_url": GROQ_URL, "llm_model": value.model, "llm_api_key": SecretStr(key)}
    )
    candidate._model_cooldown_until = 0
    await LLMClient(candidate).structured(
        ModelCheck,
        "Return status ok to confirm the connection.",
        "Synthetic connection check. No CV, profile or Gmail data is included.",
    )
    require_idle(app)
    payload = {"enabled": True, "version": uuid4().hex, "config": {"api_key": key, "model": value.model}}
    # Clear processing approvals before publishing the changed provider configuration.
    reset_processing_approvals(app, owner)
    persist_model(app.state.settings, app.state.tokens, payload)
    apply_values(app.state.settings, payload)
    app.state.model_setup_error = ""
    app.state.store.activity(owner, "Model connection verified and configured")


def model_status(app):
    settings = app.state.settings
    return {
        "configured": settings.model_ready,
        "model": settings.llm_model if settings.model_ready else "",
        "provider": urlsplit(settings.llm_base_url).hostname if settings.model_ready else "",
        "recommended_model": GROQ_MODEL,
        "version": settings._model_version,
        "setup_error": app.state.model_setup_error,
    }
