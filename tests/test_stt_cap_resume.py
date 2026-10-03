"""Explicit optional STT cap repair; synthetic payloads, no HTTP or devices."""
import asyncio
import json
from pathlib import Path

import pytest

from secretary.application.worker import Worker
from secretary.infrastructure.database import Database
from secretary.infrastructure.polza import PolzaClient, ProviderError
from secretary.settings import Settings
from test_backend import prepared


def context(tmp_path, *, caps=True, mode='standard', first_complete=False):
    settings=Settings(_env_file=None,project_dir=tmp_path,data_dir=tmp_path/'data',
        polza_api_key='synthetic-only',cloud_enabled=True,local_cost_limits_enabled=caps,
        stt_model='aiesa/transcribe',stt_price_rub_per_minute=.048)
    db=Database(settings.data_dir/'synthetic.sqlite3')
    m,_=prepared(db,settings)
    if mode=='voice':db.execute("UPDATE meetings SET processing_mode='voice_identification' WHERE id=?",(m['id'],))
    prepared(db,settings,meeting=m,sequence=1,offset=1000)
    requests=[]
    class Provider:
        def __init__(self,cfg):self.cfg=cfg
        async def transcribe(self,path,*,provider_job_id=None,**kwargs):
            if provider_job_id:
                requests.append(('poll',provider_job_id,self.cfg.stt_price_rub_per_minute))
                raise ProviderError('authentication','synthetic poll denied',provider_job_id=provider_job_id)
            payload=PolzaClient(self.cfg)._stt_payload(Path(path))
            requests.append(('post',payload))
            if first_complete and len(requests)==1:
                return {'segments':[],'speakers':[],'usage':{'confirmed_rub':.01,'provider_request_id':'synthetic-complete'}}
            raise ProviderError('price_limit','synthetic known rejection')
    worker=Worker(db,settings,Provider)
    worker.enqueue_transcription(m['id'])
    return db,settings,m,worker,requests


def transcript_jobs(db):
    return db.rows("SELECT * FROM jobs WHERE stage='transcribe' ORDER BY created_at,id")


def test_explicit_cap_resume_preserves_route_ids_released_history_and_durable_override(tmp_path):
    db,settings,m,worker,requests=context(tmp_path)
    asyncio.run(worker.run_once())
    route=db.one('SELECT * FROM meeting_transcript_routes')
    ids=[j['id'] for j in transcript_jobs(db)]
    settings.stt_price_rub_per_minute=.06
    before=transcript_jobs(db)
    worker.enqueue_transcription(m['id'])
    assert transcript_jobs(db)==before and not asyncio.run(worker.run_once())
    worker.enqueue_transcription(m['id'],resume=True)
    assert len(requests)==1  # Continue only enqueues
    assert [j['id'] for j in transcript_jobs(db)]==ids
    assert all(json.loads(j['payload'])['settings']['stt_price_rub_per_minute']==.06 for j in transcript_jobs(db))
    assert db.one('SELECT * FROM meeting_transcript_routes')==route
    assert db.meeting(m['id'])['transcript_version']==1
    settings.stt_price_rub_per_minute=.07
    worker.enqueue_transcription(m['id'])
    assert all(json.loads(j['payload'])['settings']['stt_price_rub_per_minute']==.06 for j in transcript_jobs(db))
    asyncio.run(worker.run_once())
    assert [item[1]['provider']['max_price']['stt_per_minute'] for item in requests]==[.048,.06]
    assert db.rows('SELECT status FROM usage')==[{'status':'released'},{'status':'released'}]


def test_cap_repair_skips_completed_sibling_and_preserves_its_receipt(tmp_path):
    db,settings,m,worker,requests=context(tmp_path,first_complete=True)
    asyncio.run(worker.run_once()); asyncio.run(worker.run_once())
    completed=next(j for j in transcript_jobs(db) if j['status']=='succeeded')
    receipt=db.rows('SELECT * FROM usage WHERE job_id=?',(completed['id'],))
    route=db.one('SELECT * FROM meeting_transcript_routes')
    settings.stt_price_rub_per_minute=.06
    worker.enqueue_transcription(m['id'],retry=True)
    assert db.job(completed['id'],internal=True)==completed
    assert db.rows('SELECT * FROM usage WHERE job_id=?',(completed['id'],))==receipt
    asyncio.run(worker.run_once())
    assert len(requests)==3 and requests[-1][1]['provider']['max_price']['stt_per_minute']==.06
    assert db.one('SELECT * FROM meeting_transcript_routes')==route
    assert len(transcript_jobs(db))==2


@pytest.mark.parametrize('status',['paused_budget','waiting_config','uncertain'])
def test_accepted_provider_id_resumes_only_poll_with_original_settings(tmp_path,status):
    db,settings,m,worker,requests=context(tmp_path)
    job=transcript_jobs(db)[0]
    db.execute("UPDATE jobs SET status=?,provider_job_id='accepted-synthetic' WHERE id=?",(status,job['id']))
    settings.stt_price_rub_per_minute=.06
    worker.enqueue_transcription(m['id'],resume=True)
    retained=db.job(job['id'],internal=True)
    assert retained['payload']==job['payload'] and retained['provider_job_id']=='accepted-synthetic'
    # Prevent the unrelated queued sibling from dispatching in this probe.
    db.execute("UPDATE jobs SET status='paused_budget' WHERE id!=?",(job['id'],))
    asyncio.run(worker.run_once())
    assert requests==[('poll','accepted-synthetic',.048)]
    assert db.one('SELECT status FROM usage WHERE job_id=?',(job['id'],))['status']=='unknown'


@pytest.mark.parametrize('status,receipt',[('uncertain',None),('paused_budget','confirmed'),('waiting_config','unknown')])
def test_ambiguous_or_accepted_without_id_does_not_resume_or_change_cap(tmp_path,status,receipt):
    db,settings,m,worker,requests=context(tmp_path)
    job=transcript_jobs(db)[0]
    db.execute('UPDATE jobs SET status=? WHERE id=?',(status,job['id']))
    if receipt:
        reservation=db.reserve(job,.01,10,True,.01)
        db.settle(reservation,{'confirmed_rub':.01} if receipt=='confirmed' else None)
    settings.stt_price_rub_per_minute=.06
    worker.enqueue_transcription(m['id'],resume=True)
    assert db.job(job['id'],internal=True)['payload']==job['payload']
    assert db.job(job['id'])['status']==status and not requests


@pytest.mark.parametrize('mode',['standard','voice'])
def test_foreign_global_model_never_supplies_cap_to_frozen_model(tmp_path,mode):
    db,settings,m,worker,requests=context(tmp_path,mode=mode)
    asyncio.run(worker.run_once())
    route=db.one('SELECT * FROM meeting_transcript_routes')
    settings.stt_model='openai/whisper-1'; settings.stt_price_rub_per_minute=.06
    worker.enqueue_transcription(m['id'],resume=True)
    assert all(json.loads(j['payload'])['settings']=={'stt_model':'aiesa/transcribe','stt_price_rub_per_minute':.048} for j in transcript_jobs(db))
    assert db.one('SELECT * FROM meeting_transcript_routes')==route


def test_caps_false_omits_provider_price_limit_on_explicit_resume(tmp_path):
    db,settings,m,worker,requests=context(tmp_path,caps=False)
    asyncio.run(worker.run_once())
    settings.stt_price_rub_per_minute=.06
    worker.enqueue_transcription(m['id'],resume=True)
    asyncio.run(worker.run_once())
    assert len(requests)==2 and all('max_price' not in item[1]['provider'] for item in requests)
    assert settings.local_cost_limits_enabled is False


def test_claimed_job_is_not_rewritten_by_explicit_cap_resume(tmp_path):
    db,settings,m,worker,_=context(tmp_path)
    job=db.claim(); settings.stt_price_rub_per_minute=.06
    before=db.job(job['id'],internal=True)
    worker.enqueue_transcription(m['id'],resume=True)
    assert db.job(job['id'],internal=True)==before
