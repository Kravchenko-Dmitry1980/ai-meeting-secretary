"""ASGI integration with real scoped SQLite/media, synthetic WAV and denied runtimes.

Run with scripts/run_local_asr_tests.py. No live server, model, capture or network
is started; all databases and media belong to pytest's owned synthetic scratch.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import copy
import hashlib
import io
import json
from pathlib import Path
import time
import wave

from fastapi.testclient import TestClient
import httpx
import pytest

from secretary.api import create_app
from secretary.application.local_asr_lifecycle import ExecutionPlan
from secretary.domain.speakers import AddParticipant, ReviewAttribution
from secretary.infrastructure.database import now
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.settings import Settings

FIXTURE = Path(__file__).parent / 'fixtures/local_asr/synthetic_run.json'


class NoCapture:
    def list_devices(self): return {'available':False,'devices':[],'error':'Synthetic test'}
    def state(self, meeting_id): return {'status':'stopped','recording':False}
    def close(self): pass


class NoEngine:
    def embed(self,*args,**kwargs): raise AssertionError('Model inference forbidden')


def denied_provider(*args,**kwargs):
    raise AssertionError('Provider construction forbidden in model-free integration')


def synthetic_wav():
    stream=io.BytesIO()
    with wave.open(stream,'wb') as writer:
        writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(16000)
        writer.writeframes(b'\0\0'*16000*5)
    return stream.getvalue()


def seed_meeting(app, settings):
    """Reusable synthetic fixture for the browser harness; no real identities."""
    db=app.state.db
    meeting=db.create_meeting('Synthetic local ASR integration')
    meeting_id=meeting['id']
    folder=settings.data_dir/'uploads'/meeting_id
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'synthetic.wav';path.write_bytes(synthetic_wav())
    input_sha=hashlib.sha256(path.read_bytes()).hexdigest()
    chunk=db.add_chunk(meeting_id,dict(sequence=0,channel='import',path=path,offset_ms=0,duration_ms=5000,sha256=input_sha))
    db.update_meeting(meeting_id,status='ready',transcript_version=1,duration_ms=5000,media_path=str(path))
    payload=json.loads(FIXTURE.read_text(encoding='utf-8'))
    payload['result']['input_sha256']=input_sha
    payload['artifacts']['input_sha256']=input_sha
    payload['artifacts']['audio_ref']=f'meeting:{meeting_id}'
    for ordinal,source in enumerate(payload['artifacts']['source_segments']):
        source['segment_id']=f'{meeting_id}-source-{ordinal}'
        payload['artifacts']['segment_sources'][ordinal]['source_segment_ids']=[source['segment_id']]
        db.execute("INSERT INTO segments VALUES(?,?,?,1,?,?,?,'segment',?,NULL,NULL,'import')",(
            source['segment_id'],meeting_id,chunk['id'],ordinal,source['started_ms'],source['ended_ms'],source['text']))
    stamp=now()
    db.execute('INSERT INTO meeting_transcript_routes VALUES(?,1,\'ordinary\',?,?,?)',
               (meeting_id,'{"provider":"polza","model":"openai/whisper-1"}','frozen-paid-route',stamp))
    db.execute('INSERT INTO summaries VALUES(?,1,1,?)',(meeting_id,'{"summary":"Paid summary","action_items":[{"id":"task","text":"Keep manual owner"}]}'))
    paid_job=f'{meeting_id}-paid'
    db.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload) VALUES(?,?,'summarize','uncertain',?,?,1,?)",(paid_job,meeting_id,stamp,stamp,'{"receipt":"saved-before-parse"}'))
    db.execute('INSERT INTO summary_checkpoints VALUES(?,\'immutable\',?)',(paid_job,'{"paid_checkpoint":"keep"}'))
    db.execute("INSERT INTO usage(id,meeting_id,job_id,kind,status,created_at) VALUES(?,?,?,'summary','uncertain',?)",(f'{meeting_id}-usage',meeting_id,paid_job,stamp))
    speakers=SpeakerRepository(db)
    participant=speakers.add_participant(meeting_id,AddParticipant(operation_id=f'{meeting_id}-roster',expected_roster_revision=0,display_name='Synthetic participant')).participants[0].id
    db.execute("""INSERT INTO task_assignments(meeting_id,summary_version,action_id,revision,participant_id,basis,
        source_segment_ids,evidence_quote,attribution_revision,roster_revision,status,named_owner_text,audience)
        VALUES(?,1,'manual-person',1,?,'manual',?,'Keep manual owner',0,1,'confirmed','Synthetic participant','person')""",
        (meeting_id,participant,json.dumps([payload['artifacts']['source_segments'][0]['segment_id']])))
    db.execute("""INSERT INTO task_assignments(meeting_id,summary_version,action_id,revision,participant_id,basis,
        source_segment_ids,evidence_quote,attribution_revision,roster_revision,status,named_owner_text,audience)
        VALUES(?,1,'manual-unassigned',1,NULL,'unknown','[]','Keep unassigned',0,1,'confirmed',NULL,'unassigned')""",(meeting_id,))
    return dict(meeting_id=meeting_id,payload=payload,participant_id=participant,path=path,input_sha256=input_sha)


def business_snapshot(db):
    names=('meetings','chunks','segments','meeting_transcript_routes','summaries','summary_checkpoints','usage',
           'task_assignments','speaker_attributions','attribution_runs','attribution_state','configuration')
    result={name:db.rows(f'SELECT * FROM {name} ORDER BY rowid') for name in names}
    result['paid_jobs']=db.rows("SELECT * FROM jobs WHERE stage!='local_asr' ORDER BY rowid")
    return result


@pytest.fixture
def make_api(tmp_path,monkeypatch):
    def denied(*args,**kwargs):raise AssertionError('HTTP network forbidden')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',denied)
    stack=ExitStack();created=[]
    def make(plan_factory=None):
        settings=Settings(_env_file=None,data_dir=tmp_path/f'isolated-{len(created)}',cloud_enabled=False,
                          polza_api_key='SYNTHETIC_SECRET_NOT_FOR_EXPORT',local_cost_limits_enabled=False,
                          polza_limits_enabled=False)
        app=create_app(settings,provider_factory=denied_provider,capture=NoCapture(),enrollment_capture=NoCapture(),
                       voice_engine=NoEngine(),run_worker=False,outbound_enabled=False,enforce_single_instance=False,
                       local_asr_plan_factory=plan_factory)
        client=stack.enter_context(TestClient(app,base_url='http://127.0.0.1:8765'))
        headers={'X-Secretary-Token':client.get('/api/v1/session').json()['csrf_token']}
        fixture=seed_meeting(app,settings)
        result=dict(app=app,db=app.state.db,client=client,headers=headers,settings=settings,**fixture)
        result['base']=f"/api/v1/meetings/{fixture['meeting_id']}/local-asr"
        created.append(result)
        return result
    yield make
    stack.close()


def post_import(case,payload=None,headers=None):
    return case['client'].post(case['base']+'/runs',json=payload or case['payload'],headers=headers if headers is not None else case['headers'])


def factory(calls):
    def build(meeting_id,version,input_sha,text_sha):
        calls.append((meeting_id,version,input_sha,text_sha))
        return ExecutionPlan(input_sha256=input_sha,source_text_sha256=text_sha,profile_digest='b'*64,
            model_digest='c'*64,code_digest='d'*64,asr_device='cpu',diarization_device='cuda',
            physical_gpu_id='GPU-synthetic',precision='float32',
            stages=('diarize','align_saved_text','assemble','validate'),deadline_at=time.time()+3600)
    return build


def test_http_import_read_export_manual_review_and_preserve_every_business_table(make_api):
    case=make_api();before=business_snapshot(case['db']);client=case['client'];headers=case['headers']
    response=post_import(case);assert response.status_code==201,response.text
    run=response.json();path=case['base']+'/runs/'+run['run_id']
    assert client.get(path).json()==run
    assert client.get(case['base']+'/runs').json()[0]['run_id']==run['run_id']
    exported=client.get(path+'/export')
    assert exported.json()==run and exported.headers['content-disposition']=='attachment; filename="local-asr-analysis.json"'
    assert run['result']['speaker_turns']==case['payload']['result']['speaker_turns']
    preview=client.get(path+'/clusters/Спикер 1/preview').json()
    assert preview['shown_segment_ordinals']==[0,1,2]
    command=dict(operation_id='cluster-1',expected_review_revision=0,speaker_label='Спикер 1',
                 shown_segment_ordinals=[0,1,2],excluded_segment_ordinals=[2],
                 participant_id=case['participant_id'],listening_confirmed=True)
    reviewed=client.post(path+'/cluster-reviews',json=command,headers=headers)
    assert reviewed.status_code==200,reviewed.text
    assert len(reviewed.json()['reviews'])==3
    assert client.post(path+'/cluster-reviews',json=command,headers=headers).json()==reviewed.json()
    individual=dict(operation_id='segment-1',expected_review_revision=1,segment_ordinal=0,participant_id=None,listening_confirmed=True)
    assert client.post(path+'/segment-reviews',json=individual,headers=headers).status_code==200
    exported=client.get(path+'/export').json()
    assert exported['review_revision']==2 and exported['reviews'][0]['status']=='excluded'
    assert exported['result']==run['result'] and exported['artifacts']==run['artifacts']
    assert business_snapshot(case['db'])==before
    assert 'SYNTHETIC_SECRET_NOT_FOR_EXPORT' not in json.dumps(exported)
    assert 'X-NeuroHR-ASR-Service-Token' not in json.dumps(exported)


def test_idempotence_scope_csrf_host_origin_and_no_frontend_service_token(make_api):
    case=make_api();client=case['client'];headers=case['headers']
    assert post_import(case,headers={}).status_code==403
    assert client.get(case['base']+'/runs',headers={'Host':'example.com'}).status_code==403
    assert client.get(case['base']+'/runs',headers={'Origin':'https://example.com'}).status_code==403
    run=post_import(case).json()
    assert post_import(case).json()==run
    other_payload=copy.deepcopy(case['payload']);other_payload['operation_id']='duplicate-artifact'
    assert post_import(case,other_payload).json()['run_id']==run['run_id']
    assert len(client.get(case['base']+'/runs').json())==1
    other=case['db'].create_meeting('Other scope')
    wrong=f"/api/v1/meetings/{other['id']}/local-asr/runs/{run['run_id']}"
    for suffix in ('','/export','/clusters/Спикер 1/preview'):
        assert client.get(wrong+suffix).status_code==404
    command=dict(operation_id='wrong-scope',expected_review_revision=0,segment_ordinal=0,
                 participant_id=None,listening_confirmed=True)
    assert client.post(wrong+'/segment-reviews',json=command,headers=headers).status_code==404
    public=client.get('/api/v1/config').text+client.get(case['base']+'/runs').text
    assert 'SYNTHETIC_SECRET_NOT_FOR_EXPORT' not in public
    injected=copy.deepcopy(case['payload']);injected['artifacts']['identity']['service_token']='SYNTHETIC_LOCAL_ASR_TOKEN'
    rejected=post_import(case,injected)
    assert rejected.status_code==422
    assert 'SYNTHETIC_LOCAL_ASR_TOKEN' not in rejected.text


def test_import_rejects_wrong_hash_path_stale_source_and_foreign_audio_ref(make_api):
    case=make_api();before=business_snapshot(case['db'])
    payload=copy.deepcopy(case['payload'])
    payload['result']['input_sha256']='a'*64;payload['artifacts']['input_sha256']='a'*64
    assert post_import(case,payload).status_code==409
    payload=copy.deepcopy(case['payload']);payload['artifacts']['audio_ref']='meeting:other'
    assert post_import(case,payload).status_code==409
    payload=copy.deepcopy(case['payload']);payload['transcript_version']=99
    assert post_import(case,payload).status_code==409
    case['db'].update_meeting(case['meeting_id'],media_path=str(case['settings'].data_dir/'outside.wav'))
    rejected=post_import(case)
    assert rejected.status_code==422 and rejected.json()['detail']=='local_asr_input_outside_meeting'
    assert str(case['settings'].data_dir) not in rejected.text
    case['db'].update_meeting(case['meeting_id'],media_path=str(case['path']))
    # Only fixture path edits changed updated_at; artifact import never touched business data.
    after=business_snapshot(case['db'])
    for row in after['meetings']:row['updated_at']=before['meetings'][0]['updated_at']
    assert after==before
    assert case['db'].rows('SELECT * FROM local_asr_runs')==[]


def test_import_body_limit_checks_declared_and_streamed_size_before_validation(make_api):
    case=make_api();client=case['client'];headers=case['headers']|{'Content-Type':'application/json'}
    path=case['base']+'/runs'
    declared=client.post(path,content=b'{}',headers=headers|{'Content-Length':str(16*1024*1024+1)})
    assert declared.status_code==413
    def chunks():
        for _ in range(17):yield b' '*1024*1024
    streamed=client.post(path,content=chunks(),headers=headers)
    assert streamed.status_code==413,streamed.text[:500]
    assert case['db'].rows('SELECT * FROM local_asr_runs')==[]


def test_jobs_default_disabled_and_request_cannot_supply_plan(make_api):
    case=make_api();before=business_snapshot(case['db']);client=case['client']
    body=dict(operation_id='prepare-default',transcript_version=1)
    response=client.post(case['base']+'/jobs',json=body,headers=case['headers'])
    assert response.status_code==503 and response.json()['detail']=='local_asr_profile_bundle_unavailable'
    response=client.post(case['base']+'/jobs',json=body|{'provider':'local','profile_digest':'a'*64},headers=case['headers'])
    assert response.status_code==422
    assert case['db'].rows('SELECT * FROM local_asr_executions')==[]
    assert business_snapshot(case['db'])==before


def test_jobs_prepare_202_bound_to_actual_input_idempotent_and_cancel_preserves_ready_meeting(make_api):
    calls=[];case=make_api(factory(calls));client=case['client'];before=business_snapshot(case['db'])
    body=dict(operation_id='prepare',transcript_version=1)
    response=client.post(case['base']+'/jobs',json=body,headers=case['headers'])
    assert response.status_code==202,response.text
    job=response.json();assert job['state']=='blocked' and job['job_status']=='waiting_config'
    assert job['reason']=='local_asr_inference_not_authorized'
    existing_jobs=client.get(f"/api/v1/meetings/{case['meeting_id']}/jobs")
    assert existing_jobs.status_code==200
    assert any(item['id']==job['job_id'] and item['stage']=='local_asr' for item in existing_jobs.json())
    assert calls[0][:3]==(case['meeting_id'],1,case['input_sha256'])
    replay=client.post(case['base']+'/jobs',json=body,headers=case['headers'])
    assert replay.json()['job_id']==job['job_id'] and len(calls)==1
    assert case['db'].claim() is None
    stale=client.post(case['base']+'/jobs',json=body|{'transcript_version':2},headers=case['headers'])
    assert stale.status_code==409
    wrong=case['db'].create_meeting('Other scope')
    wrong_path=f"/api/v1/meetings/{wrong['id']}/local-asr/jobs/{job['job_id']}"
    assert client.get(wrong_path).status_code==404
    assert client.post(wrong_path+'/cancel',headers=case['headers']).status_code==404
    cancelled=client.post(case['base']+'/jobs/'+job['job_id']+'/cancel',headers=case['headers'])
    assert cancelled.status_code==200 and cancelled.json()['state']=='cancelled'
    after=business_snapshot(case['db']);after['meetings']=[m for m in after['meetings'] if m['id']!=wrong['id']]
    assert after==before
    # A separate explicitly prepared analysis is also safe through the legacy cancel endpoint.
    second=client.post(case['base']+'/jobs',json=body|{'operation_id':'second'},headers=case['headers']).json()
    assert client.post('/api/v1/jobs/'+second['job_id']+'/cancel',headers=case['headers']).status_code==200
    assert case['db'].meeting(case['meeting_id'])['status']=='ready'


def test_existing_worker_defers_accidentally_queued_local_job_without_provider_or_meeting_change(make_api):
    case=make_api(factory([]));client=case['client'];before=business_snapshot(case['db'])
    job=client.post(case['base']+'/jobs',json={'operation_id':'prepare-worker','transcript_version':1},headers=case['headers']).json()
    case['db'].execute("UPDATE jobs SET status='queued' WHERE id=?",(job['job_id'],))
    assert asyncio.run(case['app'].state.worker.run_once()) is True
    state=client.get(case['base']+'/jobs/'+job['job_id']).json()
    assert state['job_status']=='waiting_config' and state['state']=='blocked'
    assert business_snapshot(case['db'])==before


def test_existing_manual_source_identity_exposed_and_excluded_from_bulk_reassignment(make_api):
    case=make_api();db=case['db'];client=case['client']
    source_id=case['payload']['artifacts']['source_segments'][1]['segment_id']
    SpeakerRepository(db).review_attribution(case['meeting_id'],ReviewAttribution(
        operation_id='old-manual-confirmation',transcript_version=1,expected_revision=0,
        changes=[{'segment_id':source_id,'participant_id':case['participant_id']}]))
    before=business_snapshot(db)
    run=post_import(case).json();path=case['base']+'/runs/'+run['run_id']
    assert run['source_decisions']==[{'source_segment_id':source_id,'participant_id':case['participant_id'],'status':'confirmed','method':'manual'}]
    preview=client.get(path+'/clusters/Спикер 1/preview').json()
    assert preview['eligible_segment_ordinals']==[0,2]
    assert preview['excluded_segments']==[{'segment_ordinal':1,'reason':'existing_manual_source_decision'}]
    payload=dict(operation_id='bad-bulk-source',expected_review_revision=0,speaker_label='Спикер 1',shown_segment_ordinals=[0,1,2],
                 excluded_segment_ordinals=[],participant_id=case['participant_id'],listening_confirmed=True)
    assert client.post(path+'/cluster-reviews',json=payload,headers=case['headers']).status_code==409
    payload.update(operation_id='good-bulk-source',excluded_segment_ordinals=[1])
    assert client.post(path+'/cluster-reviews',json=payload,headers=case['headers']).status_code==200
    payload.update(operation_id='stale-bulk-source')
    assert client.post(path+'/cluster-reviews',json=payload,headers=case['headers']).status_code==409
    assert client.get(path+'/export').json()['source_decisions']==run['source_decisions']
    assert business_snapshot(db)==before


def test_server_factory_scope_mismatch_does_not_create_job_or_change_provider(make_api):
    builder=factory([])
    def mismatched(meeting_id,version,input_sha,text_sha):
        return builder(meeting_id,version,input_sha,text_sha).model_copy(update={'owner_scope':'foreign-owner'})
    case=make_api(mismatched);before=business_snapshot(case['db'])
    response=case['client'].post(case['base']+'/jobs',json={'operation_id':'bad-plan','transcript_version':1},headers=case['headers'])
    assert response.status_code==409 and response.json()['detail']=='local_asr_profile_scope_mismatch'
    assert case['db'].rows('SELECT * FROM local_asr_executions')==[]
    assert business_snapshot(case['db'])==before
