import base64

import pytest

from app.config import Settings
from app.services.inbox import analyze_threads


def thread(body, subject="Career update", headers=()):
    return {
        "id": "synthetic-thread",
        "messages": [
            {
                "id": "message",
                "internalDate": "1791580000000",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "Career Team <team@example.invalid>"},
                        {"name": "Subject", "value": subject},
                        *headers,
                    ],
                    "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
                },
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "Weekly job recommendations\nHere are recent roles. Please share feedback about these jobs.",
        "Daily job digest\nHere are positions. Please reply to this message.",
        "Recommended jobs for you\nPlease confirm your preferences for these positions.",
        "Here are some of the most recent jobs based on your profile. Please let us know about this role.",
        "Career newsletter\nAssessment and interview tips. Please send feedback. Manage your subscription.",
    ],
)
async def test_unmarked_job_digests_never_become_reply_requests(body, tmp_path):
    result = await analyze_threads(
        [thread(body)], "candidate@example.invalid", Settings(_env_file=None, data_dir=tmp_path)
    )
    assert result[0]["automated"] is True and result[0]["requires_response"] is False
    assert result[0]["response_state"] == "informational"


@pytest.mark.asyncio
async def test_personal_recruiter_with_job_suggestions_is_not_suppressed(tmp_path):
    body = "I found a role that suits your experience. Please confirm your availability for an interview."
    result = await analyze_threads(
        [thread(body, "Interview invitation")],
        "candidate@example.invalid",
        Settings(_env_file=None, data_dir=tmp_path),
    )
    assert not result[0]["automated"] and result[0]["requires_response"]
    assert result[0]["evidence"] in body and "Please confirm" in result[0]["evidence"]


@pytest.mark.asyncio
async def test_reply_evidence_points_to_actual_request_after_long_context(tmp_path):
    body = "Role details and background. " * 30 + "Please reply by tomorrow to confirm the interview."
    result = await analyze_threads(
        [thread(body, "Interview invitation")],
        "candidate@example.invalid",
        Settings(_env_file=None, data_dir=tmp_path),
    )
    assert result[0]["requires_response"] and "Please reply" in result[0]["evidence"]
    assert result[0]["evidence"] in body
