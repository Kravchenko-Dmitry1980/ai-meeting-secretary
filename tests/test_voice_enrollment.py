"""Isolated synthetic lifecycle/API/capture tests. No native device or cloud calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import struct
import tempfile
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app, BodyLimitMiddleware
from secretary.domain.enrollment import (EnrollCommand, ConfirmEnrollment, DeleteEnrollment, EnrollmentFailure,
    StartEnrollmentRecording, StopEnrollmentRecording)
from secretary.domain.speakers import PatchProfile
from secretary.domain.voice import ModelStamp, VoiceError, MAX_PAYLOAD
from secretary.infrastructure.database import uid
from secretary.infrastructure.voice_audio import screen_pcm, wav_bytes
from secretary.infrastructure.voice_engine import EmbeddingBatch
from secretary.infrastructure.voice_store import VoiceStore
from secretary.settings import Settings

MODEL = ModelStamp('speechbrain/spkrec-ecapa-voxceleb', 'a'*40, 'b'*64)


class Cipher:
    def protect(self, plain):
        return b'synthetic-encrypted:' + plain[::-1]
    def unprotect(self, cipher):
        if not cipher.startswith(b'synthetic-encrypted:'):
            raise VoiceError('dpapi_failed')
        return cipher[20:][::-1]


class Decoder:
    def decode(self, payload, *, cancel=None):
        if payload == b'bad':
            raise VoiceError('invalid_audio')
        return screen_pcm(struct.pack('<h', 3000) * (16000*20))


class Engine:
    def __init__(self):
        self.calls = 0
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False
    def embed(self, clips, *, cancel):
        self.calls += 1
        self.entered.set()
        if self.block:
            assert self.release.wait(8)
        return EmbeddingBatch(MODEL, tuple((1.,)*192 for _ in clips), 1)


class Capture:
    def __init__(self):
        self.active = None
        self.closed = False
        self.timeout = False
        self.callback = None
        self.chunks = []
    def start(self, identifier, microphone_id=None, system_id=None, on_chunk=None, on_error=None, on_finish=None):
        self.active, self.callback = identifier, on_finish
        return self.state(identifier)
    def state(self, identifier):
        return {'status': 'recording' if self.active == identifier else 'stopped',
                'recording': self.active == identifier, 'native_closed': self.active != identifier,
                'duration_ms': 20000, 'chunks': self.chunks}
    def stop(self, identifier):
        if self.timeout:
            raise RuntimeError('synthetic timeout')
        if self.active == identifier:
            self.active = None
            snapshot = self.state(identifier)
            if self.callback:
                self.callback(snapshot)
        return self.state(identifier)
    def close(self):
        self.closed = True
        if self.active:
            self.stop(self.active)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('No cloud/provider/native calls')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', denied)
    monkeypatch.setattr(tempfile, 'tempdir', tempfile.tempdir)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path/'data', polza_api_key='', cloud_enabled=False, local_cost_limits_enabled=False)
    engine, meeting, sample = Engine(), Capture(), Capture()
    store = VoiceStore(settings.data_dir, cipher=Cipher())
    app = create_app(settings, provider_factory=denied, capture=meeting, enrollment_capture=sample,
        voice_store=store, enrollment_decoder=Decoder(), voice_engine=engine, voice_model=MODEL, run_worker=False)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 40000)) as api:
        token = {'X-Secretary-Token': api.get('/api/v1/session').json()['csrf_token']}
        person = api.post('/api/v1/participants', headers=token, json={'display_name':'Synthetic', 'operation_id':uid()}).json()['id']
        yield SimpleNamespace(api=api, token=token, person=person, service=app.state.enrollments,
            db=app.state.db, app=app, engine=engine, sample=sample, meeting=meeting, store=store)


def upload(ctx, payload=b'synthetic', **edits):
    return ctx.api.post(f'/api/v1/participants/{ctx.person}/enrollments', headers=ctx.token,
        files={'file':('synthetic.wav',payload,'audio/wav')}, data={'consent_confirmed':'true','operation_id':uid(),**edits})


def confirm_command(enrollment, **edits):
    return ConfirmEnrollment(expected_revision=enrollment.revision, listening_confirmed=True, single_speaker_confirmed=True, operation_id=uid(), **edits)


def test_review_immutable_generation_receipts_delete_privacy(ctx):
    operation = uid()
    first = upload(ctx, operation_id=operation)
    assert first.status_code == 201
    draft = first.json()
    assert draft['status'] == 'awaiting_review' and draft['revision'] == 1
    assert upload(ctx, operation_id=operation).json() == draft
    assert upload(ctx, b'different', operation_id=operation).status_code == 409
    original = ctx.db.one('SELECT storage_key FROM voice_enrollments WHERE id=?', (draft['id'],))['storage_key']
    command = ConfirmEnrollment(expected_revision=1,listening_confirmed=True,single_speaker_confirmed=True,operation_id=uid())
    ready = ctx.service.confirm(ctx.person, draft['id'], command)
    assert ready.id == draft['id'] and ready.status == 'ready' and ready.revision == 2
    assert ctx.service.confirm(ctx.person, ready.id, command) == ready and ctx.engine.calls == 1
    generation = ctx.db.one('SELECT storage_key FROM voice_enrollments WHERE id=?', (ready.id,))['storage_key']
    assert generation != original and generation != ready.id
    assert not (ctx.store.root / ctx.person / (original+'.dpapi')).exists()
    revision, snapshot = ctx.service.repository.voice_snapshot()
    assert snapshot[0]['revision'] == 2 and revision > 0
    public = ctx.api.get(f'/api/v1/participants/{ctx.person}/enrollments').json()
    assert not any(secret in json.dumps(public) for secret in ('storage_key', 'pcm16', 'embeddings', str(ctx.store.root), generation))
    command = DeleteEnrollment(expected_revision=2, operation_id=uid())
    removed = ctx.service.delete(ctx.person, ready.id, command)
    assert removed.status == 'revoked' and removed.revision == 3 and not removed.consent_confirmed
    assert ctx.service.delete(ctx.person, ready.id, command) == removed
    assert ctx.service.repository.voice_snapshot()[1] == ()
    assert list((ctx.store.root/ctx.person).iterdir()) == []
    assert ctx.db.rows('SELECT * FROM jobs') == ctx.db.rows('SELECT * FROM meetings') == []


def test_consent_review_revision_and_cross_profile_gates(ctx):
    assert upload(ctx, consent_confirmed='false').status_code == 422
    assert ctx.db.rows('SELECT * FROM voice_enrollments') == []
    assert upload(ctx,b'bad').status_code == 422
    draft = ctx.service.upload(ctx.person,b'synthetic',EnrollCommand(consent_confirmed=True,operation_id=uid()))
    for body in ({'listening_confirmed':False,'single_speaker_confirmed':True}, {'listening_confirmed':True,'single_speaker_confirmed':False}):
        with pytest.raises(EnrollmentFailure, match='human_review_required'):
            ctx.service.confirm(ctx.person,draft.id,ConfirmEnrollment(expected_revision=1,operation_id=uid(),**body))
    with pytest.raises(EnrollmentFailure, match='stale_material'):
        ctx.service.confirm(ctx.person,draft.id,ConfirmEnrollment(expected_revision=0,listening_confirmed=True,single_speaker_confirmed=True,operation_id=uid()))
    with pytest.raises(EnrollmentFailure, match='profile_not_found'):
        ctx.service.preview(uid(),draft.id,expected_revision=1)


@pytest.mark.parametrize('action',['revoke','disable'])
def test_inflight_completion_cannot_publish_after_revocation_or_disable(ctx, action):
    draft = ctx.service.upload(ctx.person,b'synthetic',EnrollCommand(consent_confirmed=True,operation_id=uid()))
    ctx.engine.block = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ctx.service.confirm,ctx.person,draft.id,confirm_command(draft))
        assert ctx.engine.entered.wait(2)
        if action == 'revoke':
            deleted = ctx.service.delete(ctx.person,draft.id,DeleteEnrollment(expected_revision=1,operation_id=uid()))
            assert deleted.status == 'cleanup_pending'
        else:
            current = ctx.db.one('SELECT revision FROM person_profiles WHERE id=?',(ctx.person,))['revision']
            ctx.app.state.speakers.patch_profile(ctx.person,PatchProfile(enabled=False,expected_revision=current,operation_id=uid()))
        ctx.engine.release.set()
        with pytest.raises(EnrollmentFailure):
            future.result(3)
    assert ctx.service.repository.get(ctx.person,draft.id).status != 'ready'
    assert not ctx.db.rows("SELECT * FROM enrollment_materials WHERE state='writing'")
    if action == 'revoke':
        assert ctx.service.repository.get(ctx.person,draft.id).status == 'revoked'
        assert list((ctx.store.root/ctx.person).iterdir()) == []


def test_shared_inference_slot_nonblocking_and_retry_receipt(ctx):
    first = ctx.service.upload(ctx.person,b'a',EnrollCommand(consent_confirmed=True,operation_id=uid()))
    second = ctx.service.upload(ctx.person,b'b',EnrollCommand(consent_confirmed=True,operation_id=uid()))
    ctx.engine.block=True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(ctx.service.confirm,ctx.person,first.id,confirm_command(first))
        assert ctx.engine.entered.wait(2)
        command=confirm_command(second)
        with pytest.raises(EnrollmentFailure,match='inference_busy'):
            ctx.service.confirm(ctx.person,second.id,command)
        with pytest.raises(EnrollmentFailure,match='inference_busy'):
            ctx.service.confirm(ctx.person,second.id,command)
        ctx.engine.release.set()
        assert future.result(3).status=='ready'


def test_preview_ranges_csrf_peer_ifrange_and_revision(ctx):
    draft = upload(ctx).json()
    base=f'/api/v1/participants/{ctx.person}/enrollments/{draft["id"]}'
    url=base+'/audio-preview?expected_revision=1'
    full=ctx.api.get(url)
    assert full.status_code==200 and full.content.startswith(b'RIFF')
    assert full.headers['cache-control']=='no-store' and full.headers['x-content-type-options']=='nosniff'
    for value, expected in [('bytes=0-9',full.content[:10]),('bytes=10-',full.content[10:]),('bytes=-10',full.content[-10:])]:
        part=ctx.api.get(url,headers={'Range':value})
        assert part.status_code==206 and part.content==expected and int(part.headers['content-length'])==len(expected)
    assert ctx.api.get(url,headers={'Range':f'bytes={len(full.content)}-'}).status_code==416
    for value in ['bytes=0-1,2-3','bytes=-','bytes=2-1','nonsense']:
        assert ctx.api.get(url,headers={'Range':value}).status_code==400
    assert ctx.api.get(url,headers={'Range':'bytes=0-1','If-Range':'"other"'}).status_code==200
    assert ctx.api.get(url,headers={'Range':'bytes=0-1','If-Range':full.headers['etag']}).status_code==206
    assert ctx.api.get(base+'/audio-preview?expected_revision=0').status_code==409
    assert ctx.api.request('DELETE',base,json={'expected_revision':1,'operation_id':uid()}).status_code==403
    assert ctx.api.options(base,headers={'Origin':'http://localhost:5173'}).headers['access-control-allow-methods'].find('DELETE')>=0
    deleted=ctx.api.request('DELETE',base,headers=ctx.token,json={'expected_revision':1,'operation_id':uid()})
    assert deleted.status_code==200 and ctx.api.get(url).status_code==409
    with TestClient(ctx.app,base_url='http://127.0.0.1:8765',client=('192.0.2.1',40000)) as remote:
        assert remote.get(url,headers={'X-Forwarded-For':'127.0.0.1'}).status_code==403


def test_capture_conflict_has_no_phantom_meeting_jobs_and_cancel(ctx):
    command=StartEnrollmentRecording(consent_confirmed=True,microphone_id='synthetic',operation_id=uid())
    start=ctx.service.start_recording(ctx.person,command)
    assert ctx.service.start_recording(ctx.person,command)==start
    meeting=ctx.db.create_meeting('Synthetic')['id']
    before=ctx.db.meeting(meeting)
    response=ctx.api.post(f'/api/v1/meetings/{meeting}/recording/start',headers=ctx.token,json={'microphone_id':'synthetic'})
    assert response.status_code==409 and ctx.db.meeting(meeting)==before and ctx.db.rows('SELECT * FROM jobs')==[]
    stop=StopEnrollmentRecording(recording_id=start.recording_id,generation=start.generation,disposition='cancel',operation_id=uid())
    ctx.service.stop_recording(ctx.person,stop)
    ctx.service._executor.submit(lambda:None).result(2)
    assert ctx.service.recording_state(ctx.person).status=='cancelled'
    assert ctx.app.state.capture_lease.owner is None
    assert ctx.service.repository.get(ctx.person,start.enrollment_id).status=='cancelled'
    assert ctx.service.stop_recording(ctx.person,stop).recording_id==start.recording_id
    later=ctx.service.start_recording(ctx.person,StartEnrollmentRecording(consent_confirmed=True,microphone_id='synthetic',operation_id=uid()))
    ctx.service.stop_recording(ctx.person,stop)
    assert ctx.sample.active==later.recording_id


def test_capture_timeout_holds_lease_and_crash_removes_only_own_staging(ctx):
    start=ctx.service.start_recording(ctx.person,StartEnrollmentRecording(consent_confirmed=True,microphone_id='synthetic',operation_id=uid()))
    folder=ctx.service.staging(start.recording_id,create=True)
    (folder/'microphone_000000.wav.part').write_bytes(b'synthetic-incomplete')
    original=ctx.service.settings.data_dir/'audio'/'synthetic-original.wav'
    original.parent.mkdir()
    original.write_bytes(b'preserved')
    ctx.sample.timeout=True
    with pytest.raises(EnrollmentFailure,match='capture_cleanup_pending'):
        ctx.service.stop_recording(ctx.person,StopEnrollmentRecording(recording_id=start.recording_id,generation=1,disposition='review',operation_id=uid()))
    assert ctx.app.state.capture_lease.owner is not None
    ctx.sample.timeout=False
    ctx.sample.stop(start.recording_id)
    ctx.service._executor.submit(lambda:None).result(2)
    ctx.service.recover()
    assert not folder.exists() and original.read_bytes()==b'preserved'
    assert ctx.service.repository.get(ctx.person,start.enrollment_id).status!='ready'


def test_delete_cleanup_failure_is_truthful_and_recovery_retries(ctx,monkeypatch):
    draft=ctx.service.upload(ctx.person,b'a',EnrollCommand(consent_confirmed=True,operation_id=uid()))
    original=ctx.store.delete
    monkeypatch.setattr(ctx.store,'delete',lambda *args: (_ for _ in ()).throw(VoiceError('private_cleanup_pending')))
    command=DeleteEnrollment(expected_revision=1,operation_id=uid())
    result=ctx.service.delete(ctx.person,draft.id,command)
    assert result.status=='cleanup_pending' and not result.consent_confirmed
    assert ctx.service.delete(ctx.person,draft.id,command)==result
    monkeypatch.setattr(ctx.store,'delete',original)
    ctx.service.recover()
    assert ctx.service.repository.get(ctx.person,draft.id).status=='revoked'


def test_exact_upload_file_and_envelope_caps_negative_length(ctx):
    assert upload(ctx,b'x'*(MAX_PAYLOAD+1)).status_code==413
    assert ctx.api.post(f'/api/v1/participants/{ctx.person}/enrollments',headers={**ctx.token,'Content-Length':str(MAX_PAYLOAD+65537)},content=b'').status_code==413
    assert ctx.api.post(f'/api/v1/participants/{ctx.person}/enrollments',headers={**ctx.token,'Content-Length':'-1'},content=b'').status_code==400


def test_streamed_body_cap_without_contentlength():
    import asyncio
    sent=[]
    async def app(scope,receive,send):
        while True:
            message=await receive()
            if not message.get('more_body'):
                break
    chunks=iter([{'type':'http.request','body':b'x'*(MAX_PAYLOAD),'more_body':True},
                 {'type':'http.request','body':b'x'*65537,'more_body':False}])
    async def receive():return next(chunks)
    async def send(message):sent.append(message)
    asyncio.run(BodyLimitMiddleware(app,2**31)({'type':'http','method':'POST','path':f'/api/v1/participants/{uid()}/enrollments','headers':[]},receive,send))
    assert sent[0]['status']==413


def test_ready_candidate_snapshot_and_generation_mapping_checks(ctx):
    draft=ctx.service.upload(ctx.person,b'a',EnrollCommand(consent_confirmed=True,operation_id=uid()))
    ready=ctx.service.confirm(ctx.person,draft.id,confirm_command(draft))
    snapshot=ctx.service.repository.voice_snapshot()
    item=snapshot[1][0]
    candidate=ctx.service.read_candidate(ctx.person,ready.id,expected_revision=2,expected_profile_revision=item['profile_revision'])
    assert candidate.profile_id==ctx.person and candidate.material_revision==2
    ctx.service.repository.revalidate_voice_snapshot(snapshot)
    ctx.db.execute('UPDATE voice_enrollments SET storage_key=? WHERE id=?',(uid(),ready.id))
    with pytest.raises(EnrollmentFailure,match='material_mapping_mismatch'):
        ctx.service.read_candidate(ctx.person,ready.id,expected_revision=2,expected_profile_revision=item['profile_revision'])
    with pytest.raises(EnrollmentFailure,match='stale_voice_snapshot'):
        ctx.service.repository.revalidate_voice_snapshot(snapshot)


def test_crash_cipherpart_receipt_cleanup_preserves_foreign_hardlink_rejection(ctx,monkeypatch):
    import os
    generation=uid()
    with ctx.db.transaction() as conn:
        enrollment_id=ctx.service.repository.insert(conn,ctx.person)
    row,_=ctx.service.repository.snapshot(ctx.person,enrollment_id,('pending',))
    ctx.service.repository.ledger(row,generation)
    folder=ctx.store.root/ctx.person
    folder.mkdir(parents=True,exist_ok=True)
    part=folder/('.'+uid()+'.cipherpart')
    part.write_bytes(b'synthetic-ciphertext-incomplete')
    info=part.stat()
    ctx.service._track_partial(ctx.person,generation,part.name,(info.st_dev,info.st_ino))
    foreign=folder/'foreign-link'
    os.link(part,foreign)
    ctx.service.recover()
    assert part.exists() and foreign.exists()
    assert ctx.db.rows('SELECT * FROM enrollment_materials') and ctx.db.rows('SELECT * FROM enrollment_partials')
    foreign.unlink()
    ctx.service.recover()
    assert not part.exists() and not ctx.db.rows('SELECT * FROM enrollment_materials')


def test_dpapi_failure_leaves_no_plaintext_or_ready_material(ctx,monkeypatch):
    monkeypatch.setattr(ctx.store.cipher,'protect',lambda plain: (_ for _ in ()).throw(VoiceError('dpapi_unavailable')))
    response=upload(ctx)
    assert response.status_code==422 and response.json()['reason_codes']==['dpapi_unavailable']
    assert not ctx.db.rows('SELECT * FROM enrollment_materials')
    assert not list(ctx.service.settings.data_dir.rglob('*.dpapi'))
    assert ctx.service.repository.list(ctx.person)[0].status=='failed'


def test_enrollment_audio_native_wall_frame_snapshot_limits_and_channels(tmp_path):
    from test_audio import FakeAudio, settings
    from secretary.infrastructure.audio import AudioCapture
    module=FakeAudio()
    capture=AudioCapture(settings(tmp_path),audio_module=module,folder_factory=lambda identifier:tmp_path/'voice'/'staging'/identifier,
                         max_seconds=30,max_channels=2,chunk_seconds=30)
    identifier=uid()
    capture.start(identifier,'wasapi:3')
    session=capture._session
    # Fake native stream, synthetic bounded blocks; frame budget rejects excess.
    for _ in range(61):
        if session.stop_event.is_set():break
        session.callback('microphone')(struct.pack('<h',2000)*4096,4096,None,0)
    session.thread.join(3)
    assert not session.thread.is_alive() and session.snapshot()['duration_ms']<=30000
    assert session.writers['microphone'].total_frames<=30*8000 and session.native_closed
    second=uid()
    capture.start(second,'wasapi:3')
    capture._session.started-=31
    assert capture._session.snapshot()['duration_ms']==30000
    capture._session.thread.join(2)
    assert not capture._session.thread.is_alive()
    capture.close()


def test_enrollment_capture_rejects_above_two_native_channels(tmp_path):
    from test_audio import FakeAudio, FakeManager, settings
    from secretary.infrastructure.audio import AudioCapture
    module=FakeAudio()
    original=module.PyAudio
    def factory():
        manager=original()
        enumerate_original=manager.get_device_info_generator
        manager.get_device_info_generator=lambda:iter([{**device,'maxInputChannels':4} if device['index']==3 else device for device in enumerate_original()])
        return manager
    module.PyAudio=factory
    capture=AudioCapture(settings(tmp_path),audio_module=module,max_seconds=30,max_channels=2,
                         folder_factory=lambda identifier:tmp_path/'voice'/'staging'/identifier)
    with pytest.raises(ValueError):capture.start(uid(),'wasapi:3')
    assert module.managers[-1].terminated


def test_capture_success_draft_then_cancel_next_generation(ctx):
    start=ctx.service.start_recording(ctx.person,StartEnrollmentRecording(consent_confirmed=True,microphone_id='synthetic',operation_id=uid()))
    folder=ctx.service.staging(start.recording_id,create=True)
    path=folder/'microphone_000000.wav'
    path.write_bytes(b'synthetic-source')
    ctx.sample.chunks=[{'path':str(path)}]
    ctx.service.stop_recording(ctx.person,StopEnrollmentRecording(recording_id=start.recording_id,generation=1,disposition='review',operation_id=uid()))
    ctx.service._executor.submit(lambda:None).result(2)
    assert ctx.service.recording_state(ctx.person).status=='awaiting_review'
    assert ctx.service.repository.get(ctx.person,start.enrollment_id).status=='awaiting_review'
    assert not folder.exists() and ctx.db.rows('SELECT * FROM meetings')==ctx.db.rows('SELECT * FROM jobs')==[]
    following=ctx.service.start_recording(ctx.person,StartEnrollmentRecording(consent_confirmed=True,microphone_id='synthetic',operation_id=uid()))
    with pytest.raises(EnrollmentFailure,match='stale_recording'):
        ctx.service.stop_recording(ctx.person,StopEnrollmentRecording(recording_id=following.recording_id,generation=0,disposition='cancel',operation_id=uid()))
    assert ctx.sample.active==following.recording_id


def test_delete_signals_task4_dependency_slot_and_reports_worker_cleanup(ctx):
    draft=ctx.service.upload(ctx.person,b'a',EnrollCommand(consent_confirmed=True,operation_id=uid()))
    entered, release, cancel=threading.Event(),threading.Event(),threading.Event()
    def identify():
        with ctx.app.state.voice_inference.slot('synthetic-run',cancel,material_ids=(draft.id,)):
            entered.set()
            assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(identify)
        assert entered.wait(1)
        result=ctx.service.delete(ctx.person,draft.id,DeleteEnrollment(expected_revision=1,operation_id=uid()))
        assert cancel.is_set() and result.status=='cleanup_pending'
        release.set()
        future.result(2)
        ctx.service._executor.submit(lambda:None).result(2)
    assert ctx.service.repository.get(ctx.person,draft.id).status=='revoked'


def test_native_failed_close_holds_lease_until_verified_retry(tmp_path):
    from test_audio import FakeAudio,settings
    from secretary.infrastructure.audio import AudioCapture
    from secretary.infrastructure.voice_resources import CaptureLease,LeasedCapture
    module=FakeAudio()
    capture=AudioCapture(settings(tmp_path),audio_module=module)
    lease=CaptureLease()
    adapter=LeasedCapture(capture,lease,'enrollment')
    identifier=uid()
    adapter.start(identifier,'wasapi:3')
    stream=module.managers[-1].streams[0]
    original=stream.close
    stream.close=lambda: (_ for _ in ()).throw(RuntimeError('synthetic driver close failure'))
    stopped=adapter.stop(identifier)
    assert stopped['native_closed'] is False and lease.owner==('enrollment',identifier)
    with pytest.raises(EnrollmentFailure,match='capture_busy'):
        lease.acquire(('meeting',uid()))
    stream.close=original
    assert adapter.stop(identifier)['native_closed'] is True and lease.owner is None


def test_crash_plaintext_policy_and_cipherpart_finalize_two_name_receipt(ctx):
    import os
    generation=uid()
    with ctx.db.transaction() as conn:
        enrollment_id=ctx.service.repository.insert(conn,ctx.person)
        recording_id=uid()
        conn.execute("INSERT INTO enrollment_recordings(id,profile_id,enrollment_id,generation,status,created_at) VALUES(?,?,?,1,'recording','synthetic')",(recording_id,ctx.person,enrollment_id))
    folder=ctx.service.staging(recording_id,create=True)
    (folder/'microphone_000000.wav.part').write_bytes(b'synthetic-interrupted')
    row,_=ctx.service.repository.snapshot(ctx.person,enrollment_id,('pending',))
    ctx.service.repository.ledger(row,generation)
    parent=ctx.store.root/ctx.person
    parent.mkdir(parents=True,exist_ok=True)
    part=parent/('.'+uid()+'.cipherpart')
    part.write_bytes(b'synthetic-complete-owned')
    info=part.stat()
    ctx.service._track_partial(ctx.person,generation,part.name,(info.st_dev,info.st_ino))
    final=parent/(generation+'.dpapi')
    os.link(part,final)
    # Restart with a fresh store loses3a in-memory proof, uses durable exact receipt.
    ctx.service.store=VoiceStore(ctx.service.settings.data_dir,cipher=Cipher())
    ctx.service.recover()
    assert not part.exists() and not folder.exists()
    assert ctx.service.recording_state(ctx.person).status=='interrupted'
    assert ctx.service.repository.get(ctx.person,enrollment_id).status=='interrupted'
    # Invalid synthetic final cannot pass envelope ownership: retained, ledger pending.
    assert final.exists() and ctx.db.rows("SELECT * FROM enrollment_materials WHERE state='cleanup_pending'")


def test_failed_shutdown_still_closes_both_captures_and_service(ctx,monkeypatch):
    async def exercise():
        async with ctx.app.router.lifespan_context(ctx.app):
            pass
    ctx.meeting.close=lambda: (_ for _ in ()).throw(RuntimeError('synthetic-close'))
    import asyncio
    with pytest.raises(RuntimeError,match='synthetic-close'):
        asyncio.run(exercise())
    assert ctx.sample.closed and ctx.service._closed
    # Fixture shutdown itself must be allowed to finish.
    ctx.meeting.close=lambda:None


def test_operation_ids_are_global_across_metadata_and_enrollment_commands(ctx):
    operation=uid()
    assert upload(ctx,operation_id=operation).status_code==201
    response=ctx.api.post('/api/v1/participants',headers=ctx.token,json={'display_name':'Other','operation_id':operation})
    assert response.status_code==409
    other=uid()
    assert ctx.api.post('/api/v1/participants',headers=ctx.token,json={'display_name':'Other','operation_id':other}).status_code==201
    assert upload(ctx,operation_id=other).status_code==409


def test_confirm_inference_is_off_http_event_loop(ctx):
    draft=upload(ctx).json()
    ctx.engine.block=True
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(ctx.api.post,f'/api/v1/participants/{ctx.person}/enrollments/{draft["id"]}/confirm',
            headers=ctx.token,json={'expected_revision':1,'listening_confirmed':True,'single_speaker_confirmed':True,'operation_id':uid()})
        assert ctx.engine.entered.wait(2)
        started=time.monotonic()
        assert ctx.api.get('/health').status_code==200
        assert time.monotonic()-started<1
        ctx.engine.release.set()
        assert future.result(3).status_code==200


def test_recording_timeout_retry_releases_device_without_claiming_success(ctx):
    start=ctx.service.start_recording(ctx.person,StartEnrollmentRecording(consent_confirmed=True,microphone_id='synthetic',operation_id=uid()))
    ctx.sample.timeout=True
    with pytest.raises(EnrollmentFailure):
        ctx.service.stop_recording(ctx.person,StopEnrollmentRecording(recording_id=start.recording_id,generation=1,disposition='cancel',operation_id=uid()))
    assert ctx.app.state.capture_lease.owner is not None
    ctx.sample.timeout=False
    ctx.service.stop_recording(ctx.person,StopEnrollmentRecording(recording_id=start.recording_id,generation=1,disposition='cancel',operation_id=uid()))
    ctx.service._executor.submit(lambda:None).result(2)
    assert ctx.app.state.capture_lease.owner is None and ctx.service.recording_state(ctx.person).status=='cancelled'


@pytest.mark.parametrize('foreign_kind',['existing','missing'])
def test_confirm_delete_http_receipts_require_exact_profile_scope(ctx,foreign_kind,monkeypatch):
    draft=upload(ctx).json()
    confirm={'expected_revision':1,'listening_confirmed':True,'single_speaker_confirmed':True,'operation_id':uid()}
    base=f'/api/v1/participants/{ctx.person}/enrollments/{draft["id"]}'
    ready=ctx.api.post(base+'/confirm',headers=ctx.token,json=confirm)
    assert ready.status_code==200
    other=uid() if foreign_kind=='missing' else ctx.api.post('/api/v1/participants',headers=ctx.token,
        json={'display_name':'Other synthetic','operation_id':uid()}).json()['id']
    foreign=f'/api/v1/participants/{other}/enrollments/{draft["id"]}'
    wrong=ctx.api.post(foreign+'/confirm',headers=ctx.token,json=confirm)
    assert wrong.status_code==409 and wrong.json()['detail']=='operation_payload_mismatch'
    assert ctx.person not in wrong.text and draft['id'] not in wrong.text and ctx.engine.calls==1
    deletes=[]
    original=ctx.store.delete
    def tracked(*args):
        deletes.append(args)
        return original(*args)
    monkeypatch.setattr(ctx.store,'delete',tracked)
    delete={'expected_revision':2,'operation_id':uid()}
    removed=ctx.api.request('DELETE',base,headers=ctx.token,json=delete)
    assert removed.status_code==200
    count=len(deletes)
    wrong=ctx.api.request('DELETE',foreign,headers=ctx.token,json=delete)
    assert wrong.status_code==409 and wrong.json()['detail']=='operation_payload_mismatch'
    assert ctx.person not in wrong.text and draft['id'] not in wrong.text and len(deletes)==count
    revision=ctx.db.one('SELECT revision FROM person_profiles WHERE id=?',(ctx.person,))['revision']
    ctx.app.state.speakers.patch_profile(ctx.person,PatchProfile(enabled=False,expected_revision=revision,operation_id=uid()))
    assert ctx.api.post(base+'/confirm',headers=ctx.token,json=confirm).content==ready.content
    assert ctx.api.request('DELETE',base,headers=ctx.token,json=delete).content==removed.content
    assert ctx.engine.calls==1 and len(deletes)==count


@pytest.mark.parametrize('kind,status,reason',[('upload',422,'invalid_audio'),('missing',404,'profile_not_found'),('stale',409,'stale_material')])
def test_failed_http_operation_replays_exact_status_and_body(ctx,kind,status,reason):
    operation=uid()
    if kind=='upload':
        invoke=lambda:upload(ctx,b'bad',operation_id=operation)
    else:
        profile=uid() if kind=='missing' else ctx.person
        enrollment=uid() if kind=='missing' else upload(ctx).json()['id']
        body={'expected_revision':0,'listening_confirmed':True,'single_speaker_confirmed':True,'operation_id':operation}
        invoke=lambda:ctx.api.post(f'/api/v1/participants/{profile}/enrollments/{enrollment}/confirm',headers=ctx.token,json=body)
    first,second=invoke(),invoke()
    assert first.status_code==status and second.status_code==status
    assert first.content==second.content and first.json()['detail']==reason
    assert ctx.engine.calls==0


@pytest.mark.parametrize('failure',['selection','enumeration','session'])
@pytest.mark.parametrize('retry',['stop','close'])
def test_pre_session_native_manager_cleanup_retries_exact_owner(tmp_path,monkeypatch,failure,retry):
    from test_audio import FakeAudio,settings
    import secretary.infrastructure.audio as audio
    from secretary.infrastructure.voice_resources import CaptureLease,LeasedCapture
    module=FakeAudio()
    manager=module.PyAudio()
    module.PyAudio=lambda:manager
    original=manager.terminate
    calls=[]
    def failed_terminate():
        calls.append('failed')
        raise RuntimeError('synthetic terminate failure')
    manager.terminate=failed_terminate
    if failure=='enumeration':
        manager.get_host_api_info_by_type=lambda *args: (_ for _ in ()).throw(RuntimeError('synthetic enumeration failure'))
    elif failure=='session':
        monkeypatch.setattr(audio,'_Session',lambda *args,**kwargs: (_ for _ in ()).throw(ValueError('synthetic session construction failure')))
    capture=audio.AudioCapture(settings(tmp_path),audio_module=module)
    lease=CaptureLease()
    adapter=LeasedCapture(capture,lease,'enrollment')
    identifier=uid()
    with pytest.raises(Exception):
        adapter.start(identifier,'wasapi:missing' if failure=='selection' else 'wasapi:3')
    assert lease.owner==('enrollment',identifier) and not capture.state(identifier)['native_closed']
    adapter.stop(uid())
    assert calls==['failed'] and not manager.terminated
    def restored():
        calls.append('restored')
        original()
    manager.terminate=restored
    try:
        if retry=='stop':
            assert adapter.stop(identifier)['native_closed'] is True
        else:
            adapter.close()
            assert capture.state(identifier)['native_closed'] is True
        assert manager.terminated and calls==['failed','restored'] and lease.owner is None
        adapter.close()
        assert calls==['failed','restored']
    finally:
        original()


def test_native_manager_constructor_failure_stays_unconfirmed_and_owned(tmp_path):
    from test_audio import FakeAudio,settings
    from secretary.infrastructure.audio import AudioCapture
    from secretary.infrastructure.voice_resources import CaptureLease,LeasedCapture
    module=FakeAudio()
    module.PyAudio=lambda: (_ for _ in ()).throw(RuntimeError('synthetic uncertain constructor'))
    capture=AudioCapture(settings(tmp_path),audio_module=module)
    lease=CaptureLease()
    adapter=LeasedCapture(capture,lease,'enrollment')
    identifier=uid()
    with pytest.raises(RuntimeError):adapter.start(identifier,'wasapi:3')
    assert adapter.stop(identifier)['native_closed'] is False and lease.owner==('enrollment',identifier)
    with pytest.raises(RuntimeError,match='cleanup pending'):adapter.close()
    assert lease.owner==('enrollment',identifier)


def test_enrollment_http_start_pending_manager_get_cancel_retry_and_shutdown(ctx,monkeypatch):
    from test_audio import FakeAudio
    from secretary.infrastructure.audio import AudioCapture
    module=FakeAudio()
    manager=module.PyAudio()
    module.PyAudio=lambda:manager
    original=manager.terminate
    manager.terminate=lambda: (_ for _ in ()).throw(RuntimeError('synthetic native terminate failure'))
    native=AudioCapture(ctx.service.settings,audio_module=module,folder_factory=lambda identifier:ctx.service.staging(identifier,create=True),max_seconds=30,max_channels=2)
    # Keep existing app-level lease/adapter and service references; only fake-native target changes.
    ctx.service.capture.capture=native
    ctx.service.capture.legacy=False
    base=f'/api/v1/participants/{ctx.person}/enrollment-recording'
    command={'consent_confirmed':True,'microphone_id':'wasapi:missing','operation_id':uid()}
    first=ctx.api.post(base+'/start',headers=ctx.token,json=command)
    try:
        assert first.status_code==409 and first.json()['detail']=='capture_cleanup_pending'
        assert ctx.api.post(base+'/start',headers=ctx.token,json=command).content==first.content
        state=ctx.api.get(base).json()
        assert state['status']=='cleanup_pending' and state['reason_codes']==['capture_cleanup_pending']
        manager.terminate=original
        stopped=ctx.api.post(base+'/stop',headers=ctx.token,json={'recording_id':state['recording_id'],'generation':state['generation'],'disposition':'cancel','operation_id':uid()})
        assert stopped.status_code==200
        ctx.service._executor.submit(lambda:None).result(2)
        assert ctx.api.get(base).json()['status']=='cancelled'
        assert manager.terminated and ctx.app.state.capture_lease.owner is None
    finally:
        manager.terminate=original
        ctx.service.capture.close()


def test_schema5_receipt_compatibility_migration_is_idempotent_and_keeps_history(ctx):
    from secretary.infrastructure.database import Database
    draft=upload(ctx).json()
    body={'expected_revision':1,'listening_confirmed':True,'single_speaker_confirmed':True,'operation_id':uid()}
    base=f'/api/v1/participants/{ctx.person}/enrollments/{draft["id"]}'
    ready=ctx.api.post(base+'/confirm',headers=ctx.token,json=body)
    assert ready.status_code==200
    delete={'expected_revision':2,'operation_id':uid()}
    removed=ctx.api.request('DELETE',base,headers=ctx.token,json=delete)
    old_error=uid()
    assert upload(ctx,b'bad',operation_id=old_error).status_code==422
    # Recreate exactly the old internal scope/column layout in this synthetic DB.
    ctx.db.execute('UPDATE enrollment_commands SET scope=? WHERE operation_id IN (?,?)',(draft['id'],body['operation_id'],delete['operation_id']))
    ctx.db.execute('ALTER TABLE enrollment_commands DROP COLUMN error_status')
    ctx.db.execute('DELETE FROM schema_migrations WHERE version=6')
    historic=ctx.db.rows('SELECT operation_id,kind,request_hash,response,error FROM enrollment_commands ORDER BY operation_id')
    migrated=Database(ctx.db.path)
    Database(ctx.db.path)
    assert historic==migrated.rows('SELECT operation_id,kind,request_hash,response,error FROM enrollment_commands ORDER BY operation_id')
    assert [r['version'] for r in migrated.rows('SELECT version FROM schema_migrations ORDER BY version')]==[1,2,3,4,5,6,7,8]
    assert migrated.one('SELECT error_status FROM enrollment_commands WHERE operation_id=?',(old_error,))['error_status']==409
    assert ctx.api.post(base+'/confirm',headers=ctx.token,json=body).content==ready.content
    assert ctx.api.request('DELETE',base,headers=ctx.token,json=delete).content==removed.content
    legacy=upload(ctx,b'bad',operation_id=old_error)
    assert legacy.status_code==409 and legacy.json()['detail']=='invalid_audio'
    foreign=f'/api/v1/participants/{uid()}/enrollments/{draft["id"]}'
    assert ctx.api.post(foreign+'/confirm',headers=ctx.token,json=body).status_code==409
    assert ctx.api.request('DELETE',foreign,headers=ctx.token,json=delete).status_code==409


def test_enrollment_shutdown_retries_retained_pre_session_manager(ctx):
    from test_audio import FakeAudio
    from secretary.infrastructure.audio import AudioCapture
    module=FakeAudio()
    manager=module.PyAudio()
    module.PyAudio=lambda:manager
    original=manager.terminate
    manager.terminate=lambda: (_ for _ in ()).throw(RuntimeError('synthetic first termination'))
    native=AudioCapture(ctx.service.settings,audio_module=module,folder_factory=lambda identifier:ctx.service.staging(identifier,create=True),max_seconds=30,max_channels=2)
    ctx.service.capture.capture=native
    ctx.service.capture.legacy=False
    response=ctx.api.post(f'/api/v1/participants/{ctx.person}/enrollment-recording/start',headers=ctx.token,
        json={'consent_confirmed':True,'microphone_id':'wasapi:missing','operation_id':uid()})
    assert response.status_code==409
    manager.terminate=original
    ctx.service.capture.close()
    ctx.service.close()
    assert manager.terminated and ctx.app.state.capture_lease.owner is None
    state=ctx.service.repository.recording(ctx.db.one('SELECT * FROM enrollment_recordings'))
    assert state.status=='interrupted'
