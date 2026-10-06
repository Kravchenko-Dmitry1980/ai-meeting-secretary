"""Known-unsent Voice dispatch outcomes: real authorities, synthetic providers."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4
import wave

import httpx
import pytest

from secretary.application.voice_commands import VoiceCommandService
from secretary.domain.cloud_budget import AccountUsage
from secretary.domain.voice_commands import VoiceError
from secretary.infrastructure.team_database import TeamDatabase
from test_voice_budget import _case, _event, _NoMedia, _billing_rows, _count, CAP, KEY_TAG, LITERAL


def voice_case(tmp_path, monkeypatch, *, lane='intent', handler=None):
    posts=[]
    def provider(request):
        posts.append(request.url.path)
        if handler:return handler(request)
        if 'transcriptions' in request.url.path:
            return httpx.Response(200,json={'id':'synthetic-stt','text':LITERAL,'duration':1,'usage':{'cost_rub':'.02'}})
        return httpx.Response(200,json={'id':'synthetic-intent','usage':{'cost_rub':'.03'},
            'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"proposals":[]}'}}]})
    c=_case(tmp_path,monkeypatch,provider)
    event=_event(c,text=LITERAL if lane=='intent' else None)
    identifier=c.voices.enqueue(event,text=LITERAL if lane=='intent' else None)
    claim=c.voices.claim_job('synthetic-final-dispatch',lease_seconds=180)
    class Media:
        def download_voice(self,event,*,cancelled=None):
            path=tmp_path/'synthetic.wav'
            with wave.open(str(path),'wb') as stream:
                stream.setparams((1,2,16000,0,'NONE','none'));stream.writeframes(b'\0\0'*16000)
            return SimpleNamespace(path=path,duration_ms=1000,command_id=event.event_id)
    c.service=VoiceCommandService(c.team,c.bot,c.voices,Media() if lane=='stt' else _NoMedia(),c.client,clock=lambda:c.clock[0])
    c.identifier,c.claim,c.posts=identifier,claim,posts
    return c


def change_after_begin(c,monkeypatch,change):
    original=c.voices.begin_request
    if change=='new_period':
        # Advance only the billing clock for rollover; service lease is independent.
        billing_now=[datetime(2026,10,31,21,59,30,tzinfo=timezone.utc)]
        c.budget.clock=lambda:billing_now[0]
        c.budget.refresh_account(AccountUsage(KEY_TAG,CAP,CAP,0,'monthly',c.budget.clock()))
    def before(claim,info):
        original(claim,info)
        if change=='exhausted':c.budget.refresh_account(AccountUsage(KEY_TAG,CAP,0,CAP,'monthly',c.budget.clock()))
        elif change=='invalid_cap':c.budget.refresh_account(AccountUsage(KEY_TAG,4000_000000,4000_000000,0,'monthly',c.budget.clock()))
        elif change=='stale':c.clock[0]+=timedelta(seconds=61)
        else:billing_now[0]+=timedelta(seconds=31)
    monkeypatch.setattr(c.voices,'begin_request',before)


def abort_count(c):
    with c.db.connection() as conn:return conn.execute('SELECT COUNT(*) FROM voice_request_aborts').fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('lane',['intent','stt'])
@pytest.mark.parametrize('change,expected',[('exhausted','paused_budget'),('invalid_cap','paused_config'),
    ('stale','paused_config'),('new_period','paused_config')])
async def test_final_budget_refusal_is_durable_unsent_not_unknown(tmp_path,monkeypatch,lane,change,expected):
    c=voice_case(tmp_path,monkeypatch,lane=lane);change_after_begin(c,monkeypatch,change)
    reply=await c.service.execute(c.claim)
    job=c.voices.get_job(c.identifier)
    assert c.posts==[] and _billing_rows(c)[0]['status']=='released'
    assert c.budget.snapshot().reserved_micro==0
    assert job['state']==expected and not job['pending_request']
    assert abort_count(c)==1 and _count(c,'voice_requests')==1 and _count(c,'voice_responses')==0
    assert 'Результат оплачиваемого запроса пока не определён' not in reply.text
    reopened=c.Repository(TeamDatabase(c.db.path),c.team,c.bot,clock=lambda:c.clock[0])
    reopened.recover_expired()
    assert reopened.get_job(c.identifier)['state']==expected
    assert reopened.claim_job('replacement') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('lane',['intent','stt'])
async def test_accepted_response_is_unchanged_and_never_aborted(tmp_path,monkeypatch,lane):
    c=voice_case(tmp_path,monkeypatch,lane=lane)
    await c.service.execute(c.claim)
    assert c.voices.get_job(c.identifier)['state']=='complete'
    assert len(c.posts)==(2 if lane=='stt' else 1)
    assert _count(c,'voice_responses')==len(c.posts) and abort_count(c)==0
    assert all(row['status']=='confirmed' for row in _billing_rows(c))


@pytest.mark.asyncio
@pytest.mark.parametrize('lane',['intent','stt'])
async def test_actual_timeout_stays_unknown_without_unsent_evidence(tmp_path,monkeypatch,lane):
    def timeout(request):raise httpx.ReadTimeout('synthetic',request=request)
    c=voice_case(tmp_path,monkeypatch,lane=lane,handler=timeout)
    await c.service.execute(c.claim)
    assert len(c.posts)==1 and abort_count(c)==0
    assert c.voices.get_job(c.identifier)['state']=='uncertain'
    assert c.voices.get_job(c.identifier)['pending_request']
    assert _billing_rows(c)[0]['status']=='uncertain'


@pytest.mark.asyncio
@pytest.mark.parametrize('persist_first',[False,True])
async def test_abort_callback_failure_is_conservative_and_never_posts(tmp_path,monkeypatch,persist_first):
    c=voice_case(tmp_path,monkeypatch);change_after_begin(c,monkeypatch,'exhausted')
    original=c.voices.record_not_submitted
    def fail(claim,info):
        if persist_first:original(claim,info)
        raise OSError('synthetic-write-failure')
    monkeypatch.setattr(c.voices,'record_not_submitted',fail)
    await c.service.execute(c.claim)
    assert c.posts==[] and _billing_rows(c)[0]['status']=='released'
    assert c.voices.get_job(c.identifier)['state']=='uncertain'
    assert abort_count(c)==int(persist_first)


@pytest.mark.asyncio
@pytest.mark.parametrize('persist_abort',[False,True])
async def test_crash_between_known_unsent_and_job_state_never_reposts(tmp_path,monkeypatch,persist_abort):
    c=voice_case(tmp_path,monkeypatch);change_after_begin(c,monkeypatch,'exhausted')
    if not persist_abort:
        monkeypatch.setattr(c.voices,'record_not_submitted',lambda *args:(_ for _ in ()).throw(OSError('synthetic')))
    monkeypatch.setattr(c.voices,'fail_job',lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('simulated-crash')))
    with pytest.raises(RuntimeError,match='simulated-crash'):await c.service.execute(c.claim)
    assert c.posts==[]
    c.clock[0]+=timedelta(seconds=181)
    reopened=c.Repository(TeamDatabase(c.db.path),c.team,c.bot,clock=lambda:c.clock[0])
    reopened.recover_expired()
    assert reopened.get_job(c.identifier)['state']==('paused_budget' if persist_abort else 'uncertain')
    assert reopened.claim_job('replacement') is None


def pending(c):
    c.voices.checkpoint_context(c.claim,{'synthetic':'context'})
    info={'operation_id':str(uuid4()),'category':'voice_intent','command_id':c.claim.event.event_id,
        'stage':'intent','attempt':0,'request_hash':'a'*64}
    c.voices.begin_request(c.claim,info)
    return {**info,'error_code':'monthly_budget_exhausted'}


@pytest.mark.parametrize('field,value',[('operation_id',str(uuid4())),('command_id',str(uuid4())),
    ('stage','stt'),('attempt',1),('request_hash','b'*64),('category','voice_stt'),('error_code','request_uncertain')])
def test_abort_requires_exact_durable_request_and_safe_reason(tmp_path,monkeypatch,field,value):
    c=voice_case(tmp_path,monkeypatch);info=pending(c)
    with pytest.raises(VoiceError):c.voices.record_not_submitted(c.claim,{**info,field:value})
    assert abort_count(c)==0 and c.voices.pending_request(c.claim)


def test_abort_is_idempotent_immutable_and_excludes_provider_response(tmp_path,monkeypatch):
    c=voice_case(tmp_path,monkeypatch);info=pending(c)
    c.voices.record_not_submitted(c.claim,info);c.voices.record_not_submitted(c.claim,info)
    assert abort_count(c)==1 and not c.voices.pending_request(c.claim)
    with pytest.raises(VoiceError):c.voices.record_not_submitted(c.claim,{**info,'error_code':'monthly_budget_stale'})
    response={k:info[k] for k in ('operation_id','stage','attempt','request_hash')}
    response.update(status=200,provider_response={},usage_receipt={})
    with pytest.raises(VoiceError):c.voices.record_response(c.claim,response)
    with c.db.transaction() as conn,pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO voice_responses VALUES(?,?,?)',(info['operation_id'],'a'*64,'{}'))
    for sql in ('UPDATE voice_request_aborts SET payload=payload','DELETE FROM voice_request_aborts',
                'INSERT OR REPLACE INTO voice_request_aborts SELECT * FROM voice_request_aborts'):
        with c.db.transaction() as conn,pytest.raises(sqlite3.IntegrityError):conn.execute(sql)


def test_response_cannot_be_reclassified_as_unsent_and_expired_claim_is_fenced(tmp_path,monkeypatch):
    c=voice_case(tmp_path,monkeypatch);info=pending(c)
    response={k:info[k] for k in ('operation_id','stage','attempt','request_hash')}
    response.update(status=429,provider_response={},usage_receipt={})
    c.voices.record_response(c.claim,response)
    with pytest.raises(VoiceError):c.voices.record_not_submitted(c.claim,info)
    with c.db.transaction() as conn,pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO voice_request_aborts VALUES(?,?,?)',(info['operation_id'],'a'*64,'{}'))
    c.clock[0]+=timedelta(seconds=181)
    with pytest.raises(VoiceError,match='voice_claim_lost'):c.voices.record_not_submitted(c.claim,info)
    assert abort_count(c)==0


@pytest.mark.asyncio
async def test_http_rejection_released_by_billing_is_not_known_unsent(tmp_path,monkeypatch):
    c=voice_case(tmp_path,monkeypatch,handler=lambda _:httpx.Response(429,json={'error':'synthetic'}))
    await c.service.execute(c.claim)
    assert len(c.posts)==1 and _billing_rows(c)[0]['status']=='released'
    assert _count(c,'voice_responses')==1 and abort_count(c)==0
    assert c.voices.get_job(c.identifier)['state']=='rejected'


@pytest.mark.asyncio
async def test_refused_repair_preserves_first_confirmed_response_without_replay(tmp_path,monkeypatch):
    def invalid_model_reply(_):
        return httpx.Response(200,json={'id':'synthetic-invalid-shape','usage':{'cost_rub':'.03'},
            'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"unexpected":true}'}}]})
    c=voice_case(tmp_path,monkeypatch,handler=invalid_model_reply)
    original=c.voices.begin_request
    def before(claim,info):
        original(claim,info)
        if info['attempt']==1:c.budget.refresh_account(AccountUsage(KEY_TAG,CAP,0,CAP,'monthly',c.clock[0]))
    monkeypatch.setattr(c.voices,'begin_request',before)
    await c.service.execute(c.claim)
    assert len(c.posts)==1 and _count(c,'voice_requests')==2 and _count(c,'voice_responses')==1
    assert abort_count(c)==1 and not c.voices.get_job(c.identifier)['pending_request']
    assert c.voices.get_job(c.identifier)['state']=='paused_budget'
    charges=_billing_rows(c)
    assert [r['status'] for r in charges]==['confirmed','released'] and charges[0]['confirmed_micro']==30_000
    c.clock[0]+=timedelta(seconds=181);c.voices.recover_expired()
    assert c.voices.claim_job('replacement') is None and len(c.posts)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('persist_first',[False,True])
async def test_cancelled_abort_callback_drains_to_uncertain_without_post(tmp_path,monkeypatch,persist_first):
    c=voice_case(tmp_path,monkeypatch);change_after_begin(c,monkeypatch,'exhausted')
    original=c.voices.record_not_submitted
    # This is a callback boundary cancellation, not a fictitious provider result.
    def cancelled(claim,info):
        if persist_first:original(claim,info)
        raise asyncio.CancelledError
    monkeypatch.setattr(c.voices,'record_not_submitted',cancelled)
    with pytest.raises(asyncio.CancelledError):await c.service.execute(c.claim)
    assert c.posts==[] and _billing_rows(c)[0]['status']=='released'
    assert c.voices.get_job(c.identifier)['state']=='uncertain'


@pytest.mark.asyncio
async def test_failed_release_never_creates_not_submitted_proof(tmp_path,monkeypatch):
    from secretary.domain.cloud_budget import BudgetError
    c=voice_case(tmp_path,monkeypatch);change_after_begin(c,monkeypatch,'exhausted')
    monkeypatch.setattr(c.budget,'release_unsubmitted',lambda operation:(_ for _ in ()).throw(BudgetError('synthetic_release_failed')))
    await c.service.execute(c.claim)
    assert c.posts==[] and abort_count(c)==0
    assert _billing_rows(c)[0]['status']=='reserved'
    assert c.voices.get_job(c.identifier)['state']=='uncertain'


def test_abort_other_job_and_stale_fence_cannot_clear_original_request(tmp_path,monkeypatch):
    c=voice_case(tmp_path,monkeypatch);info=pending(c)
    for claim in (replace(c.claim,job_id=str(uuid4())),replace(c.claim,fence=c.claim.fence+1)):
        with pytest.raises(VoiceError):c.voices.record_not_submitted(claim,info)
    assert c.voices.pending_request(c.claim) and abort_count(c)==0


def legacy_eight(c):
    with c.db.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM voice_request_aborts').fetchone()[0]==0
        conn.execute('DROP TRIGGER response_abort_guard');conn.execute('DROP TRIGGER abort_response_guard')
        conn.execute('DROP TABLE voice_request_aborts')
        conn.execute('DELETE FROM team_schema WHERE version=9')
        return tuple(conn.execute('SELECT * FROM voice_requests').fetchone())


def test_real_schema_eight_upgrades_without_reclassifying_pending_attempt(tmp_path,monkeypatch):
    c=voice_case(tmp_path,monkeypatch);pending(c);before=legacy_eight(c)
    reopened=TeamDatabase(c.db.path)
    with reopened.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0]==9
        assert tuple(conn.execute('SELECT * FROM voice_requests').fetchone())==before
        assert conn.execute('PRAGMA foreign_key_check').fetchone() is None
        assert conn.execute('SELECT COUNT(*) FROM voice_request_aborts').fetchone()[0]==0
        assert c.Repository.recovery_outcome(conn,c.identifier)==('uncertain','voice_request_uncertain')


def test_schema_nine_migration_failure_is_atomic(tmp_path,monkeypatch):
    import secretary.infrastructure.voice_repository as source
    c=voice_case(tmp_path,monkeypatch);pending(c);before=legacy_eight(c)
    monkeypatch.setattr(source,'VOICE_ABORT_MIGRATION',source.VOICE_ABORT_MIGRATION+('INVALID MIGRATION STATEMENT',))
    with pytest.raises(sqlite3.OperationalError):TeamDatabase(c.db.path)
    with sqlite3.connect(c.db.path) as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0]==8
        assert tuple(conn.execute('SELECT * FROM voice_requests').fetchone())==before
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='voice_request_aborts'").fetchone()


def recovery_case(tmp_path,monkeypatch):
    from test_team_runtime_recovery import case,NOW
    from secretary.infrastructure.bot_repository import BotRepository
    from secretary.infrastructure.team_auth_repository import AuthRepository
    from secretary.infrastructure.voice_repository import VoiceRepository
    c=case(tmp_path,monkeypatch)
    from secretary.domain.team import TeamMember
    c.actor=TeamMember(id=str(uuid4()),display_name='Synthetic abort recovery actor',role='owner',
        max_user_id='777',vikunja_user_id='778',project_ids=('7',))
    c.team.upsert_member(c.actor,expected_revision=None);c.clock=[NOW]
    c.auth=AuthRepository(c.db,secret=b'SYNTHETIC_ABORT_RECOVERY_SECRET_32',clock=lambda:NOW)
    c.bot=BotRepository(c.db,c.team,c.auth,clock=lambda:NOW)
    c.voices=VoiceRepository(c.db,c.team,c.bot,clock=lambda:NOW)
    c.Repository=VoiceRepository
    event=_event(c,text=LITERAL)
    c.identifier=c.voices.enqueue(event,text=LITERAL)
    # Existing fixture's unrelated old jobs use raw rows and do not block this user's new event.
    c.claim=c.voices.claim_job(c.old+':voice',lease_seconds=120)
    assert c.claim and c.claim.job_id==c.identifier
    return c


def test_dead_gateway_recovery_uses_abort_and_watermark_without_requeue(tmp_path,monkeypatch):
    from test_team_runtime_recovery import checkpoint,raw_preflight,NOW
    c=recovery_case(tmp_path,monkeypatch);info=pending(c)
    with c.db.connection() as conn:before=c.module._watermark(conn,c.module._TEAM_TABLES)
    c.voices.record_not_submitted(c.claim,info)
    with c.db.connection() as conn:after=c.module._watermark(conn,c.module._TEAM_TABLES)
    assert before!=after
    with c.maintenance.admission(c.new,'crash_recovery'):
        raw_preflight(c)
        proof=checkpoint(c)
    with c.db.connection() as conn:
        row=conn.execute('SELECT * FROM voice_job_state WHERE id=?',(c.identifier,)).fetchone()
        assert row['state']=='paused_budget' and row['worker_id'] is None
        assert conn.execute('SELECT COUNT(*) FROM voice_request_aborts').fetchone()[0]==1
    assert proof.source_watermarks


@pytest.mark.parametrize('version',[7,8,9])
def test_raw_no_ticket_active_voice_preflight_never_writes_or_migrates(tmp_path,monkeypatch,version):
    from test_team_runtime_recovery import raw_preflight,source_digest
    from secretary.infrastructure.team_maintenance import MaintenanceError
    c=recovery_case(tmp_path,monkeypatch);info=pending(c)
    if version==9:c.voices.record_not_submitted(c.claim,info)
    else:
        legacy_eight(c)
        if version==7:
            with c.db.transaction() as conn:conn.execute('DELETE FROM team_schema WHERE version=8')
    for ticket in c.tickets:c.maintenance.leave(ticket)
    before=source_digest(c.db.path)
    with c.maintenance.admission(c.new,'crash_recovery'),pytest.raises(MaintenanceError,match='unrepresented'):
        raw_preflight(c,())
    assert source_digest(c.db.path)==before


@pytest.mark.parametrize('version',[7,8])
def test_old_schema_raw_preflight_and_read_watermark_need_no_abort_table(tmp_path,monkeypatch,version):
    from test_team_runtime_recovery import raw_preflight,source_digest
    c=recovery_case(tmp_path,monkeypatch);pending(c);legacy_eight(c)
    if version==7:
        with c.db.transaction() as conn:conn.execute('DELETE FROM team_schema WHERE version=8')
    before=source_digest(c.db.path)
    with c.maintenance.admission(c.new,'crash_recovery'):raw_preflight(c)
    with c.db.connection() as conn:
        assert c.module._watermark(conn,c.module._TEAM_TABLES)
        assert c.Repository.recovery_outcome(conn,c.identifier)==('uncertain','voice_request_uncertain')
    assert source_digest(c.db.path)==before
