"""Synthetic ASGI -> existing Worker -> native child -> SQLite/export checks.

Run through scripts/run_local_asr_tests.py. The runtime supplies only the checked
in synthetic Python stage; actual model, capture and HTTP transports are denied.
No live server or live application database is used.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import psutil
import pytest

from secretary.api import create_app
from secretary.application.local_asr_execution_protocol import StageSpec
from secretary.application.local_asr_executor import LocalAsrExecutor
from secretary.application.local_asr_lifecycle import ExecutionPlan
from secretary.domain.local_asr import model_manifest_digest
from secretary.domain.speakers import ReviewAttribution
from secretary.infrastructure.local_asr_publication import publish_execution_result
from secretary.infrastructure.local_asr_resources import ResourceSnapshot
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.settings import Settings
from test_local_asr_api import FIXTURE, NoCapture, NoEngine, business_snapshot, denied_provider, seed_meeting, synthetic_wav
from test_local_asr_executor import GPU, SCRIPT, SyntheticRuntime, sample_resources


class InitialSyntheticRuntime(SyntheticRuntime):
    """A first transcript still comes from a harmless real subprocess."""

    def plan_factory(self, meeting_id, transcript_version, input_sha256, source_text_sha256):
        assert source_text_sha256 is None
        return ExecutionPlan(
            input_sha256=input_sha256, source_text_sha256=None,
            profile_digest=self.identity.profile_digest, model_digest=model_manifest_digest(self.identity.models),
            code_digest=self.identity.code_digest, asr_device='cpu', diarization_device='cuda', physical_gpu_id=GPU,
            precision=self.identity.precision, stages=('asr_rnnt', 'diarize', 'align_rnnt', 'assemble', 'validate'),
            deadline_at=time.time()+self.deadline_seconds, nemotron_free_vram_mib=1, max_attempts=1)

    def build_attempt(self, plan, context, workspace):
        assert context.source_segments == ()
        self.workspaces.append(workspace)
        payload=copy.deepcopy(self.payload)
        payload['operation_id']=context.operation_id
        payload['transcript_version']=context.transcript_version
        payload['result']['input_sha256']=context.input_sha256
        payload['artifacts']['input_sha256']=context.input_sha256
        payload['artifacts']['audio_ref']=f'meeting:{context.meeting_id}'
        for ordinal, source in enumerate(payload['artifacts']['source_segments']):
            source['segment_id']=f'{context.meeting_id}-synthetic-{ordinal}'
            payload['artifacts']['segment_sources'][ordinal]['source_segment_ids']=[source['segment_id']]
        request=workspace/'synthetic-request.json'
        request.write_text(json.dumps({'payload':payload, 'input_path':str(context.input_path)}, ensure_ascii=False), encoding='utf-8')
        stages=[]
        prior=None
        for stage in plan.stages[:-1]:
            output=workspace/(stage+'.json')
            command=[sys.executable, '-B', str(SCRIPT), '--stage', stage, '--request', str(request),
                     '--output', str(output), '--counter', str(self.counter)]
            if prior is not None:
                command+=['--previous', str(prior)]
            stages.append(StageSpec(stage, tuple(command), (output.name,), timeout_seconds=30))
            prior=output
        return tuple(stages)


@pytest.fixture
def make_case(tmp_path, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError('Real HTTP transport forbidden in synthetic ASGI integration')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', deny_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', deny_network)
    monkeypatch.delenv('SECRETARY_LOCAL_ASR_PROFILE', raising=False)
    stack=ExitStack()

    def make(*, initial=False, unseeded=False, mode='success', deadline_seconds=60, resource_snapshot=sample_resources):
        settings=Settings(_env_file=None, data_dir=tmp_path/'data', cloud_enabled=False, polza_api_key='',
                          local_cost_limits_enabled=False, polza_limits_enabled=False)
        payload=json.loads(FIXTURE.read_text(encoding='utf-8'))
        runtime_type=InitialSyntheticRuntime if initial else SyntheticRuntime
        runtime=runtime_type(payload, tmp_path, mode=mode, deadline_seconds=deadline_seconds, max_attempts=1)

        def executor_factory(db, configured_settings, configured_runtime):
            assert configured_settings.data_dir == settings.data_dir and configured_runtime is runtime
            return LocalAsrExecutor(
                db, configured_settings, runtime, shared_resource_root=tmp_path/'shared-gpu', resource_snapshot=resource_snapshot,
                workspace_root=settings.data_dir/'local-asr-work',
                publish_result=lambda conn, context, command, trusted_sha: publish_execution_result(
                    db, conn, context, command, trusted_sha))

        app=create_app(settings, provider_factory=denied_provider, capture=NoCapture(), enrollment_capture=NoCapture(),
                       voice_engine=NoEngine(), run_worker=False, outbound_enabled=False, enforce_single_instance=False,
                       local_asr_runtime=runtime, local_asr_executor_factory=executor_factory)
        # TestClient is an in-process ASGI transport, never a socket connection.
        client=stack.enter_context(TestClient(app, base_url='http://127.0.0.1:61983'))
        db=app.state.db
        headers={'X-Secretary-Token':client.get('/api/v1/session').json()['csrf_token']}
        if unseeded:
            assert initial
            return SimpleNamespace(app=app, db=db, client=client, headers=headers, settings=settings,
                                   runtime=runtime, worker=app.state.worker, payload=payload)
        if initial:
            meeting_id=db.create_meeting('Synthetic first local transcript')['id']
            folder=settings.data_dir/'uploads'/meeting_id
            folder.mkdir(parents=True)
            path=folder/'synthetic.wav'
            path.write_bytes(synthetic_wav())
            input_sha=hashlib.sha256(path.read_bytes()).hexdigest()
            db.add_chunk(meeting_id, dict(sequence=0, channel='import', path=path, offset_ms=0, duration_ms=5000, sha256=input_sha))
            db.update_meeting(meeting_id, status='ready', duration_ms=5000, media_path=str(path))
            seeded=dict(meeting_id=meeting_id, payload=payload, path=path, input_sha256=input_sha)
        else:
            seeded=seed_meeting(app, settings)
            SpeakerRepository(db).review_attribution(seeded['meeting_id'], ReviewAttribution(
                operation_id='preserve-manual-source', transcript_version=1, expected_revision=0,
                changes=[{'segment_id':seeded['payload']['artifacts']['source_segments'][0]['segment_id'],
                          'participant_id':seeded['participant_id']}]))
        runtime.payload=copy.deepcopy(seeded['payload'])
        runtime.payload['artifacts']['identity']=runtime.identity.model_dump(mode='json')
        return SimpleNamespace(app=app, db=db, client=client, headers=headers, settings=settings, runtime=runtime,
                               worker=app.state.worker, before=business_snapshot(db),
                               base=f"/api/v1/meetings/{seeded['meeting_id']}/local-asr", **seeded)

    yield make
    stack.close()


def prepare(case, operation_id='http-native-execution'):
    response=case.client.post(case.base+'/jobs', json={'operation_id':operation_id, 'transcript_version':1}, headers=case.headers)
    assert response.status_code == 202, response.text
    return response.json()


def job_view(case, job_id):
    response=case.client.get(case.base+'/jobs/'+job_id)
    assert response.status_code == 200, response.text
    return response.json()


def assert_owned_processes_exited(case):
    pids=[json.loads(line)['pid'] for line in case.runtime.counter.read_text(encoding='utf-8').splitlines()]
    pids += [row['owner_pid'] for row in case.db.rows('SELECT owner_pid FROM local_asr_attempts')]
    assert pids and all(pid != os.getpid() and not psutil.pid_exists(pid) for pid in pids)


def test_http_worker_native_execution_exports_once_and_preserves_paid_text_and_manual_decisions(make_case):
    case=make_case()
    assert all(row['text'] for row in case.before['segments'])
    assert case.before['summaries'] and case.before['usage'] and case.before['summary_checkpoints']
    assert len(case.before['task_assignments']) == 2 and case.before['speaker_attributions']
    queued=prepare(case)
    assert queued['state'] == 'queued' and queued['attempt_count'] == 0, queued
    assert prepare(case)['job_id'] == queued['job_id']
    assert case.client.get(case.base+'/runs').json() == []
    assert asyncio.run(case.worker.run_once()) is True
    completed=job_view(case, queued['job_id'])
    assert completed['state'] == 'applied' and completed['job_status'] == 'succeeded', completed
    assert completed['attempt_count'] == 1
    runs=case.client.get(case.base+'/runs').json()
    assert len(runs) == 1 and runs[0]['run_id'] == completed['result_run_id']
    path=case.base+'/runs/'+runs[0]['run_id']
    read=case.client.get(path)
    exported=case.client.get(path+'/export')
    assert read.status_code == exported.status_code == 200
    assert exported.json() == read.json()
    artifact=exported.json()
    assert exported.headers['content-disposition'] == 'attachment; filename="local-asr-analysis.json"'
    assert artifact['result'] == case.payload['result']
    assert artifact['artifacts']['selected_text'] == case.payload['artifacts']['selected_text']
    assert artifact['artifacts']['runtime_receipts'][-1]['stage'] == 'synthetic_child_execution'
    assert artifact['source_decisions'][0]['participant_id'] == case.participant_id
    assert case.runtime.counts() == {'diarize':1, 'align_saved_text':1, 'assemble':1}
    assert prepare(case) == completed
    assert asyncio.run(case.worker.run_once()) is False
    assert len(case.db.rows("SELECT id FROM jobs WHERE stage='local_asr'")) == 1
    assert business_snapshot(case.db) == case.before
    assert hashlib.sha256(case.path.read_bytes()).hexdigest() == case.input_sha256
    assert_owned_processes_exited(case)


@pytest.mark.parametrize('stop', ['cancel', 'timeout'])
def test_http_cancel_or_deadline_drains_real_child_and_does_not_publish(make_case, stop):
    case=make_case(mode='hang', deadline_seconds=8 if stop == 'timeout' else 60)
    queued=prepare(case)

    async def run():
        task=asyncio.create_task(case.worker.run_once())
        deadline=time.monotonic()+18
        pids=None
        while not task.done() and time.monotonic() < deadline:
            if case.runtime.marker.exists():
                try:
                    pids=json.loads(case.runtime.marker.read_text(encoding='utf-8'))
                    break
                except json.JSONDecodeError:
                    pass  # The real child may still be flushing its tiny marker.
            await asyncio.sleep(.03)
        assert pids, job_view(case, queued['job_id'])
        assert all(psutil.pid_exists(pid) for pid in pids)
        if stop == 'cancel':
            response=case.client.post(case.base+'/jobs/'+queued['job_id']+'/cancel', headers=case.headers)
            assert response.status_code == 200, response.text
            assert response.json()['cancel_requested']
        assert await asyncio.wait_for(task, 25) is True
        return pids

    pids=asyncio.run(run())
    final=job_view(case, queued['job_id'])
    assert final['state'] == ('cancelled' if stop == 'cancel' else 'timed_out'), final
    assert final['result_run_id'] is None
    assert all(not psutil.pid_exists(pid) for pid in pids)
    assert case.client.get(case.base+'/runs').json() == []
    assert business_snapshot(case.db) == case.before
    assert prepare(case) == final
    assert asyncio.run(case.worker.run_once()) is False
    assert_owned_processes_exited(case)


def test_http_first_transcript_runs_synthetic_asr_then_atomically_installs_local_text_and_route(make_case):
    case=make_case(initial=True)
    assert case.before['segments'] == [] and case.before['meeting_transcript_routes'] == []
    assert case.db.meeting(case.meeting_id)['transcript_version'] == 0
    queued=prepare(case)
    assert queued['state'] == 'queued', queued
    assert case.db.meeting(case.meeting_id)['transcript_version'] == 0
    assert asyncio.run(case.worker.run_once()) is True
    completed=job_view(case, queued['job_id'])
    assert completed['state'] == 'applied' and completed['job_status'] == 'succeeded', completed
    runs=case.client.get(case.base+'/runs').json()
    assert len(runs) == 1
    exported=case.client.get(case.base+'/runs/'+runs[0]['run_id']+'/export')
    assert exported.status_code == 200, exported.text
    artifact=exported.json()
    assert artifact['artifacts']['selected_text'] == case.payload['artifacts']['selected_text']
    sources=artifact['artifacts']['source_segments']
    stored=case.db.rows('SELECT id,text FROM segments WHERE meeting_id=? ORDER BY ordinal', (case.meeting_id,))
    assert stored == [{'id':source['segment_id'], 'text':source['text']} for source in sources]
    route=case.db.one('SELECT route FROM meeting_transcript_routes WHERE meeting_id=?', (case.meeting_id,))
    assert json.loads(route['route'])['provider'] == 'local'
    assert case.db.meeting(case.meeting_id)['status'] == 'ready'
    assert case.db.meeting(case.meeting_id)['transcript_version'] == 1
    assert case.runtime.counts() == {'asr_rnnt':1, 'diarize':1, 'align_rnnt':1, 'assemble':1}
    for table in ('summaries', 'summary_checkpoints', 'usage', 'task_assignments', 'speaker_attributions'):
        assert case.db.rows(f'SELECT * FROM {table}') == []
    assert prepare(case) == completed
    assert asyncio.run(case.worker.run_once()) is False
    assert hashlib.sha256(case.path.read_bytes()).hexdigest() == case.input_sha256
    assert_owned_processes_exited(case)


def test_http_resource_pause_resume_is_meeting_scoped_and_runs_only_after_admission(make_case):
    available=False

    def resources():
        return sample_resources() if available else ResourceSnapshot(1, 1, {GPU:1})

    case=make_case(resource_snapshot=resources)
    queued=prepare(case)
    assert asyncio.run(case.worker.run_once()) is True
    paused=job_view(case, queued['job_id'])
    assert paused['state'] == 'waiting_resources' and paused['job_status'] == 'waiting_config', paused
    assert paused['attempt_count'] == 0 and case.runtime.counts() == {}
    assert case.client.get(case.base+'/runs').json() == []
    assert business_snapshot(case.db) == case.before

    other_id=case.db.create_meeting('Synthetic unrelated meeting')['id']
    foreign=f'/api/v1/meetings/{other_id}/local-asr/jobs/'+queued['job_id']
    assert case.client.get(foreign).status_code == 404
    rejected=case.client.post(foreign+'/resume', headers=case.headers)
    assert rejected.status_code == 404, rejected.text
    assert job_view(case, queued['job_id']) == paused

    available=True
    resumed=case.client.post(case.base+'/jobs/'+queued['job_id']+'/resume', headers=case.headers)
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()['state'] == 'queued'
    assert asyncio.run(case.worker.run_once()) is True
    completed=job_view(case, queued['job_id'])
    assert completed['state'] == 'applied' and completed['attempt_count'] == 1, completed
    assert len(case.client.get(case.base+'/runs').json()) == 1
    assert case.runtime.counts() == {'diarize':1, 'align_saved_text':1, 'assemble':1}
    # The only non-analysis write during this test was the explicit second
    # synthetic meeting used to verify scope; all prior business rows remain.
    after=business_snapshot(case.db)
    after['meetings']=[row for row in after['meetings'] if row['id'] != other_id]
    assert after == case.before
    assert_owned_processes_exited(case)


def test_http_local_upload_runs_worker_and_exports_first_transcript_with_playable_audio(make_case):
    case=make_case(initial=True, unseeded=True)
    created=case.client.post('/api/v1/meetings', json={'title':'Synthetic HTTP local upload'}, headers=case.headers)
    assert created.status_code == 201, created.text
    case.meeting_id=created.json()['id']
    case.base=f'/api/v1/meetings/{case.meeting_id}/local-asr'
    meeting_path=f'/api/v1/meetings/{case.meeting_id}'
    uploaded_bytes=synthetic_wav()
    upload=case.client.post(meeting_path+'/upload?processing_route=local',
        files={'file':('synthetic.wav', uploaded_bytes, 'audio/wav')}, headers=case.headers)
    assert upload.status_code == 202, upload.text
    job_id=upload.json()['job_id']
    queued=job_view(case, job_id)
    assert queued['state'] == 'queued' and queued['attempt_count'] == 0, queued
    before=case.db.meeting(case.meeting_id, internal=True)
    assert before['transcript_version'] == 0 and before['duration_ms'] is None
    original=Path(before['media_path'])
    assert original == case.settings.data_dir/'uploads'/case.meeting_id/'original.media'
    assert original.read_bytes() == uploaded_bytes
    assert case.db.chunks(case.meeting_id) == []
    assert case.db.rows('SELECT * FROM segments') == []
    assert case.db.rows('SELECT * FROM meeting_transcript_routes') == []
    assert case.db.rows("SELECT * FROM jobs WHERE stage!='local_asr'") == []
    assert case.db.rows('SELECT * FROM usage') == []

    assert asyncio.run(case.worker.run_once()) is True
    completed=job_view(case, job_id)
    assert completed['state'] == 'applied' and completed['job_status'] == 'succeeded', completed
    assert completed['attempt_count'] == 1
    meeting=case.client.get(meeting_path)
    assert meeting.status_code == 200, meeting.text
    assert meeting.json()['status'] == 'ready' and meeting.json()['transcript_version'] == 1
    assert meeting.json()['duration_ms'] == 5000
    runs=case.client.get(case.base+'/runs').json()
    assert len(runs) == 1 and runs[0]['run_id'] == completed['result_run_id']
    result_path=case.base+'/runs/'+runs[0]['run_id']
    read=case.client.get(result_path)
    exported=case.client.get(result_path+'/export')
    assert read.status_code == exported.status_code == 200
    assert read.json() == exported.json()
    artifact=exported.json()
    assert artifact['result']['input_sha256'] == hashlib.sha256(uploaded_bytes).hexdigest()
    assert artifact['artifacts']['selected_text'] == case.payload['artifacts']['selected_text']
    assert {receipt['stage'] for receipt in artifact['artifacts']['runtime_receipts']} >= {
        'native_controller', 'synthetic_child_execution', 'canonical_audio'}
    sources=artifact['artifacts']['source_segments']
    assert case.db.rows('SELECT id,text FROM segments ORDER BY ordinal') == [
        {'id':source['segment_id'], 'text':source['text']} for source in sources]
    assert json.loads(case.db.one('SELECT route FROM meeting_transcript_routes')['route'])['provider'] == 'local'
    chunks=case.db.chunks(case.meeting_id)
    assert len(chunks) == 1 and chunks[0]['channel'] == 'import' and chunks[0]['duration_ms'] == 5000
    canonical=Path(chunks[0]['path'])
    assert canonical.is_relative_to(case.settings.data_dir/'audio'/case.meeting_id/'local-asr')
    assert canonical != original and canonical.read_bytes() == uploaded_bytes
    assert case.db.meeting(case.meeting_id, internal=True)['media_path'] == str(original)
    assert original.read_bytes() == uploaded_bytes
    playback=case.client.get(meeting_path+'/audio?channel=import')
    assert playback.status_code == 200 and playback.headers['content-type'] == 'audio/wav'
    assert playback.headers['x-secretary-audio-channel'] == 'import'
    assert playback.content == uploaded_bytes
    ranged=case.client.get(meeting_path+'/audio?channel=import', headers={'Range':'bytes=0-43'})
    assert ranged.status_code == 206 and ranged.content == uploaded_bytes[:44]
    assert ranged.headers['content-range'] == f'bytes 0-43/{len(uploaded_bytes)}'
    assert case.runtime.counts() == {'asr_rnnt':1, 'diarize':1, 'align_rnnt':1, 'assemble':1}
    assert case.db.rows("SELECT * FROM jobs WHERE stage!='local_asr'") == []
    for table in ('usage', 'summaries', 'summary_checkpoints', 'task_assignments', 'speaker_attributions'):
        assert case.db.rows(f'SELECT * FROM {table}') == []
    assert asyncio.run(case.worker.run_once()) is False
    assert_owned_processes_exited(case)
