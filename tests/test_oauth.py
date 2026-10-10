import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import api
from app.config import Settings
from app.db import Connection
from app.integrations.gmail import READ_SCOPE
from app.main import create_app
from app.security import OWNER


@pytest.fixture
def oauth_client(monkeypatch, tmp_path):
    # Synthetic credentials and mocked Google boundaries: never call Google.
    credentials = Mock()
    credentials.granted_scopes = [READ_SCOPE]
    credentials.to_json.return_value = json.dumps({"scopes": [READ_SCOPE], "token": "test-token"})
    flow = Mock(code_verifier="test-verifier", credentials=credentials)
    flow.authorization_url.return_value = ("https://accounts.google.com/test", "test-state")
    factory = Mock(return_value=flow)
    monkeypatch.setattr(api, "oauth_flow", factory)
    service = Mock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "test@example.invalid"
    }
    monkeypatch.setattr(api, "gmail_service", Mock(return_value=(service, credentials)))
    settings = Settings(
        _env_file=None,
        mode="live",
        data_dir=tmp_path,
        base_url="http://127.0.0.1:8765",
        google_client_id="test-client",
        google_client_secret="test-secret",
        llm_api_key="",
        llm_model="",
    )
    app = create_app(settings)
    with TestClient(app, base_url=settings.base_url) as client:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf"]
        yield SimpleNamespace(client=client, flow=flow, factory=factory, app=app)


def begin(oauth):
    response = oauth.client.post("/api/gmail/connect")
    assert response.status_code == 200, response.text
    assert response.json()["url"] == "https://accounts.google.com/test"


def callback(oauth, **params):
    return oauth.client.get(
        "/api/gmail/callback",
        params={"state": "test-state", "code": "test-code", **params},
        headers={"Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )


def test_google_redirect_connects_and_state_cannot_be_replayed(oauth_client):
    oauth = oauth_client
    begin(oauth)
    response = callback(oauth)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/?gmail=connected"
    oauth.flow.fetch_token.assert_called_once_with(code="test-code", timeout=20)
    assert oauth.factory.call_args.kwargs == {"state": "test-state", "code_verifier": "test-verifier"}
    with oauth.app.state.store.session() as db:
        connection = db.get(Connection, OWNER)
        assert connection.account == "test@example.invalid"
        assert connection.scopes == [READ_SCOPE]
        assert connection.consent is False
        assert connection.encrypted_token != oauth.flow.credentials.to_json.return_value
    response = callback(oauth)
    assert response.status_code == 303
    assert response.headers["location"] == "/?gmail_error=oauth_state_invalid"
    assert oauth.flow.fetch_token.call_count == 1


@pytest.mark.parametrize("scenario", ["missing_session", "wrong_state", "expired", "denied"])
def test_invalid_callback_never_exchanges_code(oauth_client, monkeypatch, scenario):
    oauth = oauth_client
    params = {}
    if scenario != "missing_session":
        begin(oauth)
    if scenario == "wrong_state":
        params["state"] = "forged"
    if scenario == "expired":
        future = datetime.now(UTC) + timedelta(minutes=11)

        class FutureDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return future

        monkeypatch.setattr(api, "datetime", FutureDateTime)
    if scenario == "denied":
        params.update(error="access_denied", code="")
    response = callback(oauth, **params)
    expected = "oauth_denied" if scenario == "denied" else "oauth_state_invalid"
    assert response.headers["location"] == "/?gmail_error=" + expected
    assert response.status_code == 303
    oauth.flow.fetch_token.assert_not_called()


def test_callback_exception_does_not_allow_other_cross_site_requests(oauth_client):
    client = oauth_client.client
    headers = {"Sec-Fetch-Site": "cross-site"}
    for method, path in [
        ("GET", "/api/session"),
        ("GET", "/api/gmail/callback/extra"),
        ("POST", "/api/gmail/callback"),
        ("POST", "/api/gmail/connect"),
    ]:
        response = client.request(method, path, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"] == "cross_site_request"
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/gmail/connect").json()["error"] == "csrf_invalid"
