"""Synthetic assignment integration, CAS/rollback and frozen paid-context evidence."""
from __future__ import annotations

import copy
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient

from secretary.api import create_app, ProcessRequest
from secretary.application.evidence import validate_summary
from secretary.application.exporting import export_meeting
from secretary.application.worker import Worker
from secretary.domain.models import TranscriptSegment
from secretary.domain.speakers import AddParticipant, PatchParticipant, ReviewAttribution, SpeakerConflict
from secretary.domain.summary_context import ReviewTaskAssignments
from secretary.domain.assignment_evidence import AssignmentEvidenceError
from secretary.infrastructure.assignment_repository import AssignmentRepository
from secretary.infrastructure.database import Database
from secretary.infrastructure.polza import PolzaClient, ProviderError
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.settings import Settings
from test_backend import prepared, FakeCapture, auth


def setup(tmp_path, text='Я подготовлю отчёт', *, identity=True):
    settings = Settings(_env_file=None,data_dir=tmp_path/'data',project_dir=tmp_path,
                        polza_api_key='synthetic-only',cloud_enabled=True,summary_batch_chars=5000,
                        local_cost_limits_enabled=False)
    db = Database(settings.data_dir/'secretary.sqlite3')
    meeting,chunk = prepared(db,settings)
    version = db.ensure_version(meeting['id'])
    stt = db.enqueue(meeting['id'],'transcribe',chunk_id=chunk['id'],version=version)
    segment = TranscriptSegment(id='s',meeting_id=meeting['id'],transcript_version=version,chunk_id=chunk['id'],
        ordinal=0,text=text,channel='import',timing_precision='unknown').model_dump()
    db.save_segments(stt,[segment],[])
    speakers = SpeakerRepository(db)
    roster = speakers.add_participant(meeting['id'],AddParticipant(display_name='Павел Александрович',aliases=['Павел'],expected_roster_revision=0,operation_id='add'))
    participant = roster.participants[0].id
    if identity:
        speakers.review_attribution(meeting['id'],ReviewAttribution(transcript_version=version,expected_revision=0,
            operation_id='identity',changes=[{'segment_id':'s','participant_id':participant}]))
    worker = Worker(db,settings,lambda *_: (_ for _ in ()).throw(AssertionError('No provider')))
    job = worker.enqueue_summary(meeting['id'])
    return db,settings,meeting,participant,speakers,worker,job


def draft(text, *, basis='self_commitment',flag='explicit_commitment',named=None,quote=None):
    return {'overview':'Синтетическая встреча','readable_transcript':text,'decisions':[],'open_questions':[],
        'action_items':[{'text':'Подготовить отчёт','owner':named,'due_date':None,'source_segment_ids':['s'],
            'evidence_quote':text if quote is None else quote,'assignment_proposal':{
                'basis':basis,'semantic_flag':flag,'named_owner_text':named,
                'commitment_segment_id':'s' if basis=='self_commitment' else None}}]}


def save(db,meeting,job,result):
    sources = db.segments(meeting['id'],1,limit=100)['items']
    return db.save_summary(job,validate_summary(copy.deepcopy(result),meeting['id'],1,sources))


def review(state, decision='confirm_proposal', participant=None, op='review', **edits):
    return ReviewTaskAssignments.model_validate({
        'transcript_version':state.transcript_version,'summary_version':state.summary_version,
        'attribution_revision':state.attribution_revision,'roster_revision':state.roster_revision,
        'expected_revision':state.revision,'operation_id':op,
        'changes':[{'action_id':state.items[0].action.action_id,'decision':decision,'participant_id':participant}],**edits})


@pytest.mark.parametrize('text,basis,flag,named,identity,expected',[
    ('Я подготовлю отчёт','self_commitment','explicit_commitment',None,True,'proposed'),
    ('Павел Александрович подготовит отчёт','named_person','explicit_commitment','Павел Александрович',False,'proposed'),
    ('Павел, подготовь отчёт','named_person','directive','Павел',False,'proposed'),
    ('Я не буду делать отчёт','self_commitment','negation',None,True,'needs_review'),
    ('Павел сказал: я подготовлю отчёт','self_commitment','reported_speech',None,True,'needs_review'),
    ('Мы сделаем отчёт','self_commitment','ambiguous',None,True,'needs_review'),
    ('Я подготовлю отчёт','self_commitment','explicit_commitment',None,False,'needs_review'),
])
def test_seven_semantic_rows_persist_exact_proof(tmp_path,text,basis,flag,named,identity,expected):
    db,_,meeting,p,_,_,job = setup(tmp_path,text,identity=identity)
    save(db,meeting,job,draft(text,basis=basis,flag=flag,named=named))
    repo = AssignmentRepository(db)
    state = repo.get(meeting['id'])
    item = state.items[0]
    assert item.status == expected and item.basis == basis
    assert item.action.evidence_quote == text and item.action.source_segment_ids == ('s',)
    assert item.anchor.segment_id == 's'
    assert repo.projected(meeting['id'])['action_items'][0]['assignment_status'] == expected
    if expected=='proposed':
        assert item.participant_id == p
        assert repo.review(meeting['id'],review(state)).current.items[0].status=='confirmed'
    else:
        with pytest.raises(AssignmentEvidenceError):
            repo.review(meeting['id'],review(state))


def test_manual_replay_after_local_edit_preserves_receipt_current_stale_and_no_jobs(tmp_path):
    db,_,m,p,speakers,_,job = setup(tmp_path)
    save(db,m,job,draft('Я подготовлю отчёт'))
    repo = AssignmentRepository(db)
    state = repo.get(m['id'])
    command = review(state,'set_manual',p)
    receipt = repo.review(m['id'],command)
    before = {t:db.rows(f'SELECT * FROM {t}') for t in ('jobs','summaries','summary_input_contexts','summary_checkpoints','usage','segments')}
    speakers.patch_participant(m['id'],p,PatchParticipant(expected_roster_revision=1,operation_id='disable',enabled=False))
    replay = repo.review(m['id'],command)
    assert replay.replayed and replay.receipt_stale and replay.receipt==receipt.receipt
    assert replay.current.items[0].basis=='manual' and replay.current.items[0].participant_id==p
    assert replay.current.items[0].status=='needs_review'
    assert before=={t:db.rows(f'SELECT * FROM {t}') for t in before}


def test_cas_and_batch_failure_receipt_rollback(tmp_path,monkeypatch):
    db,_,m,p,_,_,job = setup(tmp_path)
    save(db,m,job,draft('Я подготовлю отчёт'))
    repo = AssignmentRepository(db)
    state=repo.get(m['id'])
    bad=review(state,'set_manual','foreign')
    with pytest.raises(AssignmentEvidenceError):
        repo.review(m['id'],bad)
    assert repo.get(m['id'])==state
    monkeypatch.setattr(repo.speakers,'_receipt',lambda *_: (_ for _ in ()).throw(sqlite3.IntegrityError('synthetic failure')))
    with pytest.raises(sqlite3.IntegrityError):
        repo.review(m['id'],review(state,'set_manual',p))
    assert repo.get(m['id'])==state and not db.one("SELECT 1 FROM speaker_operations WHERE operation_id='review'")
    with pytest.raises(SpeakerConflict):
        repo.review(m['id'],review(state,expected_revision=10))


def test_concurrent_cas_one_winner_and_exact_replay(tmp_path):
    db,_,m,p,_,_,job = setup(tmp_path)
    save(db,m,job,draft('Я подготовлю отчёт'))
    repo=AssignmentRepository(db); state=repo.get(m['id']); barrier=Barrier(2)
    def commit(op):
        barrier.wait()
        try:
            return repo.review(m['id'],review(state,'set_manual',p,op))
        except SpeakerConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(commit,['op1','op2']))
    assert sum(r=='conflict' for r in result)==1
    winner=next(r for r in result if r!='conflict')
    assert repo.review(m['id'],review(state,'set_manual',p,winner.operation_id)).replayed


def test_legacy_read_only_rev0_manual_mutation_and_historical_scope(tmp_path):
    db,_,m,p,_,_,job = setup(tmp_path)
    raw=validate_summary({'overview':'legacy','readable_transcript':'','decisions':[],'open_questions':[],
        'action_items':[{'text':'Старая задача','owner':'Павел','due_date':None,'source_segment_ids':['s']}]},m['id'],1,db.segments(m['id'],1)['items'])
    raw['summary_version']=1
    db.execute('INSERT INTO summaries VALUES(?,?,?,?)',(m['id'],1,1,json.dumps(raw)))
    repo=AssignmentRepository(db); state=repo.get(m['id'])
    assert state.revision==0 and state.captured_context_hash is None
    assert state.items[0].participant_id is None and not db.rows('SELECT * FROM assignment_snapshots')
    result=repo.review(m['id'],review(state,'set_manual',p))
    assert result.current.items[0].status=='confirmed' and result.current.items[0].action.evidence_quote==''
    assert len(db.rows('SELECT * FROM assignment_snapshots'))==2
    db.update_meeting(m['id'],transcript_version=2)
    with pytest.raises(SpeakerConflict):
        # Same summary is now historical. New operation cannot edit it.
        repo.review(m['id'],review(result.current,'clear',op='historical'))


@pytest.mark.parametrize('format',['json','txt','md','docx'])
def test_exports_keep_unknown_quote_status_provenance_and_honest_time(tmp_path,format):
    db,_,m,_,_,_,job=setup(tmp_path,identity=False)
    save(db,m,job,draft('Я подготовлю отчёт'))
    values=AssignmentRepository(db).export_snapshot(m['id'])
    meeting,segments,summary,state,raw,context=values
    content,_=export_meeting(meeting,segments,summary,format,assignment_snapshot=state,raw_summary=raw,summary_context=context)
    if format=='json':
        payload=json.loads(content)
        assert payload['summary_context']['context_hash']==state.captured_context_hash
        assert payload['task_assignments']['items'][0]['action']['evidence_quote']=='Я подготовлю отчёт'
        assert payload['summary']['action_items'][0]['owner'] is None
    else:
        text='\n'.join(p.text for p in Document(io.BytesIO(content)).paragraphs) if format=='docx' else content.decode()
        assert 'Я подготовлю отчёт' in text and 'needs_review' in text
        assert 'Не определён' in text and 'таймкод неизвестен' in text and 'self_commitment' in text


@pytest.mark.asyncio
async def test_paid_cancel_resume_frozen_context_exact_posts_and_explicit_regenerate(tmp_path):
    db,settings,m,p,speakers,worker,job=setup(tmp_path)
    requests=[]
    def handle(request):
        payload=json.loads(request.content); requests.append(payload)
        output=draft('Я подготовлю отчёт')
        db.cancel(job['id'])  # cancellation after accepted response keeps checkpoint
        return httpx.Response(200,json={'id':'synthetic','choices':[{'message':{'content':json.dumps(output)},'finish_reason':'stop'}],'usage':{'cost_rub':.01}})
    worker.provider_factory=lambda cfg:PolzaClient(cfg,httpx.MockTransport(handle))
    assert await worker.run_once()
    assert db.job(job['id'])['status']=='cancelled' and len(requests)==1
    original=db.one('SELECT payload FROM summary_input_contexts WHERE job_id=?',(job['id'],))['payload']
    checkpoints=db.rows('SELECT * FROM summary_checkpoints')
    speakers.review_attribution(m['id'],ReviewAttribution(transcript_version=1,expected_revision=1,operation_id='unset',changes=[{'segment_id':'s','participant_id':None}]))
    # Do not cancel on cache reuse; no POST is expected.
    assert worker.enqueue_summary(m['id'],retry=True)['id']==job['id']
    assert await worker.run_once()
    assert len(requests)==1 and db.job(job['id'])['status']=='succeeded'
    assert db.rows('SELECT * FROM summary_checkpoints')==checkpoints
    assert db.one('SELECT payload FROM summary_input_contexts WHERE job_id=?',(job['id'],))['payload']==original
    assert AssignmentRepository(db).get(m['id']).items[0].status=='needs_review'
    fresh=worker.enqueue_summary(m['id'],regenerate_summary=True)
    assert fresh['id']!=job['id']
    assert json.loads(fresh['payload'])['summary_context']['attribution_revision']==2
    assert len(db.rows('SELECT * FROM summaries'))==1


def test_publication_failure_rolls_back_summary_snapshot_and_recovery_is_idempotent(tmp_path,monkeypatch):
    db,_,m,_,_,_,job=setup(tmp_path)
    original=AssignmentRepository.publish
    def fail(self,conn,job,summary):
        original(self,conn,job,summary)
        raise sqlite3.IntegrityError('injected after all publication rows')
    monkeypatch.setattr(AssignmentRepository,'publish',fail)
    with pytest.raises(sqlite3.IntegrityError):
        save(db,m,job,draft('Я подготовлю отчёт'))
    assert not db.rows('SELECT * FROM summaries') and not db.rows('SELECT * FROM assignment_snapshots')
    assert db.job(job['id'])['status']=='queued'
    monkeypatch.setattr(AssignmentRepository,'publish',original)
    first=save(db,m,job,draft('Я подготовлю отчёт'))
    assert save(db,m,job,draft('Я подготовлю отчёт'))==first
    assert len(db.rows('SELECT * FROM summaries'))==1


def test_process_flag_and_assignment_api_security(tmp_path):
    from pydantic import ValidationError
    with pytest.raises(ValidationError): ProcessRequest(stage='transcribe',regenerate_summary=True)
    with pytest.raises(ValidationError): ProcessRequest(stage='summarize',regenerate_summary=True,retry=True)
    db,settings,m,p,_,_,job=setup(tmp_path)
    save(db,m,job,draft('Я подготовлю отчёт'))
    with TestClient(create_app(settings,capture=FakeCapture(),enrollment_capture=FakeCapture(),run_worker=False),base_url='http://127.0.0.1:8765') as client:
        url=f"/api/v1/meetings/{m['id']}/task-assignments"
        state=AssignmentRepository(db).get(m['id']); body=review(state).model_dump(mode='json')
        assert client.get(url).status_code==200
        assert client.patch(url,json=body).status_code==403
        assert client.patch(url,headers={**auth(client),'Origin':'https://evil.example'},json=body).status_code==403
        assert client.patch(url,headers=auth(client),json=body).status_code==200
        assert client.patch(url,headers=auth(client),json=body).json()['replayed']


def test_segments_explicit_version_exact_source_paging_and_foreign_meeting(tmp_path):
    db,settings,m,_,_,_,_=setup(tmp_path)
    db.update_meeting(m['id'],transcript_version=2)
    foreign=db.create_meeting('Other synthetic')
    with TestClient(create_app(settings,capture=FakeCapture(),enrollment_capture=FakeCapture(),run_worker=False),base_url='http://127.0.0.1:8765') as client:
        url=f"/api/v1/meetings/{m['id']}/segments"
        assert client.get(url,params={'segment_id':'s'}).json()=={'items':[],'total':0,'transcript_version':2}
        old=client.get(url,params={'segment_id':'s','version':1}).json()
        assert old['items'][0]['text']=='Я подготовлю отчёт' and old['transcript_version']==1
        page=client.get(url,params={'version':1,'offset':0,'limit':1}).json()
        assert page['total']==1 and page['items'][0]['id']=='s'
        assert client.get(url,params={'version':1,'offset':1,'limit':1}).json()['items']==[]
        assert client.get(f"/api/v1/meetings/{foreign['id']}/segments",params={'version':1,'segment_id':'s'}).json()['items']==[]


def test_unknown_self_local_manual_identity_revalidation_and_clear_no_paid_work(tmp_path):
    db,_,m,p,speakers,_,job=setup(tmp_path,identity=False)
    save(db,m,job,draft('Я подготовлю отчёт'))
    repo=AssignmentRepository(db); before={t:db.rows(f'SELECT * FROM {t}') for t in ('jobs','summary_checkpoints','usage','summaries','summary_input_contexts','segments')}
    assert repo.get(m['id']).items[0].participant_id is None
    speakers.review_attribution(m['id'],ReviewAttribution(transcript_version=1,expected_revision=0,operation_id='review-identity',changes=[{'segment_id':'s','participant_id':p}]))
    current=repo.get(m['id'])
    assert current.items[0].participant_id==p and current.items[0].status=='needs_review' and current.items[0].confirm_eligible
    confirmed=repo.review(m['id'],review(current))
    assert confirmed.current.items[0].status=='confirmed'
    cleared=repo.review(m['id'],review(confirmed.current,'clear',op='clear')).current
    assert cleared.items[0].participant_id is None and cleared.items[0].basis=='unknown'
    assert before=={t:db.rows(f'SELECT * FROM {t}') for t in before}


@pytest.mark.parametrize('mutation',['duplicate','alias_removed','disabled','pronoun'])
def test_named_roster_changes_locally_invalidate_and_do_not_spend(tmp_path,mutation):
    text='Павел подготовит отчёт'
    db,_,m,p,speakers,_,job=setup(tmp_path,text,identity=False)
    save(db,m,job,draft(text,basis='named_person',named='Павел'))
    repo=AssignmentRepository(db); current=repo.get(m['id'])
    repo.review(m['id'],review(current))
    before=db.rows('SELECT * FROM jobs')
    if mutation=='duplicate':
        speakers.add_participant(m['id'],AddParticipant(display_name='Павел',expected_roster_revision=1,operation_id='second'))
    elif mutation=='alias_removed':
        speakers.patch_participant(m['id'],p,PatchParticipant(aliases=[],expected_roster_revision=1,operation_id='remove'))
    elif mutation=='disabled':
        speakers.patch_participant(m['id'],p,PatchParticipant(enabled=False,expected_roster_revision=1,operation_id='disable'))
    else:
        speakers.patch_participant(m['id'],p,PatchParticipant(display_name='Я',aliases=['я','мы'],expected_roster_revision=1,operation_id='pronoun'))
    changed=repo.get(m['id']).items[0]
    assert changed.status=='needs_review' and changed.participant_id is None
    assert db.rows('SELECT * FROM jobs')==before and not db.rows('SELECT * FROM usage')


@pytest.mark.asyncio
async def test_v2_repair_retains_context_and_checkpoint_evidence(tmp_path):
    db,settings,m,_,_,_,job=setup(tmp_path)
    frozen=json.loads(job['payload'])['summary_context']; calls=[]; checkpoints={}
    def handler(request):
        payload=json.loads(request.content); calls.append(payload)
        output=draft('Я подготовлю отчёт')
        if len(calls)==1: output['action_items'][0]['evidence_quote']='выдумка'
        return httpx.Response(200,json={'id':str(len(calls)),'choices':[{'message':{'content':json.dumps(output)},'finish_reason':'stop'}],'usage':{'cost_rub':.01}})
    async def get(key):return checkpoints.get(key)
    async def put(key,value):checkpoints[key]=value
    provider=PolzaClient(settings,httpx.MockTransport(handler))
    sources=db.segments(m['id'],1)['items']
    from secretary.domain.summary_context import FrozenSummaryContext
    context=FrozenSummaryContext.model_validate(frozen)
    result=await provider.summarize(sources,context=context,checkpoint_get=get,checkpoint_put=put)
    assert len(calls)==2 and result['action_items'][0]['evidence_quote']=='Я подготовлю отчёт'
    repair=json.loads(calls[1]['messages'][-1]['content'].split('\n')[-1])
    assert repair['untrusted_summary_context']['context_hash']==context.context_hash
    assert repair['untrusted_source_segments'][0]['participant_id']==context.sources[0].participant_id
    assert next(iter(checkpoints.values()))['version']==2
    assert (await provider.summarize(sources,context=context,checkpoint_get=get,checkpoint_put=put))['action_items']==result['action_items']
    assert len(calls)==2
    checkpoint=next(iter(checkpoints.values())); checkpoint['context']['roster_revision']+=1
    with pytest.raises(ProviderError,match='Контекст'):
        await provider.summarize(sources,context=context,checkpoint_get=get,checkpoint_put=put)
    assert len(calls)==2


@pytest.mark.asyncio
async def test_contextless_paid_checkpoint_legacy_v1_resume_and_unreconstructable_no_post(tmp_path):
    db,settings,m,_,_,worker,job=setup(tmp_path)
    # Construct an independently accepted legacy job, retaining exact legacy settings.
    db.cancel(job['id'])
    calls=[]; checkpoints={}
    def handler(request):
        calls.append(json.loads(request.content))
        output=draft('Я подготовлю отчёт'); output['action_items'][0].pop('assignment_proposal')
        return httpx.Response(200,json={'id':'v1','choices':[{'message':{'content':json.dumps(output)},'finish_reason':'stop'}],'usage':{'cost_rub':.01}})
    async def get(key):return checkpoints.get(key)
    async def put(key,value):checkpoints[key]=value
    old=PolzaClient(settings,httpx.MockTransport(handler))
    sources=db.segments(m['id'],1)['items']
    await old.summarize(sources,checkpoint_get=get,checkpoint_put=put)
    legacy=db.enqueue(m['id'],'summarize',version=1,payload={'settings':worker.route_snapshot('summarize')})
    for key,value in checkpoints.items():db.execute('INSERT INTO summary_checkpoints VALUES(?,?,?)',(legacy['id'],key,json.dumps(value)))
    worker.provider_factory=lambda cfg:PolzaClient(cfg,httpx.MockTransport(handler))
    assert await worker.run_once() and db.job(legacy['id'])['status']=='succeeded' and len(calls)==1
    assert json.loads(db.job(legacy['id'],internal=True)['payload'])['summary_contract']=='legacy-v1'
    assert AssignmentRepository(db).get(m['id']).items[0].status=='needs_review'
    broken=db.enqueue(m['id'],'summarize',version=1,payload={'settings':{}})
    db.execute('INSERT INTO summary_checkpoints VALUES(?,?,?)',(broken['id'],'synthetic-key','{}'))
    assert await worker.run_once() and len(calls)==1
    assert 'legacy_summary_context_needs_review' in db.job(broken['id'])['error']
    # Complete settings alone cannot justify an unrelated or malformed paid step.
    for corrupt in ('key', 'payload', 'source'):
        settings_payload = worker.route_snapshot('summarize')
        if corrupt == 'payload':
            settings_payload['summary_max_output_tokens'] += 1
        rejected=db.enqueue(m['id'],'summarize',version=1,payload={'settings':settings_payload})
        for key,value in checkpoints.items():
            value=json.loads(json.dumps(value))
            if corrupt == 'source':
                value['output']['action_items'][0]['evidence_quote']='Чужая неподтверждённая цитата'
            if corrupt == 'key':
                key='unrelated-key'; value['key']=key
            db.execute('INSERT INTO summary_checkpoints VALUES(?,?,?)',(rejected['id'],key,json.dumps(value)))
        assert await worker.run_once() and len(calls)==1
        assert 'legacy_summary_context_needs_review' in db.job(rejected['id'])['error']
        assert not db.one('SELECT 1 FROM summary_input_contexts WHERE job_id=?',(rejected['id'],))


def test_export_snapshot_keeps_public_meeting_fields_and_error_projection(tmp_path):
    db,_,m,_,_,_,_=setup(tmp_path)
    db.update_meeting(m['id'],media_path='D:/private-local/synthetic-source.wav',
        error='ordinary error',capture_error='capture error',recording=True,auto_process=False)
    meeting,*_=AssignmentRepository(db).export_snapshot(m['id'])
    assert meeting==db.meeting(m['id']) and meeting['error']=='capture error'
    assert not {'media_path','recording','auto_process','capture_error'} & set(meeting)


def test_v2_provider_price_routing_is_frozen_while_local_budget_switch_can_change(tmp_path):
    from secretary.domain.summary_context import FrozenSummaryContext
    db,settings,m,_,_,worker,old_job=setup(tmp_path)
    db.cancel(old_job['id'])
    worker.settings=settings.model_copy(update={'summary_input_rub_per_million':1.0,
        'summary_output_rub_per_million':2.0,'local_cost_limits_enabled':False})
    job=worker.enqueue_summary(m['id'],regenerate_summary=True)
    context=FrozenSummaryContext.model_validate(json.loads(job['payload'])['summary_context'])
    old=PolzaClient(worker.settings)
    live=PolzaClient(worker.settings.model_copy(update={'local_cost_limits_enabled':True}))
    data=[s.model_dump(mode='json') for s in context.sources]
    assert 'max_price' not in old.summary_payload(data,merge=False,context=context)['provider']
    assert old.summary_checkpoint_key(data,merge=False,context=context)==live.summary_checkpoint_key(data,merge=False,context=context)
    assert 'max_price' in live.summary_payload([{'id':'s','text':'Я подготовлю отчёт'}],merge=False)['provider']


def test_schema8_failure_rollback_and_immutable_context(tmp_path,monkeypatch):
    import secretary.infrastructure.assignment_repository as module
    old=module.ASSIGNMENT_MIGRATION
    monkeypatch.setattr(module,'ASSIGNMENT_MIGRATION',(*old,'INVALID synthetic SQL'))
    path=tmp_path/'rollback.sqlite3'
    with pytest.raises(sqlite3.OperationalError):Database(path)
    with sqlite3.connect(path) as conn:
        assert not conn.execute('SELECT 1 FROM schema_migrations WHERE version=8').fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='summary_input_contexts'").fetchone()
    monkeypatch.setattr(module,'ASSIGNMENT_MIGRATION',old)
    db=Database(path); assert db.one('SELECT MAX(version) AS v FROM schema_migrations')['v']==9
    db,_,_,_,_,_,job=setup(tmp_path/'immutable')
    payload=json.loads(job['payload']); payload['summary_context']['roster_revision']+=1
    with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),job['id']))
    with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM summary_input_contexts WHERE job_id=?',(job['id'],))


def test_regenerated_persistence_exact_carryover_is_only_suggestion_and_collision_does_not_transfer(tmp_path):
    db,_,m,p,_,worker,job=setup(tmp_path)
    first=save(db,m,job,draft('Я подготовлю отчёт'))
    repo=AssignmentRepository(db)
    command=review(repo.get(m['id']),'set_manual',p)
    confirmed=repo.review(m['id'],command).current
    exact_job=worker.enqueue_summary(m['id'],regenerate_summary=True)
    exact=save(db,m,exact_job,draft('Я подготовлю отчёт'))
    suggested=repo.get(m['id']).items[0]
    assert suggested.status=='proposed' and suggested.basis=='manual' and not suggested.confirm_eligible
    assert suggested.previous_provenance.summary_version==first['summary_version']
    changed_job=worker.enqueue_summary(m['id'],regenerate_summary=True)
    changed=validate_summary(draft('Я подготовлю отчёт'),m['id'],1,db.segments(m['id'],1)['items'])
    changed['action_items'][0].update(id=confirmed.items[0].action.action_id,text='Другая задача при прежнем ID')
    db.save_summary(changed_job,changed)
    current=repo.get(m['id']).items[0]
    assert current.previous_provenance is None and current.basis!='manual' and current.status!='confirmed'
    replay=repo.review(m['id'],command)
    assert replay.replayed and replay.receipt_stale
