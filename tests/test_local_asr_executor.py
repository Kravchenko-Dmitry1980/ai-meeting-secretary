"""Actual subprocess/controller/SQLite integration; every model is synthetic.

The runtime seam supplies harmless Python stages, but never mocks the native
runner, controller handshake, Worker dispatch, checkpoint storage or publication.
Run only through scripts/run_local_asr_tests.py so live data/network are denied.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest

from secretary.application.local_asr_execution_protocol import StageSpec
from secretary.application.local_asr_lifecycle import ExecutionPlan, LocalAsrLifecycle, LocalExecutionError, RuntimeQualification
from secretary.application.worker import Worker
from secretary.domain.local_asr import ImportLocalAsrRun, RuntimeIdentity, model_manifest_digest
from secretary.domain.speakers import ReviewAttribution
from secretary.infrastructure.database import Database
from secretary.infrastructure.local_asr_repository import LocalAsrRepository, initialize_local_asr_schema
from secretary.infrastructure.local_asr_resources import ResourceSnapshot
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.settings import Settings
from test_local_asr_api import seed_meeting, business_snapshot, denied_provider

SCRIPT=Path(__file__).parent/'fixtures/local_asr/synthetic_stage.py'
GPU='GPU-synthetic-executor'


def sample_resources():
    # A fixture reading tests admission branching; it is not host qualification.
    return ResourceSnapshot(32768,32768,{GPU:24576})


class SyntheticRuntime:
    def __init__(self,payload,root,*,mode='success',mode_stage='align_saved_text',deadline_seconds=60,max_attempts=2):
        self.payload=copy.deepcopy(payload)
        self.root=root
        self.mode,self.mode_stage=mode,mode_stage
        self.deadline_seconds,self.max_attempts=deadline_seconds,max_attempts
        self.counter=root/'stage-executions.jsonl'
        self.marker=root/'active-tree.json'
        self.workspaces=[]
        self.qualification_error=None
        self.qualification_calls=0
        self.payload['artifacts']['identity'].update(asr_device='cpu',diarization_device='cuda',physical_gpu_id=GPU)
        self.identity=RuntimeIdentity.model_validate(self.payload['artifacts']['identity'])

    def plan_factory(self,meeting_id,transcript_version,input_sha256,source_text_sha256):
        return ExecutionPlan(input_sha256=input_sha256,source_text_sha256=source_text_sha256,
            profile_digest=self.identity.profile_digest,model_digest=model_manifest_digest(self.identity.models),
            code_digest=self.identity.code_digest,asr_device='cpu',diarization_device='cuda',physical_gpu_id=GPU,
            precision=self.identity.precision,stages=('diarize','align_saved_text','assemble','validate'),
            deadline_at=time.time()+self.deadline_seconds,nemotron_free_vram_mib=1,max_attempts=self.max_attempts)

    def qualification(self,plan):
        self.qualification_calls+=1
        if self.qualification_error:
            raise LocalExecutionError(self.qualification_error)
        return RuntimeQualification(plan.profile_digest,'synthetic-native-test-only','synthetic-test-authorization',True,True,True)

    def build_attempt(self,plan,context,workspace):
        self.workspaces.append(workspace)
        payload=copy.deepcopy(self.payload)
        payload['operation_id']=context.operation_id
        payload['transcript_version']=context.transcript_version
        payload['result']['input_sha256']=context.input_sha256
        payload['artifacts']['input_sha256']=context.input_sha256
        payload['artifacts']['audio_ref']=f'meeting:{context.meeting_id}'
        payload['artifacts']['source_segments']=[item.model_dump(mode='json') for item in context.source_segments]
        request=workspace/'synthetic-request.json'
        request.write_text(json.dumps({'payload':payload,'input_path':str(context.input_path)},ensure_ascii=False),encoding='utf-8')
        stages=[]
        prior=None
        for stage in ('diarize','align_saved_text','assemble'):
            output=workspace/(stage+'.json')
            command=[sys.executable,'-B',str(SCRIPT),'--stage',stage,'--request',str(request),
                     '--output',str(output),'--counter',str(self.counter),
                     '--mode',self.mode if stage==self.mode_stage else 'success',
                     '--marker',str(self.marker),'--attempt-marker',str(self.root/'failed-once')]
            if prior is not None:command+=['--previous',str(prior)]
            stages.append(StageSpec(stage,tuple(command),(output.name,),timeout_seconds=30))
            prior=output
        return tuple(stages)

    def read_result(self,plan,context,workspace):
        return ImportLocalAsrRun.model_validate_json((workspace/'assemble.json').read_text(encoding='utf-8'))

    def counts(self):
        if not self.counter.exists():return Counter()
        return Counter(json.loads(line)['stage'] for line in self.counter.read_text(encoding='utf-8').splitlines())


@pytest.fixture
def setup(tmp_path):
    def make(*,mode='success',mode_stage='align_saved_text',deadline_seconds=60,max_attempts=2,snapshot=sample_resources,queue=True):
        from secretary.application.local_asr_executor import LocalAsrExecutor
        settings=Settings(_env_file=None,data_dir=tmp_path/'data',cloud_enabled=False,polza_api_key='',
                          local_cost_limits_enabled=False,polza_limits_enabled=False)
        db=Database(settings.data_dir/'secretary.sqlite3')
        initialize_local_asr_schema(db)
        life=LocalAsrLifecycle(db);life.initialize_schema()
        seeded=seed_meeting(SimpleNamespace(state=SimpleNamespace(db=db)),settings)
        SpeakerRepository(db).review_attribution(seeded['meeting_id'],ReviewAttribution(
            operation_id='preserved-old-manual',transcript_version=1,expected_revision=0,
            changes=[{'segment_id':seeded['payload']['artifacts']['source_segments'][0]['segment_id'],
                      'participant_id':seeded['participant_id']}]))
        runtime=SyntheticRuntime(seeded['payload'],tmp_path,mode=mode,mode_stage=mode_stage,
                                 deadline_seconds=deadline_seconds,max_attempts=max_attempts)
        executor=LocalAsrExecutor(db,settings,runtime,shared_resource_root=tmp_path/'shared-gpu',
                                 resource_snapshot=snapshot,workspace_root=settings.data_dir/'local-asr-work'/'attempts')
        worker=Worker(db,settings,denied_provider,recovery=False,local_asr_executor=executor)
        plan=executor.plan_factory(seeded['meeting_id'],1,seeded['input_sha256'],
                                   seeded['payload']['artifacts']['selected_text_sha256'])
        job=life.prepare(seeded['meeting_id'],1,plan,'synthetic-executor-request')
        if queue:executor.queue_prepared(job['job_id'])
        return SimpleNamespace(db=db,settings=settings,life=life,runtime=runtime,executor=executor,worker=worker,
                               plan=plan,job_id=job['job_id'],before=business_snapshot(db),**seeded)
    return make


def state(case):return case.life.view(case.job_id)


def test_existing_worker_runs_real_stages_and_publishes_exact_evidence_without_business_mutation(setup):
    case=setup()
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state']=='applied' and result['job_status']=='succeeded',result
    assert case.runtime.qualification_calls==3
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':1,'assemble':1}
    runs=LocalAsrRepository(case.db).list_runs(case.meeting_id)
    assert len(runs)==1 and result['result_run_id']==runs[0]['run_id']
    artifact=LocalAsrRepository(case.db).export_run(case.meeting_id,runs[0]['run_id'])
    assert artifact['result']==case.payload['result']
    assert artifact['artifacts']['selected_text']==case.payload['artifacts']['selected_text']
    assert artifact['artifacts']['runtime_receipts'][-1]['stage']=='synthetic_child_execution'
    native_receipt=artifact['artifacts']['runtime_receipts'][0]
    receipt_file=case.runtime.workspaces[-1]/'native-receipt.json'
    assert native_receipt['stage']=='native_controller' and native_receipt['status']=='succeeded'
    assert native_receipt['receipt_sha256']==hashlib.sha256(receipt_file.read_bytes()).hexdigest()
    assert artifact['source_decisions'][0]['participant_id']==case.participant_id
    assert business_snapshot(case.db)==case.before
    checkpoints=case.db.rows('SELECT stage FROM local_asr_checkpoints WHERE job_id=? ORDER BY stage',(case.job_id,))
    assert {row['stage'] for row in checkpoints}>={'diarize','align_saved_text','assemble'}
    children=[json.loads(line)['pid'] for line in case.runtime.counter.read_text().splitlines()]
    assert all(pid!=os.getpid() and not psutil.pid_exists(pid) for pid in children)
    assert asyncio.run(case.worker.run_once()) is False


def test_admission_refusal_happens_before_any_native_or_stage_process(setup):
    case=setup(snapshot=lambda:ResourceSnapshot(1,1,{GPU:1}))
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state'] in {'blocked','waiting_resources'} and result['job_status']=='waiting_config',result
    assert result['attempt_count']==0
    assert case.runtime.counts()=={}
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


@pytest.mark.parametrize('mode',['invalid-json','wrong-model'])
def test_child_artifact_mismatch_never_publishes_or_changes_paid_business(setup,mode):
    case=setup(mode=mode,mode_stage='assemble',max_attempts=1)
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state']!='applied' and result['job_status']!='succeeded',result
    assert case.runtime.counts()['assemble']==1
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


@pytest.mark.parametrize('stop',['cancel','timeout'])
def test_cancel_and_deadline_terminate_real_stage_grandchild_only(setup,stop):
    case=setup(mode='hang',deadline_seconds=4 if stop=='timeout' else 30,max_attempts=1)
    sentinel=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(60)'],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    async def execute():
        task=asyncio.create_task(case.worker.run_once())
        deadline=time.monotonic()+12
        while not case.runtime.marker.exists() and not task.done() and time.monotonic()<deadline:
            await asyncio.sleep(.03)
        assert case.runtime.marker.exists(),state(case)
        pids=json.loads(case.runtime.marker.read_text())
        if stop=='cancel':
            # Actual supervisor is alive: expiry observed by another recovery
            # process cannot authorize a concurrent inference attempt.
            future=LocalAsrLifecycle(case.db,clock=lambda:time.time()+120)
            with pytest.raises(LocalExecutionError,match='previous_owner_not_exited'):
                future.recover_expired(case.job_id)
            assert case.db.claim() is None
            case.life.request_cancel(case.job_id)
        await asyncio.wait_for(task,20)
        return pids
    try:
        pids=asyncio.run(execute())
        result=state(case)
        assert result['state']==('cancelled' if stop=='cancel' else 'timed_out'),result
        assert all(not psutil.pid_exists(pid) for pid in pids)
        assert sentinel.poll() is None
        assert case.db.rows('SELECT * FROM local_asr_runs')==[]
        assert business_snapshot(case.db)==case.before
    finally:
        sentinel.kill();sentinel.wait(timeout=5)


def test_clean_stage_failure_requeues_bounded_attempt_and_reuses_verified_checkpoint(setup):
    case=setup(mode='fail-once')
    assert asyncio.run(case.worker.run_once()) is True
    first=state(case)
    assert first['state']=='queued' and first['job_status']=='queued',first
    assert first['attempt_count']==1
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':1}
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert asyncio.run(case.worker.run_once()) is True
    final=state(case)
    assert final['state']=='applied' and final['attempt_count']==2,final
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':2,'assemble':1}
    attempts=case.db.rows('SELECT fence,owner_pid,state FROM local_asr_attempts WHERE job_id=? ORDER BY fence',(case.job_id,))
    assert [attempt['fence'] for attempt in attempts]==[1,2]
    assert all(attempt['owner_pid']!=os.getpid() and not psutil.pid_exists(attempt['owner_pid']) for attempt in attempts)
    assert len(LocalAsrRepository(case.db).list_runs(case.meeting_id))==1
    assert business_snapshot(case.db)==case.before


def test_repeated_clean_failure_stops_at_frozen_attempt_bound(setup):
    case=setup(mode='always-fail',max_attempts=2)
    assert asyncio.run(case.worker.run_once()) is True
    assert state(case)['state']=='queued'
    assert asyncio.run(case.worker.run_once()) is True
    final=state(case)
    assert final['state']=='failed' and final['attempt_count']==2,final
    assert asyncio.run(case.worker.run_once()) is False
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':2}
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def test_admission_is_checked_again_between_stages_and_retains_completed_checkpoint(setup):
    case=setup()
    case.executor.resource_snapshot=lambda:(ResourceSnapshot(1,1,{GPU:1})
        if case.runtime.counter.exists() else sample_resources())
    assert asyncio.run(case.worker.run_once()) is True
    waiting=state(case)
    assert waiting['state']=='waiting_resources' and waiting['job_status']=='waiting_config',waiting
    assert waiting['attempt_count']==1
    assert case.runtime.counts()=={'diarize':1}
    checkpoints=case.db.rows('SELECT stage FROM local_asr_checkpoints WHERE job_id=?',(case.job_id,))
    assert [row['stage'] for row in checkpoints]==['diarize']
    attempts=case.db.rows('SELECT owner_pid FROM local_asr_attempts WHERE job_id=?',(case.job_id,))
    assert all(not psutil.pid_exists(row['owner_pid']) for row in attempts)
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def test_resume_rejects_changed_checkpoint_bytes_before_starting_any_new_stage(setup):
    case=setup(mode='fail-once')
    assert asyncio.run(case.worker.run_once()) is True
    assert state(case)['state']=='queued'
    checkpoint=case.runtime.workspaces[0]/'_checkpoints'/'diarize'/'diarize.json'
    value=json.loads(checkpoint.read_text(encoding='utf-8'))
    value['input_sha256']='0'*64
    checkpoint.write_text(json.dumps(value),encoding='utf-8')
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state']=='failed' and result['job_status']=='failed',result
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':1}
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


@pytest.mark.parametrize('reason',['local_asr_runtime_disabled','local_asr_runtime_file_changed'])
def test_runtime_qualification_is_rechecked_after_queue_before_native_start(setup,reason):
    case=setup()
    assert case.runtime.qualification_calls==1
    case.runtime.qualification_error=reason
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state']=='blocked' and result['job_status']=='waiting_config',result
    assert result['reason']==reason and result['attempt_count']==0
    assert case.runtime.qualification_calls==2
    assert case.runtime.workspaces==[] and case.runtime.counts()=={}
    assert case.db.rows('SELECT * FROM local_asr_attempts')==[]
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def test_runtime_disabled_before_clean_retry_preserves_checkpoint_without_new_attempt(setup):
    case=setup(mode='fail-once')
    assert asyncio.run(case.worker.run_once()) is True
    assert state(case)['state']=='queued'
    assert case.runtime.qualification_calls==2
    checkpoints=case.db.rows('SELECT * FROM local_asr_checkpoints WHERE job_id=?',(case.job_id,))
    assert checkpoints
    case.runtime.qualification_error='local_asr_runtime_disabled'
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state']=='blocked' and result['job_status']=='waiting_config',result
    assert result['reason']=='local_asr_runtime_disabled' and result['attempt_count']==1
    assert case.runtime.qualification_calls==3
    assert len(case.runtime.workspaces)==1
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':1}
    assert case.db.rows('SELECT * FROM local_asr_checkpoints WHERE job_id=?',(case.job_id,))==checkpoints
    assert len(case.db.rows('SELECT * FROM local_asr_attempts'))==1
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def test_initial_qualification_denial_persists_blocked_reason_without_queueing(setup):
    case=setup(queue=False)
    case.runtime.qualification_error='local_asr_runtime_disabled'
    with pytest.raises(LocalExecutionError,match='local_asr_runtime_disabled'):
        case.executor.queue_prepared(case.job_id)
    result=state(case)
    assert result['state']=='blocked' and result['job_status']=='waiting_config',result
    assert result['reason']=='local_asr_runtime_disabled' and result['attempt_count']==0
    assert asyncio.run(case.worker.run_once()) is False
    assert case.runtime.qualification_calls==1
    assert case.runtime.workspaces==[] and case.runtime.counts()=={}
    assert business_snapshot(case.db)==case.before


def environment_unit_plan():
    return ExecutionPlan(input_sha256='a'*64,source_text_sha256='b'*64,
        profile_digest='c'*64,model_digest='d'*64,code_digest='e'*64,
        asr_device='cpu',diarization_device='cuda',physical_gpu_id=GPU,precision='float32',
        stages=('diarize','align_saved_text','assemble','validate'),deadline_at=time.time()+60,
        nemotron_free_vram_mib=1)


def environment_unit_specs(plan,*,environment=None,python_paths=()):
    return tuple(StageSpec(stage,(sys.executable,'-B','-c','pass'),(stage+'.json',),
        environment=environment or {},python_paths=python_paths) for stage in plan.stages[:-1])


@pytest.mark.parametrize('environment',[
    {'PYTHONPATH':'injected-modules'},
    {'HF_HUB_OFFLINE':'0'},
    {'HOME':'injected-home'},
    {'PATH':'injected-executables'},
    {'Path':'injected-executables'},
    {'CUDA_VISIBLE_DEVICES':'GPU-other-device'},
])
def test_stage_environment_cannot_override_parent_isolation_or_reserved_device(environment):
    from secretary.application.local_asr_executor import ExecutorArtifactError,LocalAsrExecutor
    plan=environment_unit_plan()
    with pytest.raises(ExecutorArtifactError,match='local_asr_runtime_environment_invalid'):
        LocalAsrExecutor._specs(plan,environment_unit_specs(plan,environment=environment))


@pytest.mark.parametrize('kind',['relative','missing','file'])
def test_stage_python_import_roots_require_existing_absolute_directories(tmp_path,kind):
    from secretary.application.local_asr_executor import ExecutorArtifactError,LocalAsrExecutor
    if kind=='relative':path=Path('relative-import-root')
    elif kind=='missing':path=tmp_path/'missing-import-root'
    else:
        path=tmp_path/'module.py';path.write_text('pass',encoding='utf-8')
    plan=environment_unit_plan()
    with pytest.raises(ExecutorArtifactError,match='local_asr_runtime_python_path_invalid'):
        LocalAsrExecutor._specs(plan,environment_unit_specs(plan,python_paths=(path,)))


def test_base_environment_discards_ambient_imports_executables_and_secrets(tmp_path,monkeypatch):
    from secretary.application.local_asr_executor import BACKEND,_base_environment
    marker='UNTRUSTED_SYNTHETIC_AMBIENT_VALUE'
    for key in ('PATH','PYTHONPATH','HOME','USERPROFILE','TEMP','TMP','HF_HOME','TORCH_HOME','HF_TOKEN','OPENAI_API_KEY'):
        monkeypatch.setenv(key,marker)
    workspace=tmp_path/'attempt';workspace.mkdir()
    environment=_base_environment(workspace)
    assert all(marker not in value for value in environment.values())
    assert 'HF_TOKEN' not in environment and 'OPENAI_API_KEY' not in environment
    assert environment['HF_HUB_OFFLINE']=='1' and environment['TRANSFORMERS_OFFLINE']=='1'
    assert environment['HF_DATASETS_OFFLINE']=='1' and environment['PYTHONDONTWRITEBYTECODE']=='1'
    imports=[Path(value) for value in environment['PYTHONPATH'].split(os.pathsep)]
    assert BACKEND in imports and all(path.is_absolute() for path in imports)
    assert all(Path(value).is_absolute() for value in environment['PATH'].split(os.pathsep))
    for key in ('HOME','USERPROFILE','TEMP','TMP','HF_HOME','TORCH_HOME'):
        assert Path(environment[key]).is_relative_to(workspace)
    assert os.environ['PATH']==marker and os.environ['HF_TOKEN']==marker


@pytest.mark.parametrize('pending_native_marker',[False,True])
def test_only_startup_recovery_repairs_unowned_queue_claim_and_respects_native_marker(setup,pending_native_marker):
    case=setup()
    assert case.db.claim()['id']==case.job_id
    assert state(case)['state']=='queued' and state(case)['job_status']=='running'
    if pending_native_marker:
        marker=case.executor.workspace_root/case.job_id/str(uuid4())/'control'/'native-pending'
        marker.mkdir(parents=True)
    assert case.executor.recover()==[]
    assert state(case)['job_status']=='running'
    recovered=case.executor.recover_startup()
    result=state(case)
    if pending_native_marker:
        assert recovered==[] and result['job_status']=='running'
    else:
        assert len(recovered)==1 and recovered[0]['job_id']==case.job_id
        assert result['job_status']=='queued'
    assert result['state']=='queued' and result['attempt_count']==0
    assert case.runtime.workspaces==[] and case.runtime.counts()=={}
    assert case.db.rows('SELECT * FROM local_asr_attempts')==[]
    assert business_snapshot(case.db)==case.before


def test_runtime_authority_revoked_after_native_success_cannot_publish(setup):
    case=setup()
    read_result=case.runtime.read_result
    def revoke_after_read(plan,context,workspace):
        command=read_result(plan,context,workspace)
        case.runtime.qualification_error='local_asr_runtime_disabled'
        return command
    case.runtime.read_result=revoke_after_read
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state']=='failed' and result['job_status']=='failed',result
    assert result['attempt_count']==1 and case.runtime.qualification_calls==3
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':1,'assemble':1}
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def test_worker_shutdown_after_read_result_persists_cancellation_without_publication(setup):
    case=setup()
    read_result=case.runtime.read_result
    def stop_after_read(plan,context,workspace):
        command=read_result(plan,context,workspace)
        case.worker.stopping.set()
        return command
    case.runtime.read_result=stop_after_read
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert result['state']=='cancelled' and result['job_status']=='cancelled',result
    assert result['cancel_requested']==1
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':1,'assemble':1}
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def test_executor_task_cancellation_drains_blocked_validation_and_never_publishes(setup):
    case=setup()
    read_result=case.runtime.read_result
    reading=threading.Event();release=threading.Event()
    def blocked_read(plan,context,workspace):
        command=read_result(plan,context,workspace)
        reading.set()
        if not release.wait(15):raise AssertionError('Test validation release was not signalled')
        return command
    case.runtime.read_result=blocked_read
    claimed=case.db.claim()
    assert claimed['id']==case.job_id
    async def execute():
        task=asyncio.create_task(case.executor.execute(claimed,asyncio.Event()))
        try:
            deadline=time.monotonic()+15
            while not reading.is_set() and not task.done() and time.monotonic()<deadline:
                await asyncio.sleep(.03)
            assert reading.is_set(),state(case)
            task.cancel()
            await asyncio.sleep(.1)
            assert not task.done(),'Executor must drain its still-running validation thread'
        finally:
            release.set()
        await asyncio.wait_for(task,15)
    asyncio.run(execute())
    result=state(case)
    assert result['state']=='cancelled' and result['job_status']=='cancelled',result
    assert result['cancel_requested']==1
    assert case.runtime.counts()=={'diarize':1,'align_saved_text':1,'assemble':1}
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def test_shutdown_inside_publication_callback_rolls_back_inserted_run(setup):
    case=setup()
    inserted=[]
    def publish_then_stop(conn,context,command,trusted_sha):
        result=LocalAsrRepository(case.db).import_run(context.meeting_id,command,
            trusted_input_sha256=trusted_sha,conn=conn)
        assert conn.execute('SELECT 1 FROM local_asr_runs WHERE id=?',(result['run_id'],)).fetchone()
        inserted.append(result['run_id'])
        case.worker.stopping.set()
        return result['run_id']
    case.executor.publish_result=publish_then_stop
    assert asyncio.run(case.worker.run_once()) is True
    result=state(case)
    assert len(inserted)==1,'The test must reach the publication transaction'
    assert result['state']=='cancelled' and result['job_status']=='cancelled',result
    assert result['cancel_requested']==1 and result['result_run_id'] is None
    assert case.db.rows('SELECT * FROM local_asr_runs')==[]
    assert business_snapshot(case.db)==case.before


def load_controller_for_atomic_write_test():
    path=Path(__file__).parents[1]/'scripts'/'local_asr_controller.py'
    spec=importlib.util.spec_from_file_location('synthetic_controller_atomic_write_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_retries_transient_replace_sharing_without_exposing_partial_json(tmp_path,monkeypatch):
    controller=load_controller_for_atomic_write_test()
    path=tmp_path/'event.json';old={'state':'completed','index':0};new={'state':'awaiting_admission','index':1}
    path.write_text(json.dumps(old),encoding='utf-8')
    replace=Path.replace;calls=[]
    def sharing_then_replace(source,target):
        assert Path(target)==path
        calls.append(source)
        assert json.loads(path.read_text(encoding='utf-8'))==old
        if len(calls)<3:raise PermissionError('Synthetic sharing violation')
        return replace(source,target)
    monkeypatch.setattr(Path,'replace',sharing_then_replace)
    monkeypatch.setattr(controller,'time',SimpleNamespace(monotonic=lambda:0.0,sleep=lambda _:None))
    controller.write_json(path,new,deadline=1.0)
    assert len(calls)==3
    assert json.loads(path.read_text(encoding='utf-8'))==new
    assert not path.with_suffix('.json.tmp').exists()


def test_controller_expired_replace_sharing_keeps_prior_valid_event_and_stops(tmp_path,monkeypatch):
    controller=load_controller_for_atomic_write_test()
    path=tmp_path/'event.json';old={'state':'completed','index':0}
    path.write_text(json.dumps(old),encoding='utf-8')
    calls=[]
    def permanent_sharing(source,target):
        calls.append(source)
        assert Path(target)==path and json.loads(path.read_text(encoding='utf-8'))==old
        raise PermissionError('Synthetic sharing violation')
    def forbidden_sleep(_):raise AssertionError('Expired deadline cannot retry')
    instant=time.monotonic()
    monkeypatch.setattr(Path,'replace',permanent_sharing)
    monkeypatch.setattr(controller,'time',SimpleNamespace(monotonic=lambda:instant,sleep=forbidden_sleep))
    with pytest.raises(PermissionError,match='Synthetic sharing violation'):
        controller.write_json(path,{'state':'finished'},deadline=instant)
    assert len(calls)==1 and json.loads(path.read_text(encoding='utf-8'))==old
