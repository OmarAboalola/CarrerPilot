import base64
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pypdf import PdfWriter
from test_core import client as client
from test_qa_regressions import mail, provider_draft

from app.config import Settings
from app.db import Scan
from app.errors import DraftOutcomeUnknown
from app.fixtures import example_profile, jobs
from app.integrations import gmail
from app.main import create_app
from app.schemas import ProfileExtract, Skill
from app.security import OWNER
from app.services import profiles, workflow
from app.services.inbox import analyze_threads
from app.services.matching import keyword_analysis, market_summary, match_job


def pdf_bytes(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(100, 100)
    if encrypted:
        writer.encrypt('test-only-password')
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.mark.parametrize('raw,code', [(b'', 'invalid_pdf'), (b'not a pdf', 'invalid_pdf'),
    (b'x' * 5_000_001, 'file_too_large'), (pdf_bytes(), 'scanned_pdf'),
    (pdf_bytes(0), 'page_limit'), (pdf_bytes(11), 'page_limit'), (pdf_bytes(encrypted=True), 'encrypted_pdf')],
    ids=['empty', 'invalid', 'oversized', 'scanned', 'no-pages', 'eleven-pages', 'encrypted'])
def test_bad_uploads_are_honest_and_preserve_profile(client, raw, code):
    saved = client.post('/api/profile/confirm', json=example_profile()).json()
    response = client.post('/api/profile/upload', files={'file': ('test.pdf', raw, 'application/pdf')})
    assert response.json()['error'] == code
    assert client.get('/api/profile').json()['id'] == saved['id']


def test_successful_quoted_provider_draft_is_verified_without_retry(monkeypatch):
    body = 'Reviewed reply\n> Quoted approved context'
    payload = {'action_id': 'abc', 'account': 'test@example.invalid', 'to': 'recruiter@example.invalid',
               'subject': 'Review', 'body': body}
    service, credentials = Mock(), Mock(scopes=[gmail.COMPOSE_SCOPE])
    credentials.to_json.return_value = '{}'
    service.users.return_value.getProfile.return_value.execute.return_value = {'emailAddress': payload['account']}
    drafts = service.users.return_value.drafts.return_value
    drafts.create.return_value.execute.return_value = {'id': 'd1'}
    drafts.get.return_value.execute.return_value = provider_draft(body + '\n')
    monkeypatch.setattr(gmail, 'gmail_service', Mock(return_value=(service, credentials)))
    result, _ = gmail.create_verified_draft('{}', payload)
    assert result['verified'] is True
    drafts.create.return_value.execute.assert_called_once_with(num_retries=0)
    raw = drafts.create.call_args.kwargs['body']['message']['raw']
    assert b'> Quoted approved context' in base64.urlsafe_b64decode(raw)
    drafts.get.return_value.execute.side_effect = TimeoutError('synthetic provider timeout')
    with pytest.raises(DraftOutcomeUnknown) as raised:
        gmail.create_verified_draft('{}', payload)
    assert raised.value.draft_id == 'd1'


@pytest.mark.asyncio
async def test_new_incoming_acknowledgment_and_malformed_thread_preserve_outstanding_request():
    threads = [{'id': 't1', 'messages': [mail('a', 'Please send your portfolio for this role.', 1000),
        mail('b', 'Thanks, I will send it tomorrow.', 2000, True), mail('c', 'Thanks, I will look out for it.', 3000)]},
        {'id': 'bad', 'messages': [{'id': 'x', 'internalDate': 'invalid'}]}]
    result = await analyze_threads(threads, 'test@example.invalid', Settings(_env_file=None))
    assert result[0]['requires_response'] and 'portfolio' in result[0]['evidence']
    assert result[1]['analysis_error'] and not result[1]['requires_response']


def test_cross_source_duplicate_url_does_not_inflate_market_and_unknowns_remain_visible():
    profile = example_profile()
    job = {**jobs()[0], 'url': 'https://jobs.example.invalid/vacancy/123'}
    matched = match_job(job, keyword_analysis(job), profile)
    duplicate = {**matched, 'id': 'different-board-id', 'url': matched['url'] + '#details'}
    market = market_summary([matched, duplicate], profile)
    assert market['sample_size'] == 1
    assert all(x['count'] == 1 for x in market['skills'])
    assert any('Salary' in x for x in matched['unknowns'])
    assert any('authorization' in x for x in matched['unknowns'])
    assert any('depth' in x for x in matched['unknowns'])


@pytest.mark.asyncio
async def test_item_failure_and_stale_scan_always_reach_terminal_state(monkeypatch, tmp_path):
    from app.db import Profile
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, mode='fixture'))
    app.state.store.migrate()
    store = app.state.store
    with store.session.begin() as db:
        profile = Profile(owner=OWNER, confirmed=True, data=example_profile())
        db.add(profile)
        db.flush()
        scan = Scan(owner=OWNER, profile_id=profile.id, mode='fixture', state='queued', result={})
        db.add(scan)
        db.flush()
        scan_id = scan.id
    real_analysis = workflow.keyword_analysis
    def partial(job, profile):
        if job['id'] == jobs()[0]['id']:
            raise ValueError('synthetic malformed item')
        return real_analysis(job, profile)
    monkeypatch.setattr(workflow, 'keyword_analysis', partial)
    await workflow.run_scan(app, scan_id)
    value = store.owned(Scan, scan_id, OWNER)
    assert value.state == 'partial' and len(value.result['jobs']) == 11
    assert value.result['market']['sample_size'] > 0
    with store.session.begin() as db:
        newer = Profile(owner=OWNER, confirmed=True, data={**example_profile(), 'name': 'New version'})
        db.add(newer)
        db.flush()
        stale = Scan(owner=OWNER, profile_id=profile.id, mode='fixture', state='queued', result={})
        db.add(stale)
        db.flush()
        stale_id = stale.id
    await workflow.run_scan(app, stale_id)
    assert store.owned(Scan, stale_id, OWNER).state == 'stale'
    store.engine.dispose()


@pytest.mark.asyncio
async def test_optional_model_cannot_upgrade_skill_mentions_or_invent_supported_names(monkeypatch):
    result = ProfileExtract(name='Asha Patel', skills=[Skill(name='Bookkeeping', evidence='Bookkeeping',
        level='demonstrated')], projects=[], education=[], experience=[], languages=[])
    model = AsyncMock(return_value=result)
    monkeypatch.setattr(profiles, 'LLMClient', Mock(return_value=Mock(structured=model)))
    settings = Settings(_env_file=None, mode='live', llm_api_key='test-only', llm_model='test-only')
    parsed = await profiles.extract_profile('Asha Patel\nSkills: Bookkeeping', settings, True)
    assert parsed.skills[0].level == 'declared'
    result.skills[0].name = 'Tax law'
    with pytest.raises(Exception, match='did not match the CV'):
        await profiles.extract_profile('Asha Patel\nSkills: Bookkeeping', settings, True)


def test_annexes_and_language_proficiency_are_not_personal_skills_or_education():
    text = 'Asha Patel\nEducation\nBachelor of Arts 2019 - 2023\nExample College\nSkills\nLanguages: Hindi (Native), English (Fluent)\nANNEXES\nEducation\nDoctor of Medicine\nSkills\nPython'
    parsed = profiles.local_profile(text)
    assert parsed.languages == ['Hindi (Native)', 'English (Fluent)']
    assert not any('Doctor' in x for x in parsed.education)
    assert not parsed.skills


def test_skill_word_in_experience_prose_does_not_end_job_history():
    text = 'Leila Haddad\nExperience\nCoordinator 2021 - 2023\nExample Company\n• Quickly gained skills to use a records system.\nRelated document(s): reference skills passport.pdf\nResearcher 2019 - 2021\nExample Institute\nSkills\nComputer skills Proficient with spreadsheets, presentation software'
    result = profiles.local_profile(text)
    assert any('Researcher' in fact and 'Example Institute' in fact for fact in result.experience)
    assert any('Coordinator' in fact for fact in result.experience)
    assert 'spreadsheets' in {s.name for s in result.skills}



@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', ['2027-01-02T14:30:00.123456+03:00', '2027-01-02T14:30Z'])
async def test_deadline_preserves_fractional_seconds_and_explicit_timezone(deadline):
    result = await analyze_threads([{'id': 'deadline', 'messages': [mail('d',
        f'Please complete the assessment by {deadline}.', 1000)]}], 'test@example.invalid',
        Settings(_env_file=None))
    assert result[0]['deadline'] == deadline
    assert deadline in result[0]['deadline_evidence']


def test_restart_recovers_queued_scan_and_uncertain_execution(tmp_path):
    from fastapi.testclient import TestClient

    from app.db import Action, Profile, Store
    from app.security import payload_hash
    settings = Settings(_env_file=None, data_dir=tmp_path, mode='fixture')
    store = Store(tmp_path)
    store.migrate()
    with store.session.begin() as db:
        profile = Profile(owner=OWNER, confirmed=True, data=example_profile())
        db.add(profile)
        db.flush()
        scan = Scan(owner=OWNER, profile_id=profile.id, mode='fixture', state='queued', result={})
        db.add(scan)
        db.flush()
        payload = {'mode':'synthetic', 'type':'create_gmail_draft'}
        action = Action(owner=OWNER, profile_id=profile.id, scan_id=scan.id, state='executing',
                        payload=payload, payload_hash=payload_hash(payload))
        db.add(action)
        db.flush()
        scan_id, action_id = scan.id, action.id
    store.engine.dispose()
    app = create_app(settings)
    with TestClient(app, base_url='http://127.0.0.1:8765'):
        assert app.state.store.owned(Scan, scan_id, OWNER).state == 'interrupted'
        assert app.state.store.owned(Action, action_id, OWNER).state == 'unknown'



def test_regression_environment_does_not_load_operator_credentials_or_connect_externally():
    import socket
    settings = Settings()
    assert not settings.model_ready and not settings.gmail_ready
    assert settings.data_dir != Path(__file__).resolve().parents[1] / 'data'
    with pytest.raises(AssertionError, match='Mock external network access'):
        socket.create_connection(('192.0.2.1', 443), timeout=0.1)



def test_pdf_symbol_bullets_render_as_unicode_without_changing_facts(monkeypatch):
    page = Mock()
    page.extract_text.return_value = 'Leila Haddad\nSkills\n Bridge inspection\n Structural surveying\nEducation\nDegree 2020 - 2024'
    monkeypatch.setattr(profiles, 'PdfReader', Mock(return_value=Mock(is_encrypted=False,pages=[page])))
    monkeypatch.setattr(profiles, 'layout_pages', Mock(return_value=[page.extract_text.return_value]))
    text = profiles.read_pdf(b'test-reader-boundary')
    assert '• Bridge inspection' in text and '• Structural surveying' in text
    assert 'Degree 2020 - 2024' in text

