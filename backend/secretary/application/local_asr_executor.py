"""Portable native executor attached to Secretary's existing durable Worker.

The controller owns no application database and cannot publish. Parent-side
admission, checkpoints and fenced publication surround real subprocess stages.
Only server-installed runtime adapters provide commands or qualification.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from uuid import uuid4

from secretary.application.local_asr_execution_protocol import ExecutionContext, StageSpec
from secretary.application.local_asr_lifecycle import LocalAsrLifecycle, LocalExecutionError, ProcessIdentity, RuntimeQualification
from secretary.application.local_asr_media import copy_verified_meeting_input, verified_meeting_input, lock_verified_input_copy
from secretary.domain.local_asr import ImportLocalAsrRun, RuntimeReceipt, SourceSegment, text_fingerprint
from secretary.infrastructure.local_asr_repository import LocalAsrRepository, canonical, digest
from secretary.infrastructure.local_asr_resources import DeviceReservation, ResourceAdmissionError
from secretary.infrastructure.local_asr_process import StageProcessStopped, StageProcessReceipt

JSON_LIMIT=16*1024*1024
BINARY_LIMIT=512*1024*1024
CONTROL_LIMIT=1024*1024
CONTROLLER=Path(__file__).resolve().parents[3]/'scripts/local_asr_controller.py'
BACKEND=Path(__file__).resolve().parents[2]
SCRIPTS=CONTROLLER.parent
FIXED_ENVIRONMENT={'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','PYTHONUTF8':'1',
    'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_DATASETS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1'}


class ExecutorArtifactError(ValueError):
    pass


def _relative(value: str, *, binary=False) -> Path:
    if not isinstance(value,str) or not value or '\\' in value or ':' in value:
        raise ExecutorArtifactError('local_asr_artifact_path_invalid')
    path=Path(value)
    if path.is_absolute() or any(p in {'','..','.'} for p in value.split('/')) or len(value)>240:
        raise ExecutorArtifactError('local_asr_artifact_path_invalid')
    if path.suffix.lower()!=('.wav' if binary else '.json'):
        raise ExecutorArtifactError('local_asr_artifact_format_invalid')
    return path


def _safe_file(root: Path, relative: str, *, binary=False) -> Path:
    path=root/_relative(relative,binary=binary)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ExecutorArtifactError('local_asr_artifact_path_invalid')
    for part in (path,*path.parents):
        if part==root.parent:break
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ExecutorArtifactError('local_asr_artifact_link_forbidden')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise ExecutorArtifactError('local_asr_artifact_file_invalid')
    if info.st_size>(BINARY_LIMIT if binary else JSON_LIMIT):
        raise ExecutorArtifactError('local_asr_artifact_size_invalid')
    return path


def _read_json(path: Path,limit=JSON_LIMIT):
    if path.stat().st_size>limit:raise ExecutorArtifactError('local_asr_json_size_invalid')
    data=path.read_bytes()
    if len(data)>limit:raise ExecutorArtifactError('local_asr_json_size_invalid')
    try:
        return json.loads(data,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    except (UnicodeError,ValueError):
        raise ExecutorArtifactError('local_asr_json_invalid') from None


def _write_json(path: Path,value):
    data=canonical(value).encode('utf-8')
    if len(data)>CONTROL_LIMIT:raise ExecutorArtifactError('local_asr_control_size_invalid')
    temporary=path.with_name(path.name+'.tmp')
    with temporary.open('xb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)


def _copy_checked(source: Path,destination: Path,*,limit:int,expected=None):
    before=source.stat();signature=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_nlink)
    if before.st_size>limit:raise ExecutorArtifactError('local_asr_artifact_size_invalid')
    destination.parent.mkdir(parents=True,exist_ok=True)
    total=0;hasher=hashlib.sha256()
    with source.open('rb') as src,destination.open('xb') as dst:
        if signature(os.fstat(src.fileno()))!=signature(before):raise ExecutorArtifactError('local_asr_artifact_changed')
        while block:=src.read(1024*1024):
            total+=len(block)
            if total>limit:raise ExecutorArtifactError('local_asr_artifact_size_invalid')
            hasher.update(block);dst.write(block)
        dst.flush();os.fsync(dst.fileno())
        if signature(os.fstat(src.fileno()))!=signature(before):raise ExecutorArtifactError('local_asr_artifact_changed')
    if signature(source.stat())!=signature(before):raise ExecutorArtifactError('local_asr_artifact_changed')
    actual=hasher.hexdigest()
    if expected is not None and actual!=expected:raise ExecutorArtifactError('local_asr_checkpoint_file_corrupt')
    return actual,total


def _plain_directory(path: Path):
    if not path.is_absolute() or not path.is_dir():
        raise ExecutorArtifactError('local_asr_runtime_python_path_invalid')
    for part in (path,*path.parents):
        info=part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ExecutorArtifactError('local_asr_runtime_python_path_invalid')


def _base_environment(workspace: Path):
    """Build a private cache/home without inherited code paths or credentials."""
    private=workspace/'_runtime'
    directories={name:private/name for name in ('home','temp','appdata','localappdata','cache')}
    for directory in directories.values():directory.mkdir(parents=True,exist_ok=True)
    windows=Path(os.environ.get('SYSTEMROOT',r'C:\Windows'))
    result={'SYSTEMROOT':str(windows),'WINDIR':str(windows),
            'PATH':os.pathsep.join(map(str,(Path(sys.executable).parent,Path(sys.base_prefix),windows/'System32',windows))),
            'HOME':str(directories['home']),'USERPROFILE':str(directories['home']),
            'TEMP':str(directories['temp']),'TMP':str(directories['temp']),
            'APPDATA':str(directories['appdata']),'LOCALAPPDATA':str(directories['localappdata']),
            'CUDA_VISIBLE_DEVICES':'-1'}
    for name in ('HF_HOME','HUGGINGFACE_HUB_CACHE','TRANSFORMERS_CACHE','TORCH_HOME','XDG_CACHE_HOME','NUMBA_CACHE_DIR'):
        result[name]=str(directories['cache']/name.lower())
    paths=[SCRIPTS,BACKEND]
    # The offline launcher deliberately installs a tiny exact child hook. Its
    # allowed location and contents are checked independently of PYTHONPATH.
    retention=os.environ.get('SECRETARY_OFFLINE_RETENTION_RUN')
    if retention:
        run=Path(retention)
        expected_root=SCRIPTS.parent/'.runtime'/'team-rollout'
        if (not run.is_absolute() or run.parent.resolve()!=expected_root.resolve()
                or not re.fullmatch(r'offline-[0-9a-f]{32}',run.name)):
            raise ExecutorArtifactError('local_asr_offline_guard_invalid')
        helper=run/'child-guard';_plain_directory(helper)
        hook=helper/'sitecustomize.py'
        info=hook.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or info.st_nlink!=1
                or info.st_size>256 or hook.read_text(encoding='utf-8')!='from run_offline_tests import install_guard\ninstall_guard()\n'):
            raise ExecutorArtifactError('local_asr_offline_guard_invalid')
        paths.insert(0,helper);result['SECRETARY_OFFLINE_RETENTION_RUN']=str(run)
    result['PYTHONPATH']=os.pathsep.join(map(str,paths))
    result.update(FIXED_ENVIRONMENT)
    return result


class LocalAsrExecutor:
    def __init__(self,db,settings,runtime,*,shared_resource_root,resource_snapshot,
                 qualification=None,workspace_root=None,publish_result=None):
        self.db,self.settings,self.runtime=db,settings,runtime
        self.shared_resource_root=Path(shared_resource_root) if shared_resource_root is not None else None
        self.resource_snapshot=resource_snapshot
        self.qualification=qualification
        self.workspace_root=Path(workspace_root or settings.data_dir/'local-asr-work')
        if not self.workspace_root.absolute().is_relative_to((settings.data_dir/'local-asr-work').absolute()):
            raise ValueError('local_asr_workspace_outside_data')
        self.lifecycle=LocalAsrLifecycle(db)
        self.repository=LocalAsrRepository(db)
        self.publish_result=publish_result

    def plan_factory(self,meeting_id,transcript_version,input_sha256,source_text_sha256):
        return self.runtime.plan_factory(meeting_id,transcript_version,input_sha256,source_text_sha256)

    def queue_prepared(self,job_id):
        plan=self.lifecycle.plan(job_id)
        try:
            qualification=self._current_qualification(plan)
            return self.lifecycle.authorize_queue(job_id,qualification)
        except Exception as exc:
            code=self._failure_code(exc,'local_asr_qualification_required')
            self.lifecycle.reject_activation(job_id,code)
            raise

    def _current_qualification(self,plan):
        # Re-read executable/model/profile authority on every attempt. An
        # optional host attestation may narrow this proof, never replace it.
        qualification=self.runtime.qualification(plan)
        additional=(self.qualification(plan) if callable(self.qualification) else self.qualification)
        if additional is not None:self._validate_qualification(plan,additional)
        self._validate_qualification(plan,qualification)
        return qualification

    @staticmethod
    def _validate_qualification(plan,qualification):
        if not isinstance(qualification,RuntimeQualification) or not (
            qualification.evidence_id and qualification.owner_authorization_id and qualification.source_verified
            and qualification.process_containment_verified and qualification.shared_admission_verified):
            raise LocalExecutionError('local_asr_qualification_required')
        if qualification.profile_digest!=plan.profile_digest:
            raise LocalExecutionError('local_asr_qualification_profile_mismatch')

    @staticmethod
    def _failure_code(failure,default='local_asr_preflight_failed'):
        code=getattr(failure,'code',str(failure))
        return code if isinstance(code,str) and re.fullmatch(r'[a-z0-9_]+',code) else default

    async def execute(self,job,stopping):
        own_stop=threading.Event()
        task=asyncio.create_task(asyncio.to_thread(self._execute_sync,job,lambda:own_stop.is_set() or stopping.is_set()))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            own_stop.set()
            # Thread ownership outlives an HTTP/task cancellation. Drain native
            # cleanup before releasing a device or returning to the worker.
            return await asyncio.shield(task)

    def _raise_if_stopped(self,job_id,stopped,*,inside_transaction=False):
        if stopped():
            # An exception rolls back the publication transaction. Persist the
            # cancellation outside that transaction in the cleanup path.
            if not inside_transaction:self.lifecycle.request_cancel(job_id)
            raise LocalExecutionError('local_asr_cancelled')

    @contextmanager
    def _validation_guard(self,claim,stopped):
        """Keep a live parent lease while bounded result/hash checks run."""
        finished=threading.Event();failures=[]
        def ensure_active():
            self._raise_if_stopped(claim.job_id,stopped)
            if failures:raise failures[0]
        def maintain():
            while not finished.wait(1):
                try:
                    ensure_active()
                    self.lifecycle.heartbeat(claim,'validate',0.0)
                except Exception as exc:
                    failures.append(exc)
                    return
        maintainer=threading.Thread(target=maintain,name='local-asr-validation-lease',daemon=True)
        maintainer.start()
        try:yield ensure_active
        finally:
            finished.set()
            maintainer.join()

    def _context(self,job,plan,workspace):
        with self.db.connection() as conn:
            sources=tuple(SourceSegment(**s) for s in self.repository.source_snapshot(conn,job['meeting_id'],job['version']))
        actual_text=text_fingerprint('\n'.join(s.text for s in sources)) if sources else None
        if actual_text!=plan.source_text_sha256:raise ExecutorArtifactError('local_asr_saved_text_fingerprint_mismatch')
        input_path=copy_verified_meeting_input(self.db,self.settings.data_dir,job['meeting_id'],workspace/'input.wav',
                       max_bytes=self.settings.max_upload_bytes,expected_sha256=plan.input_sha256)
        meeting=self.db.meeting(job['meeting_id'],internal=True)
        duration=meeting.get('duration_ms')
        if type(duration) is not int or duration<=0:
            if plan.source_text_sha256 is None and not sources:duration=0
            else:raise ExecutorArtifactError('local_asr_duration_unavailable')
        return ExecutionContext(job['meeting_id'],job['version'],input_path,plan.input_sha256,sources,
                                'local-asr-'+job['id'],actual_text,duration)

    @staticmethod
    def _specs(plan,specs):
        if not plan.stages or plan.stages[-1]!='validate' or tuple(s.stage for s in specs)!=plan.stages[:-1]:
            raise ExecutorArtifactError('local_asr_runtime_stage_mismatch')
        seen=set()
        for spec in specs:
            if not isinstance(spec,StageSpec) or not spec.command or any(not isinstance(v,str) or not v for v in spec.command):
                raise ExecutorArtifactError('local_asr_runtime_command_invalid')
            if not Path(spec.command[0]).is_absolute():raise ExecutorArtifactError('local_asr_runtime_executable_not_absolute')
            if type(spec.timeout_seconds) is not int or not 1<=spec.timeout_seconds<=1000:
                raise ExecutorArtifactError('local_asr_stage_deadline_invalid')
            if not spec.outputs or len(spec.outputs)+len(spec.binary_outputs)>128:
                raise ExecutorArtifactError('local_asr_stage_outputs_invalid')
            for name in spec.outputs+spec.binary_outputs:
                _relative(name,binary=name in spec.binary_outputs)
                if name in seen or name.split('/')[0] in {'control','_checkpoints','_runtime'} or name in {'input.wav','controller-request.json','native-receipt.json'}:
                    raise ExecutorArtifactError('local_asr_output_collision')
                seen.add(name)
            for key,value in spec.environment.items():
                if not isinstance(key,str) or not isinstance(value,str):
                    raise ExecutorArtifactError('local_asr_runtime_environment_invalid')
                if key in FIXED_ENVIRONMENT and value==FIXED_ENVIRONMENT[key]:continue
                if key in {'OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS'} and value.isdecimal() and 1<=int(value)<=plan.cpu_threads:continue
                if key in {'DEV_ASR_TRACEBACK','DEV_CTC_TRACE_PROGRESS','NEUROHR_ASR_EXPERIMENTAL_NEMOTRON_TURN_END_PAD_MS'} and value=='0':continue
                if key=='NEUROHR_ASR_EXPERIMENTAL_GROUP_GAP_SECONDS' and value=='1.5':continue
                device=plan.diarization_device if spec.stage=='diarize' else (plan.asr_device if spec.stage in {'asr_rnnt','align_rnnt','align_saved_text'} else 'cpu')
                if key=='CUDA_VISIBLE_DEVICES' and value==(plan.physical_gpu_id if device=='cuda' else '-1'):continue
                raise ExecutorArtifactError('local_asr_runtime_environment_invalid')
            if not isinstance(spec.python_paths,tuple) or len(spec.python_paths)>8:
                raise ExecutorArtifactError('local_asr_runtime_python_path_invalid')
            for directory in spec.python_paths:_plain_directory(Path(directory))

    def _checkpoint_stage(self,claim,spec,workspace):
        files=[]
        for name in spec.outputs+spec.binary_outputs:
            binary=name in spec.binary_outputs
            source=_safe_file(workspace,name,binary=binary)
            if not binary:_read_json(source)
            destination=workspace/'_checkpoints'/spec.stage/name
            sha,size=_copy_checked(source,destination,limit=BINARY_LIMIT if binary else JSON_LIMIT)
            files.append(dict(relative=name,binary=binary,sha256=sha,size=size,
                              storage_key=destination.relative_to(self.workspace_root/claim.job_id).as_posix()))
        payload={'schema_version':'secretary.local-asr-stage-files.v1','files':files}
        plan=self.lifecycle.plan(claim.job_id)
        self.lifecycle.save_checkpoint(claim,spec.stage,plan.checkpoint_identity(spec.stage),payload)

    def _restore_stage(self,claim,plan,spec,workspace):
        checkpoint=self.lifecycle.checkpoint(claim,spec.stage,plan.checkpoint_identity(spec.stage))
        if checkpoint is None:return False
        if checkpoint.get('schema_version')!='secretary.local-asr-stage-files.v1':raise ExecutorArtifactError('local_asr_checkpoint_schema_invalid')
        files=checkpoint.get('files')
        if not isinstance(files,list) or [f.get('relative') for f in files]!=list(spec.outputs+spec.binary_outputs):
            raise ExecutorArtifactError('local_asr_checkpoint_outputs_invalid')
        job_root=self.workspace_root/claim.job_id
        for item in files:
            binary=item['relative'] in spec.binary_outputs
            if item.get('binary') is not binary:raise ExecutorArtifactError('local_asr_checkpoint_outputs_invalid')
            source=_safe_file(job_root,item['storage_key'],binary=binary)
            if not binary:_read_json(source)
            _,size=_copy_checked(source,workspace/_relative(item['relative'],binary=binary),
                                  limit=BINARY_LIMIT if binary else JSON_LIMIT,expected=item['sha256'])
            if size!=item['size']:raise ExecutorArtifactError('local_asr_checkpoint_file_corrupt')
        return True

    def _execute_sync(self,job,stopped):
        job_id=job['id'];claim=None;receipt=None;workspace=None;failure=None;retryable=False
        pending=None
        try:
            plan=self.lifecycle.plan(job_id)
            if plan.provider!='local':raise ExecutorArtifactError('local_asr_no_cloud_fallback')
            if stopped():self.lifecycle.request_cancel(job_id);return self.lifecycle.view(job_id)
            if self._containment_pending(job_id,plan):raise LocalExecutionError('local_asr_cleanup_unconfirmed')
            self._current_qualification(plan)
            profile=plan.resource_profile(time.time())
            with DeviceReservation(self.shared_resource_root,profile) as reservation, ExitStack() as owned_files:
                # Admission is before controller creation and consumes no attempt.
                reservation.check_stage(self.resource_snapshot)
                workspace=self.workspace_root/job_id/str(uuid4())
                workspace.mkdir(parents=True,exist_ok=False)
                (workspace/'control').mkdir()
                context=self._context(job,plan,workspace)
                owned_files.enter_context(lock_verified_input_copy(context.input_path,
                    expected_sha256=plan.input_sha256,max_bytes=self.settings.max_upload_bytes))
                specs=tuple(self.runtime.build_attempt(plan,context,workspace));self._specs(plan,specs)
                token=uuid4().hex;base_env=_base_environment(workspace)
                def stage_environment(spec):
                    environment=base_env|dict(spec.environment)
                    for key in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS'):
                        environment.setdefault(key,str(plan.cpu_threads))
                    environment['PYTHONPATH']=base_env['PYTHONPATH']+''.join(os.pathsep+str(p) for p in spec.python_paths)
                    return environment
                request={'attempt_token':token,'deadline_seconds':max(.001,plan.deadline_at-time.time()),
                    'job_commit_limit_bytes':plan.job_commit_limit_mib*1024**2,
                    'working_set_limit_bytes':plan.owned_rss_limit_mib*1024**2,
                    'owned_rss_limit_bytes':plan.owned_rss_limit_mib*1024**2,
                    'stages':[{'stage':s.stage,'command':list(s.command),'environment':stage_environment(s),
                               'timeout_seconds':s.timeout_seconds,'skip':False} for s in specs]}
                request_path=workspace/'controller-request.json';_write_json(request_path,request)
                pending=workspace/'control'/'native-pending'
                permits=set();completed=set();current=[plan.stages[0],0.0];last_heartbeat=[0.0]
                stage_receipts=[]
                callback_error=[]

                def on_started(process):
                    nonlocal claim
                    import psutil
                    identity=ProcessIdentity(process.pid,psutil.Process(process.pid).create_time())
                    claim=self.lifecycle.begin_claim(job_id,'secretary-native-controller',identity)
                    pending.mkdir()
                    for index,spec in enumerate(specs):
                        if self._restore_stage(claim,plan,spec,workspace):
                            request['stages'][index]['skip']=True;completed.add(index)
                    _write_json(request_path,request)

                def on_poll(process):
                    try:
                        if claim is None:raise ExecutorArtifactError('local_asr_claim_missing')
                        if stopped():self.lifecycle.request_cancel(job_id)
                        event_path=workspace/'control'/'event.json'
                        if event_path.exists():
                            event=_read_json(event_path,CONTROL_LIMIT)
                            if event.get('attempt_token')!=token:raise ExecutorArtifactError('local_asr_controller_token_mismatch')
                            state=event.get('state')
                            if state=='finished':
                                if len(completed)!=len(specs):raise ExecutorArtifactError('local_asr_stage_completion_missing')
                            elif state in {'awaiting_admission','completed','failed'}:
                                index=event.get('index')
                                if type(index) is not int or not 0<=index<len(specs):raise ExecutorArtifactError('local_asr_controller_stage_invalid')
                                spec=specs[index]
                                if event.get('stage')!=spec.stage or any(i not in completed for i in range(index)):
                                    raise ExecutorArtifactError('local_asr_controller_stage_invalid')
                                if state=='awaiting_admission' and index not in permits:
                                    reservation.check_stage(self.resource_snapshot)
                                    current[:]=[spec.stage,0.0]
                                    self.lifecycle.heartbeat(claim,*current);last_heartbeat[0]=time.monotonic()
                                    _write_json(workspace/'control'/f'permit-{index}.json',dict(attempt_token=token,index=index,stage=spec.stage))
                                    permits.add(index)
                                elif state=='completed' and index not in completed:
                                    if index not in permits or event.get('cleanup_confirmed') is not True:
                                        raise ExecutorArtifactError('local_asr_stage_cleanup_unverified')
                                    self._checkpoint_stage(claim,spec,workspace)
                                    peak=event.get('peak_owned_rss_bytes');elapsed=event.get('elapsed_ms')
                                    if type(peak) is not int or peak<0 or type(elapsed) is not int or elapsed<0:
                                        raise ExecutorArtifactError('local_asr_native_measurement_invalid')
                                    stage_receipts.append(dict(stage=spec.stage,peak_owned_rss_bytes=peak,elapsed_ms=elapsed,cleanup_confirmed=True))
                                    completed.add(index);current[:]=[spec.stage,1.0]
                                    self.lifecycle.heartbeat(claim,*current);last_heartbeat[0]=time.monotonic()
                                    _write_json(workspace/'control'/f'checkpoint-{index}.json',dict(attempt_token=token,index=index,stage=spec.stage))
                            else:raise ExecutorArtifactError('local_asr_controller_event_invalid')
                        if time.monotonic()-last_heartbeat[0]>=1:
                            self.lifecycle.heartbeat(claim,*current);last_heartbeat[0]=time.monotonic()
                    except Exception as exc:
                        callback_error.append(exc);raise

                def cancelled():
                    return stopped() or bool(self.db.job(job_id,internal=True)['cancel_requested'])

                native_started=time.monotonic()
                def native_receipt(result):
                    measured={'schema_version':'secretary.local-asr-native-receipt.v1',
                        'attempt_id':claim.attempt_id if claim else None,'plan_digest':plan.digest,
                        'cleanup_confirmed':result.cleanup_confirmed,
                        'returncode':getattr(result,'returncode',getattr(result,'exit_code',None)),
                        'elapsed_ms':max(0,int((time.monotonic()-native_started)*1000)),
                        'peak_owned_rss_bytes':getattr(result,'peak_owned_rss_bytes',None),
                        'job_commit_limit_bytes':profile.job_commit_limit_mib*1024**2,
                        'working_set_limit_bytes':profile.owned_rss_limit_mib*1024**2,
                        'completed_stages':stage_receipts}
                    _write_json(workspace/'native-receipt.json',measured)
                    return measured
                try:
                    receipt=reservation.run_native_stage([sys.executable,'-B',str(CONTROLLER),'--request',str(request_path)],
                        snapshot_supplier=self.resource_snapshot,environment=base_env,cwd=str(workspace),
                        timeout_seconds=max(.001,plan.deadline_at-time.time()),stdout_limit=65536,stderr_limit=65536,
                        job_commit_limit_bytes=profile.job_commit_limit_mib*1024**2,
                        working_set_limit_bytes=profile.owned_rss_limit_mib*1024**2,
                        owned_rss_limit_bytes=profile.owned_rss_limit_mib*1024**2,
                        on_started=on_started,on_poll=on_poll,cancel_requested=cancelled)
                except StageProcessStopped as exc:
                    receipt=exc
                    if exc.cleanup_confirmed and pending.exists():pending.rmdir()
                    native_receipt(exc)
                    if callback_error:raise callback_error[-1] from exc
                    raise
                if pending.exists():pending.rmdir()
                measured=native_receipt(receipt)
                if receipt.returncode:
                    retryable=receipt.returncode not in {124,130,78}
                    if receipt.returncode==124:raise LocalExecutionError('local_asr_timeout')
                    if receipt.returncode==130:raise LocalExecutionError('local_asr_cancelled')
                    raise LocalExecutionError('local_asr_stage_nonzero')
                if len(completed)!=len(specs):raise ExecutorArtifactError('local_asr_stage_completion_missing')
                self.lifecycle.heartbeat(claim,'validate',0.0)
                with self._validation_guard(claim,stopped) as ensure_active:
                    ensure_active()
                    command=self.runtime.read_result(plan,context,workspace)
                    ensure_active()
                    command=ImportLocalAsrRun.model_validate(command.model_dump(mode='json'))
                    if any(r.stage=='native_controller' for r in command.artifacts.runtime_receipts):
                        raise ExecutorArtifactError('local_asr_reserved_receipt_stage')
                    receipt_summary=RuntimeReceipt(stage='native_controller',receipt_sha256=digest(measured),
                        status='succeeded',elapsed_ms=measured['elapsed_ms'])
                    command.artifacts.runtime_receipts.insert(0,receipt_summary)
                    command=ImportLocalAsrRun.model_validate(command.model_dump(mode='json'))
                    if command.operation_id!=context.operation_id or command.transcript_version!=context.transcript_version:
                        raise ExecutorArtifactError('local_asr_result_scope_mismatch')
                    if not context.source_segments:
                        from secretary.infrastructure.local_asr_publication import prepare_initial_audio
                        context,command=prepare_initial_audio(self.settings,context,command)
                    ensure_active()
                    self._current_qualification(plan)
                    ensure_active()
                    trusted=verified_meeting_input(self.db,self.settings.data_dir,context.meeting_id,max_bytes=self.settings.max_upload_bytes)
                    if trusted!=plan.input_sha256:raise ExecutorArtifactError('local_asr_input_changed')
                    ensure_active()
                manifest={'schema_version':'secretary.local-asr-run.v1','meeting_id':context.meeting_id,
                          'transcript_version':command.transcript_version,'result':command.result.model_dump(mode='json'),
                          'artifacts':command.artifacts.model_dump(mode='json')}
                self._raise_if_stopped(job_id,stopped)
                self.lifecycle.store_artifact(claim,digest(manifest))
                def publish(conn):
                    self._raise_if_stopped(job_id,stopped,inside_transaction=True)
                    if self.publish_result is not None:run_id=self.publish_result(conn,context,command,trusted)
                    else:run_id=self.repository.import_run(context.meeting_id,command,trusted_input_sha256=trusted,conn=conn)['run_id']
                    self._raise_if_stopped(job_id,stopped,inside_transaction=True)
                    return run_id
                self._raise_if_stopped(job_id,stopped)
                self.lifecycle.apply_atomically(claim,publish)
                return self.lifecycle.view(job_id)
        except Exception as exc:
            failure=exc
        # This branch is deliberately isolated from Worker's paid-cloud retry and
        # meeting-status logic. An analysis failure cannot cancel a ready meeting.
        if claim is None:
            code=self._failure_code(failure)
            try:
                return self.lifecycle.block_before_start(job_id,code,waiting_resources=isinstance(failure,ResourceAdmissionError))
            except LocalExecutionError:
                return self.lifecycle.view(job_id)
        cleanup=isinstance(receipt,(StageProcessReceipt,StageProcessStopped)) and receipt.cleanup_confirmed is True
        if not cleanup:return self.lifecycle.view(job_id)
        cancelled=stopped() or bool(self.db.job(job_id,internal=True)['cancel_requested'])
        timeout=time.time()>=plan.deadline_at or getattr(failure,'code','') in {'local_asr_timeout','local_asr_deadline_expired'}
        try:
            if cancelled:self.lifecycle.request_cancel(job_id)
            if isinstance(failure,ResourceAdmissionError) and not cancelled and not timeout:
                self.lifecycle.wait_after_native_cleanup(claim,receipt,failure.code)
            elif retryable and not cancelled and not timeout:
                self.lifecycle.retry_after_native_cleanup(claim,receipt)
            else:
                self.lifecycle.finish_after_cleanup(claim,cleanup_complete=True,
                    reason='cancelled' if cancelled else ('timed_out' if timeout else 'failed'))
        except LocalExecutionError:
            # Fencing/terminal state may have changed while cleanup drained.
            # The old owner must never overwrite the new authoritative state.
            pass
        return self.lifecycle.view(job_id)

    def recover(self):
        """Recover only expired owners with no unresolved containment marker."""
        recovered=[]
        rows=self.db.rows("SELECT job_id,plan FROM local_asr_executions WHERE state IN ('running','artifact_stored','cancel_requested') AND lease_until<=?",(time.time(),))
        for row in rows:
            plan=self.lifecycle.plan(row['job_id'])
            if self._containment_pending(row['job_id'],plan):continue
            try:recovered.append(self.lifecycle.recover_expired(row['job_id']))
            except LocalExecutionError:continue
        return recovered

    def _containment_pending(self,job_id,plan):
        job_root=self.workspace_root/job_id
        if job_root.exists() and any(job_root.glob('*/control/native-pending')):return True
        if plan.physical_gpu_id and self.shared_resource_root is not None:
            key=hashlib.sha256(plan.physical_gpu_id.casefold().encode('ascii')).hexdigest()
            if os.path.lexists(self.shared_resource_root/'physical-gpu'/key/'native-state-unverified'):return True
        return False

    def recover_startup(self):
        """Only call under the application's exclusive data lock before Worker starts.

        Periodic recovery must never release a freshly claimed preflight. Startup
        can repair the crash gap between queue claim and native owner registration.
        """
        recovered=[]
        rows=self.db.rows("""SELECT e.job_id FROM local_asr_executions e JOIN jobs j ON j.id=e.job_id
            WHERE e.state='queued' AND j.status='running' AND e.owner_pid IS NULL
            AND e.attempt_id IS NULL AND e.lease_until IS NULL AND e.attempt_count=0""")
        for row in rows:
            plan=self.lifecycle.plan(row['job_id'])
            if self._containment_pending(row['job_id'],plan):continue
            try:recovered.append(self.lifecycle.recover_unowned_queue(row['job_id']))
            except LocalExecutionError:continue
        return recovered+self.recover()
