"""Synthetic durable pipeline acceptance. No provider/device/material I/O."""
import asyncio
from dataclasses import asdict
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import struct
import threading
from types import SimpleNamespace
import wave

from fastapi.testclient import TestClient
import pytest

from secretary.api import create_app
from secretary.application.worker import Worker
from secretary.application.speakers import SpeakerService
from secretary.application.voice_matching import CandidateMaterial, CalibrationSnapshot
from secretary.domain.identification import IdentifySpeakers, BypassIdentification
from secretary.domain.speakers import AddParticipant, CreateProfile, PatchProfile, PatchParticipant, ReviewAttribution, SpeakerConflict
from secretary.domain.voice import ModelStamp, VoiceError
from secretary.infrastructure.database import Database, uid, now
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.infrastructure.voice_engine import EmbeddingBatch
from secretary.infrastructure.voice_resources import InferenceCoordinator
from secretary.settings import Settings

MODEL = ModelStamp('speechbrain/spkrec-ecapa-voxceleb','a'*40,'b'*64)
VECTOR = (1.0,)+(0.0,)*191


def waveform(seconds=2):
    out=io.BytesIO()
    with wave.open(out,'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b''.join(struct.pack('<h',int(5000*math.sin(i/15))) for i in range(seconds*16000)))
    return out.getvalue()


class Engine:
    def __init__(self):
        self.calls=0
        self.hook=None
        self.fail=None

    def embed(self,clips,*,cancel=None):
        self.calls+=1
        if self.hook:
            self.hook(cancel)
        if self.fail:
            raise VoiceError(self.fail)
        return EmbeddingBatch(MODEL,tuple(VECTOR for _ in clips),0)


class Materials:
    def __init__(self,engine):
        self.engine=engine
        self.reads=[]

    def model(self):
        return MODEL

    def read_candidate(self,profile_id,enrollment_id,**kwargs):
        self.reads.append(enrollment_id)
        return CandidateMaterial(profile_id,kwargs['expected_revision'],MODEL,(VECTOR,))


class Cloud:
    def __init__(self):
        self.posts=0; self.polls=0; self.llm=0; self.factories=0; self.models=[]

    def factory(self,settings):
        self.factories+=1
        self.models.append(settings.stt_model)
        cloud=self
        class Provider:
            async def transcribe(self,path,*,offset_ms=0,channel='import',provider_job_id=None,on_provider_job=None):
                if provider_job_id: cloud.polls+=1
                else: cloud.posts+=1
                return {'segments':[{'text':'Synthetic speech','start_ms':offset_ms,'end_ms':offset_ms+2000,'timing_precision':'segment','speaker_label':'speaker_0'}],
                        'usage':{'confirmed_rub':.01}}
            async def summarize(self,segments,*,before_request=None,on_usage=None):
                cloud.llm+=1
                if before_request: await before_request('summary',200,500)
                if on_usage: await on_usage({'confirmed_rub':.01})
                return {'overview':'Synthetic','readable_transcript':'Synthetic','decisions':[],'action_items':[],'open_questions':[]}
        return Provider()


@pytest.fixture
def ctx(tmp_path):
    settings=Settings(_env_file=None,data_dir=tmp_path/'data',project_dir=tmp_path,polza_api_key='synthetic',cloud_enabled=True,local_cost_limits_enabled=False,stt_price_rub_per_minute=7)
    db=Database(settings.data_dir/'test.sqlite3')
    engine=Engine(); materials=Materials(engine); cloud=Cloud(); slot=InferenceCoordinator()
    worker=Worker(db,settings,cloud.factory,enrollments=materials,coordinator=slot)
    return SimpleNamespace(db=db,settings=settings,engine=engine,materials=materials,cloud=cloud,slot=slot,worker=worker,speakers=SpeakerService(SpeakerRepository(db)))


def meeting(ctx,mode='voice_identification',chunks=1):
    m=ctx.db.create_meeting('Synthetic',mode)
    for n in range(chunks):
        folder=ctx.settings.data_dir/'audio'/m['id']; folder.mkdir(parents=True,exist_ok=True)
        path=folder/f'{n}.wav'; path.write_bytes(waveform())
        ctx.db.add_chunk(m['id'],{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'sequence':n,'channel':'import','offset_ms':n*2000,'duration_ms':2000})
    return m['id']


def profile(ctx,m,name='Synthetic'):
    p=ctx.speakers.create_profile(CreateProfile(display_name=name,operation_id=uid()))
    roster=ctx.speakers.get_roster(m)
    r=ctx.speakers.add_participant(m,AddParticipant(person_profile_id=p.id,expected_roster_revision=roster.roster_revision,operation_id=uid()))
    return p,r.participants[-1]


def ready(ctx,p,version=1):
    ident=uid()
    ctx.db.execute("INSERT INTO voice_enrollments(id,person_profile_id,material_version,revision,consent_confirmed,status,model_id,model_revision,created_at,listening_confirmed,single_speaker_confirmed) VALUES(?,?,?,2,1,'ready',?,?,?,1,1)", (ident,p.id,version,MODEL.model_id,MODEL.revision,now()))
    return ident


def transcribe(ctx,m):
    ctx.worker.enqueue_transcription(m)
    for _ in ctx.db.chunks(m):
        assert asyncio.run(ctx.worker.run_once())
    return ctx.db.one("SELECT * FROM jobs WHERE meeting_id=? AND stage='identify_speakers'",(m,))


def command(ctx,m,**kwargs):
    return IdentifySpeakers(transcript_version=ctx.db.meeting(m)['transcript_version'],expected_revision=ctx.speakers.get_attribution(m).revision,operation_id=uid(),**kwargs)


def test_routes_pipeline_and_ordinary_remain_distinct(ctx):
    ordinary=meeting(ctx,'ordinary')
    transcribe(ctx,ordinary)
    assert [j['stage'] for j in ctx.db.jobs(ordinary)]==['transcribe','summarize']
    asyncio.run(ctx.worker.run_once())
    voice=meeting(ctx); local=transcribe(ctx,voice)
    assert ctx.cloud.posts==2 and ctx.cloud.llm==1
    asyncio.run(ctx.worker.run_once())
    assert [j['stage'] for j in ctx.db.jobs(voice)]==['transcribe','identify_speakers','summarize']
    assert ctx.engine.calls==0 and ctx.cloud.factories==3
    assert ctx.db.job(local['id'])['status']=='succeeded'
    payload=json.loads(ctx.db.one("SELECT payload FROM jobs WHERE meeting_id=? AND stage='summarize'",(voice,))['payload'])
    assert payload['attribution_revision']==1 and payload['attribution_run_id']
    asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.llm==2


def test_voice_route_never_inherits_whisper_price_and_rebinding_is_frozen(ctx):
    m=meeting(ctx)
    version,route=ctx.worker.routes.bind(m)
    assert version==1 and route['settings']=={'stt_model':'aiesa/transcribe','stt_price_rub_per_minute':None}
    ctx.settings.stt_model='other/model'; ctx.settings.stt_price_rub_per_minute=99
    assert ctx.worker.routes.bind(m)==(version,route)
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.execute("UPDATE meetings SET processing_mode='ordinary' WHERE id=?",(m,))
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.execute("UPDATE meeting_transcript_routes SET route='{}' WHERE meeting_id=?",(m,))
    m2=meeting(ctx,'ordinary')
    assert ctx.worker.routes.bind(m2)[1]['settings']['stt_model']=='other/model'


def test_same_model_price_is_frozen(ctx):
    ctx.settings.stt_model='aiesa/transcribe'
    m=meeting(ctx)
    assert ctx.worker.routes.bind(m)[1]['settings']['stt_price_rub_per_minute']==7


def test_local_has_no_cloud_gate_or_usage_and_retains_uncalibrated_reviews(ctx):
    m=meeting(ctx); p,participant=profile(ctx,m); ready(ctx,p)
    local=transcribe(ctx,m)
    count=ctx.cloud.factories; usage=len(ctx.db.rows('SELECT * FROM usage'))
    ctx.settings.cloud_enabled=False; ctx.settings.polza_api_key=''
    assert asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.factories==count and len(ctx.db.rows('SELECT * FROM usage'))==usage
    snapshot=ctx.speakers.get_attribution(m)
    assert snapshot.revision==1
    item=snapshot.items[0]
    assert item.participant_id is None and item.status=='unknown' and 'uncalibrated' in item.reason_codes
    assert item.review_candidates[0].participant_id==participant.id and item.review_candidates[0].raw_score==1
    state=ctx.worker.attribution.state(m,json.loads(local['payload'])['run_id'])
    assert state.outcome=='completed' and state.progress['observations_processed']==1 and state.progress['inference_batches']==1
    public=state.model_dump_json()+snapshot.model_dump_json()+json.dumps(ctx.db.jobs(m))
    for forbidden in ('vectors','storage_key','pcm16',str(ctx.settings.data_dir)):
        assert forbidden not in public


def test_latest_ready_per_profile_reads_once_but_all_materials_are_captured(ctx):
    m=meeting(ctx); p,_=profile(ctx,m); old=ready(ctx,p); latest=ready(ctx,p,2)
    transcribe(ctx,m); asyncio.run(ctx.worker.run_once())
    assert ctx.materials.reads==[latest]
    snapshot=json.loads(ctx.worker.attribution.current(m,1)['snapshot'])
    assert {r['id'] for r in snapshot['materials']}=={old,latest}


@pytest.mark.parametrize('edit',['manual','manual_unset','roster','profile','revoke','audio','version'])
def test_late_result_whole_run_stale_or_cancelled_without_manual_overwrite(ctx,edit):
    m=meeting(ctx); p,participant=profile(ctx,m); enrollment=ready(ctx,p); local=transcribe(ctx,m)
    segment=ctx.db.segments(m)['items'][0]['id']
    before_text=ctx.db.segments(m)['items']
    def hook(cancel):
        if edit in ('manual','manual_unset'):
            ctx.speakers.review_attribution(m,ReviewAttribution(transcript_version=1,expected_revision=0,operation_id=uid(),changes=[{'segment_id':segment,'participant_id':participant.id if edit=='manual' else None}]))
        elif edit=='roster':
            ctx.speakers.patch_participant(m,participant.id,PatchParticipant(display_name='Renamed',expected_roster_revision=1,operation_id=uid()))
        elif edit=='profile':
            ctx.speakers.patch_profile(p.id,PatchProfile(display_name='Renamed',expected_revision=0,operation_id=uid()))
        elif edit=='revoke':
            ctx.db.execute("UPDATE voice_enrollments SET consent_confirmed=0,status='revoked',revision=3 WHERE id=?",(enrollment,))
            ctx.slot.cancel_material(enrollment)
        elif edit=='audio':
            path=Path(ctx.db.chunks(m)[0]['path']); data=bytearray(path.read_bytes()); data[-2:]=b'zz'; path.write_bytes(data)
        elif edit=='version':
            ctx.db.update_meeting(m,transcript_version=2)
    ctx.engine.hook=hook
    asyncio.run(ctx.worker.run_once())
    intent=ctx.db.one('SELECT * FROM identification_intents WHERE job_id=?',(local['id'],))
    assert intent['outcome'] in ('stale','cancelled')
    assert ctx.db.one("SELECT 1 FROM jobs WHERE meeting_id=? AND stage='summarize'",(m,)) is None
    assert ctx.db.segments(m,1)['items']==before_text
    if edit in ('manual','manual_unset'):
        attribution=ctx.speakers.get_attribution(m)
        assert attribution.revision==1 and attribution.items[0].method=='manual'
        assert attribution.items[0].participant_id==(participant.id if edit=='manual' else None)


def test_existing_manual_unset_survives_new_local_publication(ctx):
    m=meeting(ctx); p,participant=profile(ctx,m); ready(ctx,p)
    transcribe(ctx,m)
    segment=ctx.db.segments(m)['items'][0]['id']
    ctx.speakers.review_attribution(m,ReviewAttribution(transcript_version=1,expected_revision=0,operation_id=uid(),changes=[{'segment_id':segment,'participant_id':None}]))
    ctx.db.cancel(ctx.worker.attribution.current(m,1)['job_id'])
    result=ctx.worker.enqueue_identification(m,command(ctx,m))
    asyncio.run(ctx.worker.run_once())
    attribution=ctx.speakers.get_attribution(m)
    assert attribution.revision==2 and attribution.items[0].method=='manual' and attribution.items[0].reason_codes==['manual_unset']
    assert ctx.db.job(result.job_id)['status']=='succeeded'
    assert ctx.cloud.llm==0  # explicit local rerun never queues summary


def test_same_operation_replay_after_publish_no_new_inference_or_cloud(ctx):
    m=meeting(ctx); transcribe(ctx,m); asyncio.run(ctx.worker.run_once()); asyncio.run(ctx.worker.run_once())
    cmd=command(ctx,m); first=ctx.worker.enqueue_identification(m,cmd)
    asyncio.run(ctx.worker.run_once())
    snapshot=ctx.db.segments(m)['items']; count=ctx.cloud.factories
    assert ctx.worker.enqueue_identification(m,cmd)==first
    assert asyncio.run(ctx.worker.run_once()) is False
    assert ctx.cloud.factories==count and ctx.db.segments(m)['items']==snapshot and ctx.cloud.llm==1
    with pytest.raises(SpeakerConflict):
        ctx.worker.enqueue_identification(m,cmd.model_copy(update={'expected_revision':5}))


def test_new_revision_intent_does_not_reuse_old_active_job(ctx):
    m=meeting(ctx); p,participant=profile(ctx,m); ready(ctx,p); first=transcribe(ctx,m)
    ctx.db.execute("UPDATE jobs SET status='running' WHERE id=?",(first['id'],))
    ctx.speakers.patch_profile(p.id,PatchProfile(display_name='Edited',expected_revision=0,operation_id=uid()))
    second=ctx.worker.enqueue_identification(m,command(ctx,m))
    assert second.job_id!=first['id'] and ctx.db.job(first['id'])['status']=='running'
    assert len(ctx.db.rows('SELECT * FROM identification_intents'))==2


def test_identical_parallel_intents_one_job(ctx):
    from concurrent.futures import ThreadPoolExecutor
    m=meeting(ctx); transcribe(ctx,m)
    def capture(_): return ctx.worker.attribution.capture(m,1,0)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(capture,range(2)))
    assert results[0]==results[1] and len(ctx.db.rows('SELECT * FROM identification_intents'))==1
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        ctx.db.execute("UPDATE identification_intents SET snapshot='{}'")


@pytest.mark.parametrize('when',['queued','engine'])
def test_local_cancellation_and_restart_never_reposts_stt(ctx,when):
    m=meeting(ctx); p,_=profile(ctx,m); ready(ctx,p); local=transcribe(ctx,m)
    if when=='queued': ctx.db.cancel(local['id'])
    else: ctx.engine.hook=lambda cancel: ctx.db.cancel(local['id'])
    asyncio.run(ctx.worker.run_once())
    assert ctx.engine.calls==(0 if when=='queued' else 1)
    cmd=command(ctx,m,retry=True)
    ctx.engine.hook=None
    result=ctx.worker.enqueue_identification(m,cmd)
    assert result.job_id==local['id']
    asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.posts==1 and ctx.cloud.llm==0 and ctx.db.job(local['id'])['status']=='succeeded'


def test_running_local_restart_same_intent_and_completed_continue_no_loop(ctx):
    m=meeting(ctx); local=transcribe(ctx,m)
    ctx.db.execute("UPDATE jobs SET status='running' WHERE id=?",(local['id'],))
    worker=Worker(ctx.db,ctx.settings,ctx.cloud.factory,enrollments=ctx.materials,coordinator=ctx.slot)
    assert ctx.db.job(local['id'])['status']=='queued'
    asyncio.run(worker.run_once()); asyncio.run(worker.run_once())
    worker.enqueue_transcription(m,resume=True)
    assert len(ctx.db.rows('SELECT * FROM identification_intents'))==1 and ctx.cloud.posts==1 and ctx.cloud.llm==1


def test_unavailable_requires_explicit_idempotent_bypass(ctx):
    m=meeting(ctx); p,_=profile(ctx,m); ready(ctx,p); local=transcribe(ctx,m)
    ctx.engine.fail='runtime_missing'
    asyncio.run(ctx.worker.run_once())
    assert ctx.db.job(local['id'])['status']=='failed' and ctx.worker.attribution.current(m,1)['outcome']=='runtime_unavailable'
    assert ctx.db.segments(m)['total']==1 and not ctx.db.rows("SELECT * FROM jobs WHERE stage='summarize'")
    with pytest.raises(ValueError,match='identification_required'): ctx.worker.enqueue_summary(m)
    bypass=BypassIdentification(transcript_version=1,expected_revision=0,operation_id=uid())
    result=ctx.worker.attribution.bypass(m,bypass)
    assert result.outcome=='bypassed' and ctx.worker.attribution.bypass(m,bypass)==result
    ctx.worker.maybe_summary(m,1); asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.posts==1 and ctx.cloud.llm==1 and ctx.engine.calls==1


def test_unknown_stage_never_falls_through_to_cloud(ctx):
    m=meeting(ctx)
    job=ctx.db.enqueue(m,'bogus')
    asyncio.run(ctx.worker.run_once())
    assert ctx.db.job(job['id'])['status']=='failed' and ctx.cloud.factories==0 and not ctx.db.rows('SELECT * FROM usage')


@pytest.mark.parametrize('legacy',['consistent','missing','conflict','unattempted'])
def test_explicit_legacy_binding_is_safe(ctx,legacy):
    m=meeting(ctx,'ordinary',chunks=2); version=ctx.db.ensure_version(m)
    for n,c in enumerate(ctx.db.chunks(m)):
        settings={'stt_model':'aiesa/transcribe','stt_price_rub_per_minute':None}
        if legacy=='conflict' and n: settings['stt_model']='other/model'
        job=ctx.db.enqueue(m,'transcribe',chunk_id=c['id'],version=version,payload=None if legacy in ('missing','unattempted') else {'settings':settings})
        if legacy!='unattempted': ctx.db.execute("UPDATE jobs SET status='uncertain',attempts=1,provider_job_id='synthetic-accepted' WHERE id=?",(job['id'],))
    if legacy in ('missing','conflict'):
        with pytest.raises(ValueError,match='legacy_route_needs_review'): ctx.worker.enqueue_transcription(m,resume=True)
        assert not ctx.db.rows('SELECT * FROM meeting_transcript_routes')
    else:
        ctx.worker.enqueue_transcription(m,resume=True)
        assert ctx.worker.routes.get(m,1)['settings']['stt_model']==('aiesa/transcribe' if legacy=='consistent' else ctx.settings.stt_model)
    assert ctx.cloud.posts==0 and ctx.cloud.polls==0


def test_calibrated_proposed_only_and_revoked_overlay_requires_review(ctx):
    m=meeting(ctx); p,participant=profile(ctx,m); e=ready(ctx,p); other,_=profile(ctx,m,'Other'); ready(ctx,other)
    original=ctx.materials.read_candidate
    def candidate(pid,eid,**kwargs):
        material=original(pid,eid,**kwargs)
        return material if pid==p.id else CandidateMaterial(pid,material.material_revision,MODEL,((0.,1.)+(0.,)*190,))
    ctx.materials.read_candidate=candidate
    ctx.worker.attribution.calibration=CalibrationSnapshot('synthetic-only','synthetic-labels',MODEL,.7,.2)
    transcribe(ctx,m); asyncio.run(ctx.worker.run_once())
    snapshot=ctx.speakers.get_attribution(m)
    assert snapshot.items[0].participant_id==participant.id and snapshot.items[0].status=='proposed'
    ctx.db.execute("UPDATE voice_enrollments SET status='revoked',consent_confirmed=0,revision=3 WHERE id=?",(e,))
    stale=ctx.speakers.get_attribution(m)
    assert stale.automatic_overlay_stale and stale.items[0].participant_id==participant.id
    assert not ctx.worker.attribution.barrier(m,1)


def test_schema7_atomic_failure_reopen_and_private_data_unchanged(ctx,monkeypatch):
    from secretary.infrastructure import database
    path=ctx.settings.data_dir/'schema6.sqlite3'
    # Existing approved schema6 generated without executing the new migration.
    original=Database._migrate_identification
    monkeypatch.setattr(Database,'_migrate_identification',lambda self:None)
    old=Database(path)
    old.execute("INSERT INTO meetings(id,title,status,created_at,updated_at) VALUES('old','keep','ready',?,?)",(now(),now()))
    old.execute("INSERT INTO configuration VALUES('local_cost_limits_enabled','false')")
    monkeypatch.setattr(Database,'_migrate_identification',original)
    statements=database.IDENTIFICATION_MIGRATION
    monkeypatch.setattr(database,'IDENTIFICATION_MIGRATION',(*statements[:3],'INVALID SQL',*statements[3:]))
    with pytest.raises(sqlite3.OperationalError): Database(path)
    with old.connection() as conn:
        assert not conn.execute('SELECT 1 FROM schema_migrations WHERE version=7').fetchone()
        assert 'processing_mode' not in {r[1] for r in conn.execute('PRAGMA table_info(meetings)')}
        assert conn.execute("SELECT title FROM meetings WHERE id='old'").fetchone()[0]=='keep'
    monkeypatch.setattr(database,'IDENTIFICATION_MIGRATION',statements)
    migrated=Database(path); reopened=Database(path)
    assert reopened.meeting('old')['processing_mode']=='ordinary' and reopened.configuration()['local_cost_limits_enabled'] is False
    with migrated.connection() as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not conn.execute('PRAGMA foreign_key_check').fetchall()


class Capture:
    def __init__(self): self.on_chunk=None
    def state(self,identifier): return {'recording':False,'status':'stopped'}
    def start(self,identifier,mic,system,on_chunk,on_error,on_finish=None): self.on_chunk=on_chunk; return {'recording':True,'status':'recording'}
    def stop(self,identifier): return {'recording':False,'status':'stopped'}
    def close(self): pass


@pytest.mark.parametrize('auto',[True,False])
def test_live_route_bound_before_start_including_auto_false_and_settings_change(tmp_path,auto):
    capture=Capture()
    settings=Settings(_env_file=None,data_dir=tmp_path/'data',project_dir=tmp_path,polza_api_key='',cloud_enabled=False,local_cost_limits_enabled=False)
    app=create_app(settings,capture=capture,enrollment_capture=Capture(),run_worker=False)
    with TestClient(app,base_url='http://127.0.0.1:8765') as api:
        headers={'X-Secretary-Token':api.get('/api/v1/session').json()['csrf_token']}
        m=api.post('/api/v1/meetings',json={'title':'Live'},headers=headers).json()['id']
        assert api.post(f'/api/v1/meetings/{m}/recording/start',json={'auto_process':auto},headers=headers).status_code==200
        assert api.get(f'/api/v1/meetings/{m}').json()['transcript_version']==1
        assert api.patch('/api/v1/config',json={'stt_model':'other/model'},headers=headers).status_code==200
        folder=settings.data_dir/'audio'/m; folder.mkdir(parents=True,exist_ok=True); path=folder/'0.wav'; path.write_bytes(waveform())
        capture.on_chunk({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'sequence':0,'channel':'microphone','offset_ms':0,'duration_ms':2000})
        if not auto: api.post(f'/api/v1/meetings/{m}/process',json={'stage':'transcribe'},headers=headers)
        job=app.state.db.one("SELECT payload FROM jobs WHERE stage='transcribe'")
        assert json.loads(job['payload'])['settings']['stt_model']=='openai/whisper-large-v3-turbo'


def test_api_local_fields_security_and_202_without_inference(tmp_path):
    settings=Settings(_env_file=None,data_dir=tmp_path/'data',project_dir=tmp_path,polza_api_key='',cloud_enabled=False,local_cost_limits_enabled=False)
    app=create_app(settings,capture=Capture(),enrollment_capture=Capture(),run_worker=False,voice_model=MODEL)
    with TestClient(app,base_url='http://127.0.0.1:8765') as api:
        headers={'X-Secretary-Token':api.get('/api/v1/session').json()['csrf_token']}
        m=api.post('/api/v1/meetings',json={'processing_mode':'voice_identification'},headers=headers).json()['id']
        for body in ({'stage':'bogus'},{'stage':'identify_speakers'},{'stage':'summarize','expected_revision':0},{'stage':'identify_speakers','transcript_version':True,'expected_revision':0,'operation_id':'a'}):
            assert api.post(f'/api/v1/meetings/{m}/process',json=body,headers=headers).status_code==422
        assert api.post(f'/api/v1/meetings/{m}/identification-bypass',json={'transcript_version':0,'expected_revision':0,'operation_id':'x'}).status_code==403
        schema=app.openapi(); assert 'identify_speakers' in schema['components']['schemas']['ProcessingJob']['properties']['stage']['enum']
        response=api.post(f'/api/v1/meetings/{m}/upload',files={'file':('synthetic.wav',waveform(),'audio/wav')},headers=headers)
        assert response.status_code==202 and app.state.db.meeting(m)['transcript_version']==1
        assert app.state.worker.routes.get(m,1)['settings']['stt_model']=='aiesa/transcribe'


def test_api_local_receipt_replays_202_after_terminal_and_progress_is_discoverable(tmp_path):
    settings=Settings(_env_file=None,data_dir=tmp_path/'data',project_dir=tmp_path,polza_api_key='',cloud_enabled=False,local_cost_limits_enabled=False)
    app=create_app(settings,capture=Capture(),enrollment_capture=Capture(),run_worker=False,voice_model=MODEL)
    with TestClient(app,base_url='http://127.0.0.1:8765') as api:
        headers={'X-Secretary-Token':api.get('/api/v1/session').json()['csrf_token']}
        m=api.post('/api/v1/meetings',json={'processing_mode':'voice_identification'},headers=headers).json()['id']
        app.state.worker.routes.bind(m)
        folder=settings.data_dir/'audio'/m; folder.mkdir(parents=True,exist_ok=True); path=folder/'0.wav'; path.write_bytes(waveform())
        chunk=app.state.db.add_chunk(m,{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'sequence':0,'channel':'import','offset_ms':0,'duration_ms':2000})
        job=app.state.db.enqueue(m,'transcribe',chunk_id=chunk['id'],version=1,payload={'settings':app.state.worker.routes.get(m,1)['settings']})
        app.state.db.finish_job(job['id'],'succeeded')
        body={'stage':'identify_speakers','transcript_version':1,'expected_revision':0,'operation_id':uid()}
        first=api.post(f'/api/v1/meetings/{m}/process',json=body,headers=headers)
        assert first.status_code==202
        app.state.db.cancel(first.json()['job_id'])
        replay=api.post(f'/api/v1/meetings/{m}/process',json=body,headers=headers)
        assert replay.status_code==202 and replay.json()==first.json()
        local=[j for j in api.get(f'/api/v1/meetings/{m}/jobs').json() if j['stage']=='identify_speakers'][0]
        assert local['run_id'] and local['local_outcome']=='cancelled'
        state=api.get(f'/api/v1/meetings/{m}/identification-runs/{local["run_id"]}').json()
        assert state['outcome']=='cancelled' and state['job_id']==first.json()['job_id']
        body['operation_id']='\ud800'
        assert api.post(f'/api/v1/meetings/{m}/process',content=json.dumps(body),headers={**headers,'content-type':'application/json'}).status_code==422


def test_bypass_preserves_manual_and_clears_previous_automatic_identity(ctx):
    m=meeting(ctx); p,participant=profile(ctx,m); ready(ctx,p); other,_=profile(ctx,m,'Other'); ready(ctx,other)
    original=ctx.materials.read_candidate
    ctx.materials.read_candidate=lambda pid,eid,**kw: original(pid,eid,**kw) if pid==p.id else CandidateMaterial(pid,kw['expected_revision'],MODEL,((0.,1.)+(0.,)*190,))
    ctx.worker.attribution.calibration=CalibrationSnapshot('synthetic','labels',MODEL,.7,.2)
    transcribe(ctx,m); asyncio.run(ctx.worker.run_once())
    assert ctx.speakers.get_attribution(m).items[0].participant_id==participant.id
    # This is an explicit local retry after a successful automatic overlay.
    ctx.db.execute("UPDATE jobs SET status='cancelled' WHERE stage='summarize'")
    local=ctx.worker.enqueue_identification(m,command(ctx,m))
    ctx.engine.fail='runtime_missing'; asyncio.run(ctx.worker.run_once())
    assert ctx.db.job(local.job_id)['status']=='failed'
    result=ctx.worker.attribution.bypass(m,BypassIdentification(transcript_version=1,expected_revision=1,operation_id=uid()))
    active=ctx.speakers.get_attribution(m)
    assert result.attribution_revision==2 and active.items[0].participant_id is None and active.items[0].method=='unknown'
    assert len(ctx.db.rows("SELECT * FROM attribution_runs WHERE status='published'"))==2


@pytest.mark.parametrize('window',['stt','local'])
def test_crash_handoff_reconciles_exact_new_mode_without_recompute_or_post(ctx,monkeypatch,window):
    m=meeting(ctx)
    original=ctx.worker.maybe_summary
    if window=='stt':
        monkeypatch.setattr(ctx.worker,'maybe_summary',lambda *args:(_ for _ in ()).throw(RuntimeError('synthetic handoff failure')))
        ctx.worker.enqueue_transcription(m); asyncio.run(ctx.worker.run_once())
        assert not ctx.db.rows('SELECT * FROM identification_intents')
    else:
        transcribe(ctx,m)
        monkeypatch.setattr(ctx.worker,'maybe_summary',lambda *args:(_ for _ in ()).throw(RuntimeError('synthetic handoff failure')))
        asyncio.run(ctx.worker.run_once())
        assert ctx.worker.attribution.current(m,1)['outcome']=='completed'
    monkeypatch.setattr(ctx.worker,'maybe_summary',original)
    while asyncio.run(ctx.worker.run_once()): pass
    assert ctx.cloud.posts==1 and ctx.cloud.llm==1 and len(ctx.db.rows('SELECT * FROM identification_intents'))==1


def test_durable_capture_failure_rolls_back_run_job_and_intent(ctx,monkeypatch):
    m=meeting(ctx)
    # Commit raw STT while suppressing orchestration, then fail the operation receipt.
    monkeypatch.setattr(ctx.worker,'maybe_summary',lambda *args:None)
    ctx.worker.enqueue_transcription(m); asyncio.run(ctx.worker.run_once())
    cmd=command(ctx,m)
    monkeypatch.setattr(ctx.worker.attribution.speakers,'_receipt',lambda *args:(_ for _ in ()).throw(RuntimeError('synthetic receipt failure')))
    with pytest.raises(RuntimeError): ctx.worker.enqueue_identification(m,cmd)
    assert not ctx.db.rows('SELECT * FROM attribution_runs') and not ctx.db.rows('SELECT * FROM identification_intents')
    assert [j['stage'] for j in ctx.db.jobs(m)]==['transcribe'] and ctx.cloud.posts==1


def test_publication_transaction_failure_has_no_partial_active_overlay(ctx):
    m=meeting(ctx); local=transcribe(ctx,m)
    ctx.db.execute("CREATE TRIGGER synthetic_fail_state BEFORE INSERT ON attribution_state BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    asyncio.run(ctx.worker.run_once())
    assert ctx.db.job(local['id'])['status']=='failed'
    assert not ctx.db.rows('SELECT * FROM speaker_attributions') and not ctx.db.rows('SELECT * FROM attribution_state')
    assert ctx.worker.attribution.current(m,1)['outcome']=='error' and ctx.cloud.posts==1 and ctx.cloud.llm==0


def test_second_simultaneous_base_revision_run_is_stale_after_first_wins(ctx):
    m=meeting(ctx); local=transcribe(ctx,m)
    # A distinct calibration revision yields a distinct intent on the same attribution base.
    ctx.worker.attribution.calibration=CalibrationSnapshot('synthetic-alt','labels',MODEL,.7,.2)
    second=ctx.worker.enqueue_identification(m,command(ctx,m))
    ctx.worker.attribution.calibration=None
    asyncio.run(ctx.worker.run_once())
    assert ctx.speakers.get_attribution(m).revision==1
    ctx.worker.attribution.calibration=CalibrationSnapshot('synthetic-alt','labels',MODEL,.7,.2)
    asyncio.run(ctx.worker.run_once())
    assert ctx.speakers.get_attribution(m).revision==1 and ctx.db.job(second.job_id)['status']=='failed'
    assert ctx.db.one('SELECT outcome FROM identification_intents WHERE job_id=?',(second.job_id,))['outcome']=='stale'


def test_restart_with_running_cancel_intent_is_cancelled_and_explicit_retry_reuses_job(ctx):
    m=meeting(ctx); local=transcribe(ctx,m)
    ctx.db.execute("UPDATE jobs SET status='running',cancel_requested=1 WHERE id=?",(local['id'],))
    ctx.db.recover()
    assert ctx.db.job(local['id'])['status']=='cancelled' and ctx.worker.attribution.current(m,1)['outcome']=='cancelled'
    result=ctx.worker.enqueue_identification(m,command(ctx,m,retry=True))
    assert result.job_id==local['id'] and ctx.db.job(local['id'])['status']=='queued'
    asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.posts==1 and ctx.cloud.llm==0


@pytest.mark.parametrize('source',['missing','changed'])
def test_original_chunk_loss_or_hash_change_never_claims_identity(ctx,source):
    m=meeting(ctx); p,_=profile(ctx,m); ready(ctx,p); local=transcribe(ctx,m)
    path=Path(ctx.db.chunks(m)[0]['path'])
    if source=='missing': path.unlink()
    else: path.write_bytes(waveform(1))
    asyncio.run(ctx.worker.run_once())
    if source=='missing':
        item=ctx.speakers.get_attribution(m).items[0]
        assert item.participant_id is None and item.reason_codes==['audio_missing']
    else:
        assert ctx.worker.attribution.current(m,1)['outcome']=='stale' and ctx.speakers.get_attribution(m).revision==0
    assert ctx.engine.calls==0 and ctx.cloud.posts==1


def test_cancel_after_compute_before_publication_keeps_snapshot_unpublished(ctx):
    from secretary.infrastructure.meeting_voice_audio import MeetingAudioReader
    m=meeting(ctx); p,_=profile(ctx,m); ready(ctx,p); local=transcribe(ctx,m)
    class Reader(MeetingAudioReader):
        def revalidate(self,*,cancel=None):
            ctx.db.cancel(local['id'])
            return super().revalidate(cancel=cancel)
    ctx.worker.attribution.reader_factory=Reader
    asyncio.run(ctx.worker.run_once())
    assert ctx.engine.calls==1 and ctx.worker.attribution.current(m,1)['outcome']=='cancelled'
    assert not ctx.db.rows('SELECT * FROM attribution_state') and ctx.cloud.llm==0


def test_shared_busy_coordinator_blocks_local_without_cloud_or_second_engine(ctx):
    m=meeting(ctx); p,_=profile(ctx,m); ready(ctx,p); local=transcribe(ctx,m)
    with ctx.slot.slot('synthetic-enrollment',threading.Event()):
        asyncio.run(ctx.worker.run_once())
    assert ctx.engine.calls==0 and ctx.worker.attribution.current(m,1)['reasons']=='["inference_busy"]'
    assert ctx.cloud.posts==1 and ctx.cloud.llm==0


def test_long_fake_inference_runs_off_event_loop_and_close_awaits_owned_cleanup(ctx):
    m=meeting(ctx); p,_=profile(ctx,m); ready(ctx,p); transcribe(ctx,m)
    started=threading.Event(); finished=threading.Event()
    def block(cancel):
        started.set()
        while not cancel.is_set(): finished.wait(.01)
        finished.set()
    ctx.engine.hook=block
    async def exercise():
        work=asyncio.create_task(ctx.worker.run_once())
        await asyncio.wait_for(asyncio.to_thread(started.wait),2)
        # A heartbeat remains schedulable while the synthetic synchronous engine blocks.
        await asyncio.wait_for(asyncio.sleep(.02),.2)
        close=asyncio.create_task(ctx.worker.close())
        await asyncio.wait_for(close,2)
        await work
        assert finished.is_set()
    asyncio.run(exercise())
    assert ctx.worker.attribution.current(m,1)['outcome']=='cancelled' and ctx.cloud.posts==1 and ctx.cloud.llm==0


def test_default_continue_polls_accepted_saved_route_despite_global_change(ctx):
    m=meeting(ctx,'ordinary')
    ctx.settings.stt_model='aiesa/transcribe'
    job=ctx.worker.enqueue_transcription(m)
    ctx.db.execute("UPDATE jobs SET provider_job_id='synthetic-accepted',status='cancelled' WHERE id=?",(job['id'],))
    ctx.settings.stt_model='openai/whisper-1'
    ctx.worker.enqueue_transcription(m,retry=True,resume=True)
    asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.posts==0 and ctx.cloud.polls==1 and ctx.cloud.models==['aiesa/transcribe']


@pytest.mark.parametrize('stopped',['cancelled','failed','waiting_config','paused_budget','uncertain','succeeded'])
def test_first_summary_handoff_preserves_every_terminal_or_stopped_paid_job(ctx,stopped):
    m=meeting(ctx); transcribe(ctx,m); asyncio.run(ctx.worker.run_once())
    summary=ctx.db.one("SELECT * FROM jobs WHERE stage='summarize'")
    ctx.db.execute("INSERT INTO summary_checkpoints VALUES(?,?,?)",(summary['id'],'map:0','{"receipt":"synthetic-paid"}'))
    ctx.db.execute('INSERT INTO usage(id,meeting_id,job_id,kind,estimated_rub,confirmed_rub,status,provider_request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (uid(),m,summary['id'],'summary',.1,.1,'confirmed','synthetic-map-receipt',now()))
    receipts=ctx.db.rows('SELECT * FROM usage ORDER BY id')
    ctx.db.execute("UPDATE jobs SET status=?,attempts=3,error='stopped',payload=json_set(payload,'$.synthetic_receipt','paid') WHERE id=?",(stopped,summary['id']))
    before=ctx.db.job(summary['id'],internal=True)
    ctx.settings.summary_model='changed/global-model'
    if stopped=='uncertain':
        assert ctx.worker.enqueue_summary(m,retry=True)==before
    for _ in range(3): assert not asyncio.run(ctx.worker.run_once())
    ctx.db.recover()
    restarted=Worker(ctx.db,ctx.settings,ctx.cloud.factory,enrollments=ctx.materials,coordinator=ctx.slot)
    for _ in range(2): assert not asyncio.run(restarted.run_once())
    assert ctx.db.job(summary['id'],internal=True)==before
    assert ctx.db.rows('SELECT * FROM summary_checkpoints')==[{'job_id':summary['id'],'key':'map:0','payload':'{"receipt":"synthetic-paid"}'}]
    assert ctx.db.rows('SELECT * FROM usage ORDER BY id')==receipts
    assert len(ctx.db.rows("SELECT * FROM jobs WHERE stage='summarize'"))==1
    assert ctx.cloud.llm==0 and ctx.cloud.posts==1
    assert ctx.db.one('SELECT summary_job_id FROM meeting_pipeline_handoffs')['summary_job_id']==summary['id']


@pytest.mark.parametrize('stopped',['cancelled','failed','waiting_config','paused_budget'])
def test_explicit_paid_retry_preserves_job_and_checkpoint_namespace(ctx,stopped):
    m=meeting(ctx); transcribe(ctx,m); asyncio.run(ctx.worker.run_once())
    summary=ctx.db.one("SELECT * FROM jobs WHERE stage='summarize'")
    ctx.db.execute("INSERT INTO summary_checkpoints VALUES(?,?,?)",(summary['id'],'map:0','{"receipt":"synthetic-paid"}'))
    ctx.db.execute('UPDATE jobs SET status=?,attempts=3,cancel_requested=1 WHERE id=?',(stopped,summary['id']))
    retried=ctx.worker.enqueue_summary(m,retry=True)
    assert retried['id']==summary['id'] and retried['status']=='queued' and retried['attempts']==3
    assert json.loads(retried['payload'])['retry_attempt_baseline']==3
    assert ctx.db.one('SELECT payload FROM summary_checkpoints WHERE job_id=?',(summary['id'],))['payload']=='{"receipt":"synthetic-paid"}'
    asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.llm==1 and ctx.cloud.posts==1
    assert len(ctx.db.rows("SELECT * FROM jobs WHERE stage='summarize'"))==1


def test_first_summary_association_and_enqueue_are_one_transaction(ctx):
    m=meeting(ctx); transcribe(ctx,m)
    ctx.db.execute("CREATE TRIGGER fail_first_handoff BEFORE UPDATE OF summary_job_id ON meeting_pipeline_handoffs BEGIN SELECT RAISE(ABORT,'synthetic handoff failure'); END")
    asyncio.run(ctx.worker.run_once())
    assert ctx.worker.attribution.current(m,1)['outcome']=='completed'
    assert not ctx.db.rows("SELECT * FROM jobs WHERE stage='summarize'")
    assert ctx.db.one('SELECT summary_job_id FROM meeting_pipeline_handoffs')['summary_job_id'] is None
    ctx.db.execute('DROP TRIGGER fail_first_handoff')
    asyncio.run(ctx.worker.run_once())
    assert ctx.cloud.llm==1 and ctx.cloud.posts==1
    summary=ctx.db.one("SELECT * FROM jobs WHERE stage='summarize'")
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.execute('UPDATE meeting_pipeline_handoffs SET summary_job_id=NULL')
    assert ctx.db.one('SELECT summary_job_id FROM meeting_pipeline_handoffs')['summary_job_id']==summary['id']


def test_local_retry_restart_requires_explicit_continue_for_first_summary(ctx):
    m=meeting(ctx); local=transcribe(ctx,m); ctx.db.cancel(local['id'])
    retry=ctx.worker.enqueue_identification(m,command(ctx,m,retry=True))
    asyncio.run(ctx.worker.run_once())
    assert retry.job_id==local['id']
    for _ in range(3): assert not asyncio.run(ctx.worker.run_once())
    ctx.db.recover()
    restarted=Worker(ctx.db,ctx.settings,ctx.cloud.factory,enrollments=ctx.materials,coordinator=ctx.slot)
    for _ in range(2): assert not asyncio.run(restarted.run_once())
    assert ctx.cloud.llm==0 and ctx.cloud.posts==1
    assert ctx.db.one('SELECT summary_authorized FROM meeting_pipeline_handoffs')['summary_authorized']==0
    restarted.enqueue_transcription(m,resume=True)
    assert ctx.db.meeting(m)['transcript_version']==1
    asyncio.run(restarted.run_once())
    for _ in range(2): assert not asyncio.run(restarted.run_once())
    assert ctx.cloud.llm==1 and ctx.cloud.posts==1
    assert len(ctx.db.rows("SELECT * FROM jobs WHERE stage='summarize'"))==1


def test_partial_manual_reviews_replay_keep_exact_proposal_lineage_across_versions(ctx):
    m=meeting(ctx,chunks=2); p,participant=profile(ctx,m); ready(ctx,p)
    transcribe(ctx,m); asyncio.run(ctx.worker.run_once())
    before=ctx.speakers.get_attribution(m); first,remaining=before.items
    assert first.group_id!=remaining.group_id and all(i.review_candidates for i in before.items)
    review=ReviewAttribution(transcript_version=1,expected_revision=before.revision,operation_id=uid(),
        changes=[{'segment_id':first.segment_id,'participant_id':participant.id}])
    after=ctx.speakers.review_attribution(m,review)
    assert ctx.speakers.review_attribution(m,review)==after
    another=ReviewAttribution(transcript_version=1,expected_revision=after.revision,operation_id=uid(),
        changes=[{'segment_id':first.segment_id,'participant_id':None}])
    old=ctx.speakers.review_attribution(m,another)
    untouched=next(i for i in old.items if i.segment_id==remaining.segment_id)
    assert untouched.review_candidates==remaining.review_candidates and untouched.group_id==remaining.group_id
    assert not next(i for i in old.items if i.segment_id==first.segment_id).review_candidates
    assert ctx.cloud.posts==2 and ctx.cloud.llm==0 and ctx.engine.calls==2
    lineage=ctx.db.one('SELECT * FROM attribution_proposal_lineage WHERE run_id=?',(untouched.run_id,))
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.execute('UPDATE attribution_proposal_lineage SET source_intent_id=source_intent_id WHERE run_id=?',(untouched.run_id,))
    # Same raw provider label in a fresh version must never supply older metadata.
    ctx.worker.enqueue_transcription(m,new_transcript_version=True)
    for _ in range(3): asyncio.run(ctx.worker.run_once())
    new=ctx.speakers.get_attribution(m)
    assert new.transcript_version==2 and {i.group_id for i in new.items}.isdisjoint({i.group_id for i in before.items})
    assert ctx.speakers.get_attribution(m,1)==old
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.execute('INSERT INTO attribution_proposal_lineage VALUES(?,?,?,?,?,?)',
            (new.items[0].run_id,first.segment_id,m,2,new.revision,lineage['source_intent_id']))
    assert ctx.cloud.posts==4 and ctx.cloud.llm==0 and ctx.engine.calls==4


@pytest.mark.parametrize('shape',['all_manual','empty'])
def test_bypass_is_durable_in_get_even_without_unknown_rows(ctx,shape):
    m=meeting(ctx); p,participant=profile(ctx,m); ready(ctx,p); local=transcribe(ctx,m)
    if shape=='all_manual':
        before=ctx.speakers.get_attribution(m)
        ctx.speakers.review_attribution(m,ReviewAttribution(transcript_version=1,expected_revision=0,operation_id=uid(),
            changes=[{'segment_id':before.items[0].segment_id,'participant_id':participant.id}]))
        # The prior queued snapshot is invalidated by manual review; request the current local snapshot.
        ctx.db.cancel(local['id']); local={'id':ctx.worker.enqueue_identification(m,command(ctx,m)).job_id}
    else:
        ctx.db.execute('DELETE FROM segments WHERE meeting_id=?',(m,))
        ctx.db.cancel(local['id']); local={'id':ctx.worker.enqueue_identification(m,command(ctx,m)).job_id}
    # Missing local material runtime is a real unavailable path, including with no observations.
    from secretary.domain.enrollment import EnrollmentFailure
    ctx.materials.read_candidate=lambda *args,**kwargs: (_ for _ in ()).throw(EnrollmentFailure('runtime_missing'))
    asyncio.run(ctx.worker.run_once())
    run=ctx.db.one('SELECT run_id FROM identification_intents WHERE job_id=?',(local['id'],))['run_id']
    assert ctx.worker.attribution.state(m,run).outcome=='runtime_unavailable'
    revision=ctx.speakers.get_attribution(m).revision
    receipt=ctx.worker.attribution.bypass(m,BypassIdentification(transcript_version=1,expected_revision=revision,operation_id=uid()))
    reloaded=Worker(ctx.db,ctx.settings,ctx.cloud.factory,enrollments=ctx.materials,coordinator=ctx.slot)
    state=reloaded.attribution.state(m,run)
    assert state.outcome=='runtime_unavailable' and state.bypass_acknowledged
    assert state.bypass_attribution_revision==receipt.attribution_revision
    overlay=ctx.speakers.get_attribution(m)
    assert 'explicit_runtime_bypass' in overlay.reason_codes
    assert all(i.method=='manual' for i in overlay.items)
    # Open the disposable database through a fresh API instance, as a page reload does.
    with ctx.db.connection() as source, sqlite3.connect(ctx.settings.data_dir/'secretary.sqlite3') as target:
        source.backup(target)
    app=create_app(ctx.settings,capture=Capture(),enrollment_capture=Capture(),run_worker=False,
        provider_factory=ctx.cloud.factory,voice_model=MODEL)
    with TestClient(app,base_url='http://127.0.0.1:8765') as api:
        got=api.get(f'/api/v1/meetings/{m}/identification-runs/{run}').json()
        assert got['outcome']=='runtime_unavailable' and got['bypass_acknowledged'] is True
        assert got['bypass_attribution_revision']==receipt.attribution_revision
        snapshot=api.get(f'/api/v1/meetings/{m}/attribution').json()
        assert 'explicit_runtime_bypass' in snapshot['reason_codes']
        assert len(snapshot['items'])==(1 if shape=='all_manual' else 0)
    # A new version has its own barrier; old historical acknowledgement remains readable.
    ctx.worker.routes.bind(m,explicit=True,new_version=True)
    assert 'explicit_runtime_bypass' not in ctx.speakers.get_attribution(m).reason_codes
    assert not ctx.worker.attribution.barrier(m,2)
    assert ctx.worker.attribution.state(m,run).bypass_acknowledged


def test_bypass_never_resumes_an_already_stopped_paid_summary(ctx):
    m=meeting(ctx); p,_=profile(ctx,m); ready(ctx,p); transcribe(ctx,m); asyncio.run(ctx.worker.run_once())
    summary=ctx.db.one("SELECT * FROM jobs WHERE stage='summarize'")
    ctx.db.cancel(summary['id']); before=ctx.db.job(summary['id'],internal=True)
    local=ctx.worker.enqueue_identification(m,command(ctx,m)); ctx.engine.fail='runtime_missing'
    asyncio.run(ctx.worker.run_once())
    assert ctx.worker.attribution.state(m,local.run_id).outcome=='runtime_unavailable'
    ctx.worker.attribution.bypass(m,BypassIdentification(transcript_version=1,expected_revision=1,operation_id=uid()))
    for _ in range(3): assert not asyncio.run(ctx.worker.run_once())
    assert ctx.db.job(summary['id'],internal=True)==before and ctx.cloud.llm==0
