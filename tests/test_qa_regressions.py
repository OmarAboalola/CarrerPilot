"""Reproducible QA defects; providers are synthetic and network-free."""
import base64
from unittest.mock import Mock

import httpx
import pytest
from test_core import client as client
from test_core import finish_scan

from app.config import Settings
from app.fixtures import example_profile
from app.integrations.gmail import draft_matches
from app.integrations.jobs import fetch_jobs
from app.services.inbox import analyze_threads
from app.services.profiles import scrub_contacts


@pytest.mark.parametrize('dates', ['2013 - 2016', '2020-2024', '01/2020 - 06/2024', '2010 - 2012'])
def test_contact_scrubbing_preserves_dates(dates):
    text = f'Education {dates}\nPhone +44 7123456789\nEmail demo@example.invalid'
    result = scrub_contacts(text)
    assert dates in result
    assert '+44 7123456789' not in result
    assert 'demo@example.invalid' not in result


def provider_draft(body, **overrides):
    return {'id': 'd1', 'message': {'id': 'm1', 'threadId': 't1', 'payload': {
        'mimeType': 'text/plain', 'headers': [
            {'name': 'Message-ID', 'value': '<careerpilot-abc@local.invalid>'},
            {'name': 'From', 'value': 'test@example.invalid'},
            {'name': 'To', 'value': 'recruiter@example.invalid'},
            {'name': 'Subject', 'value': 'Review'}],
        'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}, **overrides}


def test_draft_verification_retains_approved_quoted_lines_and_spaces():
    body = 'My reply\n> Approved quotation\n  Indented line  '
    payload = {'action_id': 'abc', 'to': 'recruiter@example.invalid', 'account': 'test@example.invalid',
               'subject': 'Review', 'body': body, 'thread_id': 't1'}
    assert draft_matches(provider_draft(body + '\n'), payload)
    assert not draft_matches(provider_draft(body.rstrip() + '\n'), payload)
    value = provider_draft(body + '\n')
    value['message']['threadId'] = 'wrong-thread'
    assert not draft_matches(value, payload)


def mail(identifier, body, time, sent=False, subject='Interview request'):
    return {'id': identifier, 'internalDate': str(time), 'labelIds': ['SENT'] if sent else [],
            'payload': {'mimeType': 'text/plain', 'headers': [
                {'name': 'From', 'value': 'test@example.invalid' if sent else 'recruiter@example.invalid'},
                {'name': 'Subject', 'value': subject}],
                'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}


@pytest.mark.asyncio
@pytest.mark.parametrize('ack', ['Thanks, I will send my availability tomorrow.',
                               'Received, I will get back to you later.', 'Thank you!'])
async def test_sent_acknowledgment_keeps_request_outstanding(ack):
    threads = [{'id': 't1', 'messages': [mail('m2', ack, 2000, True),
               mail('m1', 'Please confirm your interview availability.', 1000)]}]
    result = await analyze_threads(threads, 'test@example.invalid', Settings(_env_file=None))
    assert result[0]['requires_response']
    assert result[0]['response_state'] == 'pending_response'
    assert 'confirm' in result[0]['evidence']


@pytest.mark.asyncio
async def test_explicit_timezone_deadline_retained_and_ambiguous_deadline_disclosed():
    threads = [{'id': 't1', 'messages': [mail('m1',
        'Please complete the assessment before 2026-10-12T17:00:00+02:00.', 1000, subject='Assessment')]},
        {'id': 't2', 'messages': [mail('m2', 'Please complete the assessment by Friday.', 1000,
                                     subject='Assessment')]}]
    result = {x['id']: x for x in await analyze_threads(threads, 'test@example.invalid',
                                                       Settings(_env_file=None))}
    assert result['t1']['deadline'] == '2026-10-12T17:00:00+02:00'
    assert result['t2']['deadline'] is None
    assert 'ambiguous' in result['t2']['uncertainty'].lower()


@pytest.mark.parametrize('question', ['Which recruiters still need a reply?',
    'Who is waiting for me to respond?', 'Which conversations require a response?'])
def test_questions_identify_requested_action_with_evidence(client, question):
    client.post('/api/profile/confirm', json=example_profile())
    scan = finish_scan(client)
    answer = client.post('/api/questions', json={'scan_id': scan['id'], 'question': question}).json()
    assert 'confirm' in answer['answer'].lower() or 'reply' in answer['answer'].lower()
    assert answer['source_ids']
    assert 'limited' in answer['answer'].lower() or 'sample' in answer['answer'].lower()


def test_job_question_explains_profile_fit(client):
    client.post('/api/profile/confirm', json=example_profile())
    scan = finish_scan(client)
    answer = client.post('/api/questions', json={'scan_id': scan['id'],
        'question': 'Why is this job recommended for this profile?'}).json()
    assert 'Python' in answer['answer'] or 'python' in answer['answer']
    assert 'unknown' in answer['answer'].lower() or 'verify' in answer['answer'].lower()


@pytest.mark.asyncio
async def test_job_source_retains_valid_items_after_malformed_item_and_before_role_cap(monkeypatch):
    from app.integrations import jobs
    rows = [{'id': i, 'title': 'Nurse' if i == 4 else 'Accountant',
             'absolute_url': f'https://jobs.example.invalid/{i}', 'content': 'Skills: Nursing'}
            for i in range(5)]
    rows.insert(1, {'id': 'bad', 'title': None, 'absolute_url': 'https://example.invalid'})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(jobs.httpx, 'AsyncClient', Mock(side_effect=lambda **kwargs:
        original_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'jobs': rows})),
                        **kwargs)))
    result, errors = await fetch_jobs(Settings(_env_file=None, max_jobs=2))
    assert len(result) == 5
    assert any(x['title'] == 'Nurse' for x in result)
    assert errors
