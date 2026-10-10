import base64
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_oauth import oauth_client as oauth_client

from app.config import Settings
from app.db import Connection, Scan
from app.errors import AppError
from app.integrations.llm import LLMClient
from app.main import create_app
from app.security import OWNER
from app.services import inbox
from app.services.model_settings import GROQ_MODEL, GROQ_URL, ModelCheck

KEY = "gsk_synthetic_test_key_never_real"


@pytest.fixture
def verified(oauth_client, monkeypatch):
    ctx = oauth_client
    ctx.model = AsyncMock(return_value=ModelCheck(status="ok"))
    monkeypatch.setattr(LLMClient, "structured", ctx.model)
    return ctx


def save(ctx, **extra):
    return ctx.client.post("/api/model/setup", json={"api_key": KEY, "model": GROQ_MODEL, **extra})


def test_setup_verifies_synthetic_data_encrypts_key_and_returns_only_metadata(verified):
    ctx = verified
    result = save(ctx)
    assert result.status_code == 200 and result.json()["configured"]
    assert result.json()["provider"] == "api.groq.com"
    assert KEY not in result.text and "api_key" not in result.text
    stored = (ctx.app.state.settings.data_dir / "model-settings.enc").read_text()
    assert KEY not in stored
    payload = json.loads(ctx.app.state.tokens.decrypt(stored))
    assert payload["config"]["api_key"] == KEY
    evidence = ctx.model.call_args.args[-1]
    assert "Synthetic connection check" in evidence and "CV" in evidence
    assert ctx.client.get("/api/session").json()["model_configured"]
    assert KEY not in ctx.client.get("/api/model").text
    assert KEY not in ctx.client.get("/api/activity").text


def test_saved_model_survives_restart_and_disable_keeps_local_mode(verified):
    ctx = verified
    assert save(ctx).status_code == 200
    settings = Settings(_env_file=None, mode="live", data_dir=ctx.app.state.settings.data_dir)
    restarted = create_app(settings)
    with TestClient(restarted, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf"]
        assert client.get("/api/model").json()["configured"]
        assert settings.llm_api_key.get_secret_value() == KEY
        result = client.post("/api/model/disable")
        assert result.status_code == 200 and not result.json()["configured"]
        assert "Local extraction" in result.json()["message"]
        assert not json.loads(
            restarted.state.tokens.decrypt((settings.data_dir / "model-settings.enc").read_text())
        ).get("config")
    second = create_app(
        Settings(
            _env_file=None,
            mode="live",
            data_dir=settings.data_dir,
            llm_model="env",
            llm_api_key="env-synthetic",
        )
    )
    assert not second.state.settings.model_ready
    second.state.store.engine.dispose()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"api_key": "x"},
        {"api_key": KEY, "model": "unsupported"},
        {"api_key": KEY, "base_url": "https://unexpected.invalid"},
        {"api_key": "private secret with spaces"},
        {"api_key": KEY, "model": 123},
        {"api_key": KEY, "extra": "secret"},
    ],
)
def test_invalid_setup_never_echoes_secret_and_does_not_call_provider(verified, raw):
    result = verified.client.post(
        "/api/model/setup", content=json.dumps(raw), headers={"Content-Type": "application/json"}
    )
    assert result.status_code == 400
    assert KEY not in result.text and "private secret" not in result.text
    verified.model.assert_not_called()
    assert not verified.app.state.settings.model_ready


@pytest.mark.parametrize(
    "error",
    [
        AppError("model_auth_failed", "Key rejected", 401),
        AppError("model_rate_limited", "Quota reached", 429),
        AppError("model_output_invalid", "Bad schema", 502),
    ],
)
def test_failed_connection_preserves_previous_configuration(verified, error):
    ctx = verified
    assert save(ctx).status_code == 200
    before = (ctx.app.state.settings.data_dir / "model-settings.enc").read_text()
    ctx.model.side_effect = error
    assert save(ctx, model="openai/gpt-oss-20b").status_code == error.status
    assert (ctx.app.state.settings.data_dir / "model-settings.enc").read_text() == before
    assert ctx.app.state.settings.llm_model == GROQ_MODEL


def test_setup_resets_processing_approvals_but_preserves_profile_and_gmail(verified):
    from app.fixtures import example_profile

    ctx = verified
    ctx.client.post("/api/profile/confirm", json=example_profile())
    profile = ctx.client.get("/api/profile").json()
    with ctx.app.state.store.session.begin() as db:
        db.add(
            Connection(
                owner=OWNER,
                account="test@example.invalid",
                encrypted_token="synthetic",
                scopes=[],
                consent=True,
            )
        )
        record = Scan(
            owner=OWNER,
            profile_id=profile["id"],
            mode="live",
            state="succeeded",
            result={"processing_consent": True, "jobs": [], "emails": []},
        )
        db.add(record)
        db.flush()
        identifier = record.id
    assert save(ctx).status_code == 200
    assert ctx.client.get("/api/profile").json() == profile
    status = ctx.client.get("/api/session").json()
    assert status["account"] == "test@example.invalid" and not status["email_processing_consent"]
    assert not ctx.app.state.store.owned(Scan, identifier, OWNER).result["processing_consent"]


def test_active_scan_blocks_setup_without_changing_key(verified):
    class Pending:
        def done(self):
            return False

    verified.app.state.tasks["synthetic"] = Pending()
    try:
        assert save(verified).status_code == 409
        verified.model.assert_not_called()
    finally:
        verified.app.state.tasks.pop("synthetic")


def test_setup_requires_csrf_and_fixture_never_calls_external_model(verified, tmp_path):
    ctx = verified
    ctx.client.headers.pop("X-CSRF-Token")
    assert save(ctx).status_code == 403
    fixture = create_app(Settings(_env_file=None, mode="fixture", data_dir=tmp_path))
    with TestClient(fixture, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf"]
        assert client.post("/api/model/setup", json={"api_key": KEY}).status_code == 403
    ctx.model.assert_not_called()


@pytest.mark.parametrize("stored", ["not-encrypted", "encrypted-list"])
def test_unreadable_saved_configuration_fails_to_local_mode(verified, stored):
    ctx = verified
    value = ctx.app.state.tokens.encrypt("[]") if stored == "encrypted-list" else "invalid"
    path = ctx.app.state.settings.data_dir / "model-settings.enc"
    path.write_text(value)
    restart = create_app(
        Settings(
            _env_file=None, mode="live", data_dir=path.parent, llm_api_key="env-key", llm_model="env-model"
        )
    )
    assert not restart.state.settings.model_ready and restart.state.model_setup_error
    restart.state.store.engine.dispose()


@pytest.mark.asyncio
async def test_groq_request_quota_cooldown_and_large_input_are_bounded(monkeypatch, tmp_path):
    from app.integrations import llm

    settings = Settings(
        _env_file=None,
        mode="live",
        data_dir=tmp_path,
        llm_base_url=GROQ_URL,
        llm_model=GROQ_MODEL,
        llm_api_key=KEY,
    )
    calls = []
    real = httpx.AsyncClient

    def response(request):
        calls.append(json.loads(request.content))
        return httpx.Response(429, headers={"retry-after": "60"}, json={"secret": KEY})

    monkeypatch.setattr(
        llm.httpx, "AsyncClient", lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(response))
    )
    client = LLMClient(settings)
    for _ in range(2):
        with pytest.raises(AppError) as error:
            await client.structured(ModelCheck, "Test", "Synthetic")
        assert error.value.code == "model_rate_limited" and KEY not in error.value.message
    assert len(calls) == 1
    assert calls[0]["max_completion_tokens"] == 2048 and calls[0]["reasoning_effort"] == "low"
    settings._model_cooldown_until = 0
    with pytest.raises(AppError) as error:
        await client.structured(ModelCheck, "Test", "x" * 20000)
    assert error.value.code == "model_input_too_large" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_provider_auth_error_never_echoes_response_body(monkeypatch, tmp_path, status):
    from app.integrations import llm

    real = httpx.AsyncClient
    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **kwargs: real(
            **kwargs,
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"message": KEY})),
        ),
    )
    settings = Settings(
        _env_file=None,
        mode="live",
        data_dir=tmp_path,
        llm_base_url=GROQ_URL,
        llm_model=GROQ_MODEL,
        llm_api_key=KEY,
    )
    with pytest.raises(AppError) as error:
        await LLMClient(settings).structured(ModelCheck, "Test", "Synthetic")
    assert error.value.code == "model_auth_failed" and KEY not in error.value.message


@pytest.mark.asyncio
async def test_gmail_model_quota_retains_threads_and_does_not_bypass_consent(monkeypatch, tmp_path):
    model = AsyncMock(side_effect=AppError("model_rate_limited", "Quota", 429))
    monkeypatch.setattr(inbox.LLMClient, "structured", model)
    body = "Please confirm your interview availability."
    threads = [
        {
            "id": "thread",
            "messages": [
                {
                    "id": "message",
                    "internalDate": "1791580000000",
                    "labelIds": ["INBOX"],
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": "recruiter@example.invalid"},
                            {"name": "Subject", "value": "Interview"},
                        ],
                        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
                    },
                }
            ],
        }
    ]
    settings = Settings(
        _env_file=None, mode="live", data_dir=tmp_path, llm_api_key=KEY, llm_model="synthetic"
    )
    rows = await inbox.analyze_threads(
        threads, "candidate@example.invalid", settings, check_consent=lambda: None
    )
    assert rows[0]["requires_response"] and rows[0]["model_fallback"]
    assert "local evidence baseline" in rows[0]["uncertainty"]

    def revoked():
        raise AppError("email_processing_consent_required", "Consent revoked", 403)

    with pytest.raises(AppError):
        await inbox.analyze_threads(threads, "candidate@example.invalid", settings, check_consent=revoked)
    assert model.call_count == 1
