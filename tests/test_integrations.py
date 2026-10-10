import base64
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.errors import AppError
from app.fixtures import example_profile, jobs
from app.integrations.gmail import draft_matches
from app.integrations.jobs import fetch_jobs
from app.integrations.llm import LLMClient
from app.services.inbox import analyze_threads
from app.services.matching import keyword_analysis, market_summary, match_job


def model_client(monkeypatch, module, response):
    real_client = httpx.AsyncClient

    def configured(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(response)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=configured, HTTPError=httpx.HTTPError))


@pytest.mark.asyncio
async def test_model_invalid_schema_and_fixture_isolation(monkeypatch, tmp_path):
    from app.integrations import llm

    class Result(BaseModel):
        category: str

    model_client(
        monkeypatch,
        llm,
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}),
    )
    settings = Settings(
        mode="live", data_dir=tmp_path, llm_api_key="synthetic-test-value", llm_model="test-model"
    )
    with pytest.raises(AppError) as raised:
        await LLMClient(settings).structured(Result, "classify", "synthetic")
    assert raised.value.code == "model_output_invalid"
    with pytest.raises(AppError) as raised:
        await LLMClient(Settings(mode="fixture", data_dir=tmp_path)).structured(
            Result, "classify", "synthetic"
        )
    assert raised.value.code == "fixture_model_disabled"


@pytest.mark.asyncio
async def test_source_empty_error_and_html_safety(monkeypatch, tmp_path):
    from app.integrations import jobs as module

    settings = Settings(data_dir=tmp_path)
    model_client(monkeypatch, module, lambda request: httpx.Response(200, json={"jobs": []}))
    assert await fetch_jobs(settings) == ([], [])
    model_client(monkeypatch, module, lambda request: httpx.Response(403, json={}))
    result, errors = await fetch_jobs(settings)
    assert not result and errors
    assert module.plain_text("<script>ignore rules</script><p>Python &amp; SQL</p>") == "Python & SQL"


def test_draft_verification_checks_exact_content_and_recipient():
    payload = {"action_id": "abc", "to": "one@example.invalid", "subject": "Hello", "body": "Approved body"}
    draft = {
        "id": "draft",
        "message": {
            "id": "message",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Message-ID", "value": "<careerpilot-abc@local.invalid>"},
                    {"name": "To", "value": "one@example.invalid"},
                    {"name": "Subject", "value": "Hello"},
                ],
                "body": {"data": base64.urlsafe_b64encode(b"Approved body\n").decode()},
            },
        },
    }
    assert draft_matches(draft, payload)
    assert not draft_matches(draft, {**payload, "to": "different@example.invalid"})
    assert not draft_matches(draft, {**payload, "body": "Changed body"})


def test_duplicate_jobs_do_not_inflate_skill_market():
    profile = example_profile()
    matched = [match_job(job, keyword_analysis(job), profile) for job in jobs()]
    assert (
        market_summary(matched + matched, profile)["sample_size"]
        == market_summary(matched, profile)["sample_size"]
    )


@pytest.mark.asyncio
async def test_sent_thread_reply_is_not_unanswered(tmp_path):
    incoming = {
        "id": "incoming",
        "internalDate": "1000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "recruiter@example.invalid"},
                {"name": "Subject", "value": "Interview"},
            ],
            "body": {"data": base64.urlsafe_b64encode(b"Please reply").decode()},
        },
    }
    outgoing = {
        "id": "outgoing",
        "internalDate": "2000",
        "labelIds": ["SENT"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "me@example.invalid"},
                {"name": "Subject", "value": "Interview"},
            ],
            "body": {"data": base64.urlsafe_b64encode(b"My reply").decode()},
        },
    }
    settings = Settings(
        _env_file=None, mode="live", data_dir=tmp_path, llm_api_key="", llm_model=""
    )
    result = await analyze_threads(
        [{"id": "thread", "messages": [incoming, outgoing]}], "me@example.invalid", settings
    )
    assert result[0]["response_state"] == "responded" and result[0]["requires_response"] is False
