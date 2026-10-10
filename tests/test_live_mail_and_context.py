import base64
import json
from unittest.mock import AsyncMock

import pytest
from test_consent import consent_client as consent_client
from test_consent import request_model
from test_inbox_digests import thread

from app.config import Settings
from app.integrations.jobs import job_key
from app.schemas import EmailAnalysis, JobAnalysis, Requirement
from app.services.inbox import analyze_threads, decode_body
from app.services.matching import keyword_analysis, market_summary, match_job
from app.services.question_context import compact_context
from app.services.requirements import normalize_analysis


@pytest.mark.asyncio
@pytest.mark.parametrize('body,category',[
    ('Thank you for your application. We are reviewing it. Please do not reply to this email. Replies are undeliverable.','confirmation'),
    ('Thank you for applying. Unfortunately we cannot offer you the position. We decided to proceed with another candidate. Please do not reply.','rejection'),
])
async def test_transactional_no_reply_mail_keeps_category_without_recommending_a_response(body,category):
    result=await analyze_threads([thread(body,'Your application')],'owner@example.invalid',Settings(_env_file=None))
    assert result[0]['category']==category
    assert result[0]['automated'] and not result[0]['requires_response']


@pytest.mark.asyncio
async def test_a_draft_is_not_a_sent_reply_or_incoming_message():
    value=thread('Please reply to the recruiter about the interview.','Interview')
    draft=thread('I can attend the interview.','Interview')['messages'][0]
    draft['id']='draft'
    draft['labelIds']=['DRAFT']
    draft['internalDate']='1791580001000'
    value['messages'].append(draft)
    result=await analyze_threads([value],'owner@example.invalid',Settings(_env_file=None))
    assert result[0]['requires_response']
    assert result[0]['latest_message_id']=='message'


@pytest.mark.asyncio
async def test_grounded_model_can_identify_a_request_remaining_after_partial_reply(monkeypatch):
    value=thread('Please send your portfolio and confirm the interview time.','Interview')
    sent=thread('My portfolio is attached.','Interview')['messages'][0]
    sent['id']='sent'
    sent['labelIds']=['SENT']
    sent['internalDate']='1791580001000'
    value['messages'].append(sent)
    mock=AsyncMock(return_value=EmailAnalysis(category='interview',summary='Time confirmation outstanding',requires_response=True,evidence='Please send your portfolio and confirm the interview time.'))
    monkeypatch.setattr('app.integrations.llm.LLMClient.structured',mock)
    result=await analyze_threads([value],'owner@example.invalid',Settings(_env_file=None,mode='live',llm_model='synthetic',llm_api_key='synthetic'))
    assert result[0]['requires_response'] and result[0]['response_state']=='pending_response'
    assert 'outgoing' in mock.call_args.args[2] and '2026-' in mock.call_args.args[2]


def test_large_context_fits_model_budget_and_discloses_omissions_and_counts():
    emails=[{'id':str(i),'subject':'Controlled interview','summary':'Long context. '*300,'evidence':'Please confirm. '*300,'requires_response':True,'automated':False} for i in range(30)]
    data={'emails':emails,'jobs':[],'coverage':{'gmail':{'threads':30,'exhaustive':False}}}
    context=compact_context(data,'Who needs a reply?','What should I do first?',['29'])
    assert len(json.dumps(context,ensure_ascii=False))<=12000
    assert context['retrieved_counts']['response_required']==30
    assert context['context_coverage']['omitted_emails']>0
    assert context['emails'][0]['id']=='29'


def test_qualifications_and_numbers_are_not_skill_gaps_but_evidence_is_retained():
    value=JobAnalysis(required=[Requirement(skill="Bachelor's degree in engineering",evidence="Bachelor's degree in engineering"),Requirement(skill='3',evidence='3. Qualifications'),Requirement(skill='Proficient in Microsoft Office',evidence='Proficient in Microsoft Office'),Requirement(skill='MS Office',evidence='MS Office')])
    normalized=normalize_analysis(value)
    assert len(normalized.required)==1
    assert len(normalized.other_required)==2
    assert normalized.other_required[0].evidence==value.required[0].evidence


def test_language_and_plc_aliases_match_while_cv_prose_mentions_remain_proficiency_questions():
    profile={'name':'Alex','skills':[{'name':'PLC Programming','level':'declared'}],'languages':['Arabic (Native)','English (Fluent)'],'locations':['Egypt'],'remote':True,'seniority':'junior','projects':[],'target_roles':['Maintenance Engineer'],'source_text':'Strong problem-solving skills.','willing_to_learn':True}
    job={'id':'test','title':'Maintenance Engineer','company':'Controlled','location':'Egypt','url':'https://example.invalid/job','description':'Required skills: English, PLC, problem-solving.','mode':'fixture'}
    analysis=keyword_analysis(job,profile)
    matched=match_job(job,analysis,profile)
    assert set(matched['matches'])>={'English','PLC'}
    assert 'problem-solving' in matched['proficiency_questions']
    assert 'problem-solving' not in matched['gaps']
    assert not market_summary([matched],profile)['learning']


def test_industrial_experience_years_block_junior_but_an_upper_bound_does_not():
    base={'title':'Maintenance Engineer','location':'Egypt'}
    value=keyword_analysis(base|{'description':'Requirements\n• 4–6 years of hands-on industrial electrical and automation experience.'})
    assert value.experience_years==4
    upper=keyword_analysis(base|{'description':'Requirements\n• Up to 3 years of relevant experience (fresh graduate acceptable).'})
    assert upper.experience_years is None


def test_localized_employer_requisition_urls_deduplicate():
    base={'id':'a','title':'Robotics Engineer','company':'Independent Employer'}
    assert job_key(base|{'url':'https://careers.example.invalid/fr/fr/job/R1234/title'})==job_key(base|{'url':'https://careers.example.invalid/us/en/job/R1234/title'})


def test_literal_cv_role_evidence_places_a_related_role_ahead_of_a_generic_skill_overlap():
    profile={'name':'Test','skills':[{'name':'PLC Programming','level':'declared'}],'languages':[],
             'experience':['Workshop Engineer 2022 - 2024\nIndependent garage\nDiagnosed electrical faults.'],
             'locations':['Egypt'],'remote':True,'seniority':'junior','projects':[],
             'target_roles':['Workshop Engineer','Maintenance Engineer']}
    base={'id':'related','company':'Controlled','location':'Egypt','url':'https://example.invalid/a','mode':'fixture','description':'Required skills: PLC.'}
    related=match_job(base|{'title':'Workshop Engineer'},keyword_analysis(base,profile),profile)
    other=match_job(base|{'title':'Maintenance Engineer'},keyword_analysis(base,profile),profile)
    assert related['score']>other['score']
    assert related['role_evidence']==['Workshop Engineer']
    assert 0<=related['coverage']<=100


def test_session_cookie_does_not_collide_between_isolated_local_instances(tmp_path):
    from fastapi.testclient import TestClient

    from app.fixtures import example_profile
    from app.main import create_app
    first=create_app(Settings(_env_file=None,data_dir=tmp_path/'a',base_url='http://127.0.0.1:8765'))
    second=create_app(Settings(_env_file=None,data_dir=tmp_path/'b',base_url='http://127.0.0.1:8785'))
    with TestClient(first,base_url='http://127.0.0.1:8765') as a,TestClient(second,base_url='http://127.0.0.1:8785') as b:
        csrf=a.get('/api/session').json()['csrf']
        b.get('/api/session')
        a.cookies.update(b.cookies)
        a.headers['X-CSRF-Token']=csrf
        assert a.post('/api/profile/confirm',json=example_profile()).status_code==200


@pytest.mark.parametrize('operation',['question','draft'])
def test_global_processing_revocation_prevents_next_model_request(consent_client,operation):
    ctx=consent_client
    assert ctx.client.post('/api/processing/consent',json={'enabled':False}).status_code==200
    assert request_model(ctx,operation).status_code==403
    ctx.model.assert_not_awaited()


def test_reply_model_context_does_not_use_an_unverified_cv_candidates_identity(consent_client):
    ctx=consent_client
    assert request_model(ctx,'draft').status_code==200
    evidence=ctx.model.call_args.args[3]
    payload=json.loads(evidence)
    assert payload['profile']=={}
    assert 'mailbox owner has not been confirmed' in payload['sender_identity']


def test_current_question_permission_is_required_even_when_scan_permission_was_saved(consent_client):
    ctx=consent_client
    result=ctx.client.post('/api/questions',json={'scan_id':ctx.scan_id,'question':'Who needs a reply?'})
    assert result.status_code==403
    ctx.model.assert_not_awaited()


def test_cv_replacement_isolates_applied_marks_but_goal_edits_keep_them():
    from types import SimpleNamespace

    from app.services.profile_scope import tracking_prefix
    a=SimpleNamespace(data={'source_text':'Independent first CV\nSkills: Accounting'})
    edited=SimpleNamespace(data={'source_text':'Independent first CV\nSkills: Accounting','target_roles':['Auditor']})
    b=SimpleNamespace(data={'source_text':'Independent second CV\nSkills: Nursing'})
    assert tracking_prefix('owner',a)==tracking_prefix('owner',edited)
    assert tracking_prefix('owner',a)!=tracking_prefix('owner',b)


def test_html_old_requests_are_removed_for_inbox_analysis_but_retained_for_draft_verification():
    body='<p>Thanks for the update.</p><div class="gmail_quote"><blockquote><p>Please confirm your interview.</p></blockquote></div>'
    payload={'mimeType':'text/html','body':{'data':base64.urlsafe_b64encode(body.encode()).decode()}}
    assert 'Please confirm' not in decode_body(payload)
    assert 'Please confirm' in decode_body(payload,strip_quotes=False)


def test_profile_change_hides_old_suggestions_without_deleting_historical_results(consent_client):
    from app.db import Profile, Scan
    from app.security import OWNER
    ctx=consent_client
    with ctx.app.state.store.session.begin() as db:
        old=ctx.app.state.store.latest_profile(OWNER)
        db.add(Profile(owner=OWNER,data=old.data|{'name':'Independent second candidate'},confirmed=True))
    viewed=ctx.client.get('/api/scans/'+ctx.scan_id).json()
    assert not viewed['profile_current']
    assert viewed['result']['jobs']==viewed['result']['emails']==viewed['result']['actions']==[]
    assert ctx.app.state.store.owned(Scan,ctx.scan_id,OWNER).result['jobs']


@pytest.mark.asyncio
async def test_queued_model_call_rechecks_consent_after_acquiring_provider_lock(monkeypatch):
    import asyncio

    from app.errors import AppError
    from app.integrations.llm import LLMClient
    settings=Settings(_env_file=None,mode='live',llm_model='controlled',llm_api_key='controlled',llm_base_url='https://api.groq.com/openai/v1')
    revoked=False
    def guard():
        if revoked:
            raise AppError('processing_consent_required','Revoked',403)
    request=AsyncMock()
    monkeypatch.setattr(LLMClient,'_request',request)
    await settings._model_lock.acquire()
    task=asyncio.create_task(LLMClient(settings,before_request=guard).structured(EmailAnalysis,'Controlled','Controlled evidence'))
    await asyncio.sleep(0)
    revoked=True
    settings._model_lock.release()
    with pytest.raises(AppError,match='Revoked'):
        await task
    request.assert_not_awaited()


@pytest.mark.parametrize('body',["I'll send my availability later.","We’ll provide the documents tomorrow."])
def test_contracted_promises_do_not_count_as_completed_responses(body):
    from app.services.inbox import acknowledgment
    assert acknowledgment(body)


@pytest.mark.asyncio
async def test_schema_valid_deadline_with_different_source_date_is_rejected(monkeypatch):
    body='Please confirm by 2026-10-20T12:00:00Z.'
    mock=AsyncMock(return_value=EmailAnalysis(category='interview',summary='Confirmation requested',requires_response=True,evidence=body,deadline='2026-10-21T12:00:00Z',deadline_evidence=body))
    monkeypatch.setattr('app.integrations.llm.LLMClient.structured',mock)
    result=await analyze_threads([thread(body,'Interview')],'owner@example.invalid',Settings(_env_file=None,mode='live',llm_model='controlled',llm_api_key='controlled'))
    assert result[0]['deadline'] is None
    assert 'ambiguous' in result[0]['uncertainty']


def test_job_outreach_stays_local_until_sender_identity_is_explicitly_confirmed(consent_client):
    from app.db import Scan
    from app.schemas import SourceDraftPlan
    from app.security import OWNER
    ctx=consent_client
    ctx.model.side_effect=lambda self,schema,*args: SourceDraftPlan()
    job_id=ctx.app.state.store.owned(Scan,ctx.scan_id,OWNER).result['jobs'][0]['id']
    value={'scan_id':ctx.scan_id,'source_kind':'job','source_id':job_id,'processing_consent':True,'recipient':'recruiter@example.invalid','contact_confirmed':True,'contact_source':'Controlled QA contact'}
    response=ctx.client.post('/api/drafts',json=value)
    assert response.status_code==200,response.text
    assert response.json()['payload']['account']==''
    refused=ctx.client.post('/api/drafts',json=value|{'sender_identity_confirmed':True,'sender_account':'different@example.invalid'})
    assert refused.status_code==409


def test_model_drafting_rejects_unsupported_fact_ids_before_saving_an_action(consent_client):
    from app.schemas import SourceDraftPlan
    ctx=consent_client
    ctx.model.side_effect=lambda self,schema,*args: SourceDraftPlan(profile_fact_ids=['invented:0'])
    response=request_model(ctx,'draft')
    assert response.status_code==502
    assert response.json()['error']=='unsupported_draft_fact'


def test_mailbox_model_draft_contains_no_candidate_identity_or_invented_proficiency(consent_client):
    from app.schemas import SourceDraftPlan
    ctx=consent_client
    ctx.model.side_effect=lambda self,schema,*args: SourceDraftPlan(request_goal='next_steps')
    response=request_model(ctx,'draft')
    assert response.status_code==200
    body=response.json()['payload']['body']
    assert 'Would you share the next steps?' in body
    assert 'proficien' not in body and 'Omar' not in body
    assert response.json()['payload']['preparation_method']=='configured_model_evidence_selection'


def test_job_model_draft_copies_selected_declared_qualification_without_promoting_completion(consent_client):
    from app.db import Profile, Scan
    from app.schemas import SourceDraftPlan
    from app.security import OWNER
    ctx=consent_client
    literal='Bachelor of Education | 2025 - 2027 (expected)'
    with ctx.app.state.store.session.begin() as db:
        profile=ctx.app.state.store.latest_profile(OWNER)
        db.get(Profile,profile.id).data=profile.data|{'education':[literal]}
    ctx.model.side_effect=lambda self,schema,*args: SourceDraftPlan(profile_fact_ids=['education:0'])
    job_id=ctx.app.state.store.owned(Scan,ctx.scan_id,OWNER).result['jobs'][0]['id']
    response=ctx.client.post('/api/drafts',json={'scan_id':ctx.scan_id,'source_kind':'job','source_id':job_id,'processing_consent':True,'recipient':'recruiter@example.invalid','contact_confirmed':True,'contact_source':'Controlled QA contact'})
    assert response.status_code==200,response.text
    body=response.json()['payload']['body']
    assert literal in body and 'subject to verification' in body
    assert 'completed' not in body and 'proficient' not in body


def test_c_cpp_compound_is_known_only_when_both_languages_are_declared():
    from app.services.matching import profile_known
    profile={'skills':[{'name':'C','level':'declared'},{'name':'C++','level':'declared'}]}
    assert 'c/c++' in profile_known(profile)
    assert 'c/c++' not in profile_known({'skills':profile['skills'][1:]})


def test_arabic_minimum_experience_is_not_treated_as_unknown_or_company_age():
    analysis=keyword_analysis({'description':'خبرة لا تقل عن [10] سنوات في مجال صيانة الآلات.','location':'Egypt'})
    assert analysis.experience_years==10
    assert analysis.experience_evidence in 'خبرة لا تقل عن [10] سنوات في مجال صيانة الآلات.'


def test_unspecified_automation_domain_does_not_drive_software_learning_priorities():
    from app.services.roles import automation_domain_uncertain
    assert automation_domain_uncertain({'title':'Software Quality Engineer (Automation)'},['Automation Engineer'])
    assert not automation_domain_uncertain({'title':'Software Quality Engineer (Automation)'},['Software Automation Engineer'])
    assert not automation_domain_uncertain({'title':'Automation Maintenance Engineer'},['Automation Engineer'])


def test_weekly_counts_cover_omitted_sources_and_distinguish_bulk_from_transactional_mail():
    from datetime import UTC, datetime
    stamp=datetime.now(UTC).isoformat()
    context=compact_context({'emails':[{'id':str(i),'received_at':stamp,'category':'confirmation' if i<2 else 'other','automated':i>=2,'summary':'Large body '*500} for i in range(30)]},'Which important mail arrived this week?')
    assert context['retrieved_counts']['this_week_utc']['total']==30
    assert context['retrieved_counts']['this_week_utc']['by_category']['confirmation']==2
    assert context['retrieved_counts']['this_week_utc']['automated']==28
    assert len(context['emails'])<30


def test_overlapping_declared_skill_is_a_proficiency_question_without_inventing_a_match():
    from app.services.matching import source_skill_mention
    profile={'skills':[{'name':'Team Collaboration','level':'declared'}]}
    assert source_skill_mention(profile,'Collaboration within cross-functional teams')
    assert not source_skill_mention(profile,'Active listening')


def test_declared_team_lead_role_is_related_leadership_evidence_without_inventing_a_skill():
    from app.services.matching import source_skill_mention
    profile={'skills':[],'experience':['Shift Team Leader | 2024\nSupervised a team of laboratory technicians.']}
    assert source_skill_mention(profile,'Leadership')
    assert profile['skills']==[]


def test_live_model_context_receives_the_confirmed_unwillingness_to_learn(consent_client):
    from app.db import Profile
    from app.security import OWNER
    ctx=consent_client
    with ctx.app.state.store.session.begin() as db:
        profile=ctx.app.state.store.latest_profile(OWNER)
        db.get(Profile,profile.id).data=profile.data|{'willing_to_learn':False}
    assert request_model(ctx,'question').status_code==200
    context=json.loads(ctx.model.call_args.args[3])
    assert context['profile_preferences']['willing_to_learn'] is False


def test_basic_qualification_heading_and_optional_inline_criterion_keep_correct_groups():
    analysis=keyword_analysis({'description':'Basic Qualifications\n• PLC\n• C# (NICE TO HAVE)\nPreferred Qualifications\n• SCADA\nJob ID: ABC123','location':'Egypt'})
    assert any(r.skill=='PLC' for r in analysis.required)
    assert all('C#' not in r.skill and 'Job ID' not in r.skill for r in analysis.required)
    assert any('C#' in r.skill for r in analysis.preferred)
    assert all('Job ID' not in r.skill for r in analysis.preferred)


def test_long_explicit_industrial_requirement_and_optional_section_are_not_lost():
    description='Basic Qualifications\n• Experience with PLC controlled automation and issue diagnosis in a continuous process or production environment\nWill be a plus:\n• SCADA'
    analysis=keyword_analysis({'description':description,'location':'Egypt'})
    assert any(r.skill=='PLC' for r in analysis.required)
    assert any(r.skill=='SCADA' for r in analysis.preferred)
    assert all(r.skill!='SCADA' for r in analysis.required)
