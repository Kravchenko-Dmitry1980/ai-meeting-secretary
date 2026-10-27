"""Model-free controller protocol for one native-contained local ASR attempt.

The parent is the sole resource and job owner. Each stage must receive its
matching one-use admission file before its command can start. All subprocesses
remain inside the parent's native Job and their own nested native stage Job.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from secretary.infrastructure.local_asr_process import run_owned_stage, StageProcessStopped

LIMIT = 1024 * 1024


def read_json(path: Path):
    raw=path.read_bytes()
    if len(raw)>LIMIT:raise ValueError('controller_message_too_large')
    return json.loads(raw)


def write_json(path: Path,value,*,deadline=None):
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(raw)>LIMIT:raise ValueError('controller_message_too_large')
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('xb') as stream:
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    # Windows readers may briefly deny FILE_SHARE_DELETE. Preserve atomic
    # replacement and retry only this sharing failure within a small bound.
    end=min(time.monotonic()+1.0,deadline) if deadline is not None else time.monotonic()+1.0
    while True:
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if time.monotonic()>=end:raise
            time.sleep(.01)


def retain_diagnostic(request, control, index, stage, receipt, *, deadline):
    """Private bootstrap evidence only; production requests leave this absent."""
    if request.get('retain_stage_diagnostics') is not True:
        return
    value=dict(stage=stage,returncode=getattr(receipt,'returncode',getattr(receipt,'exit_code',None)),
               cleanup_confirmed=receipt.cleanup_confirmed,
               stdout=getattr(receipt,'stdout',b'')[:65536].decode('utf-8',errors='replace'),
               stderr=getattr(receipt,'stderr',b'')[:65536].decode('utf-8',errors='replace'))
    write_json(control/f'stage-{index}-diagnostic.json',value,deadline=deadline)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--request',required=True)
    args=parser.parse_args()
    request_path=Path(args.request).resolve()
    workspace=request_path.parent
    request=read_json(request_path)
    token=request['attempt_token']
    stages=request['stages']
    end=time.monotonic()+request['deadline_seconds']
    control=workspace/'control'
    for index,spec in enumerate(stages):
        if spec['skip']:
            continue
        write_json(control/'event.json',dict(attempt_token=token,index=index,stage=spec['stage'],state='awaiting_admission'),deadline=end)
        permit_path=control/f'permit-{index}.json'
        while not permit_path.exists():
            if time.monotonic()>=end:return 124
            time.sleep(.025)
        permit=read_json(permit_path)
        if permit != dict(attempt_token=token,index=index,stage=spec['stage']):
            raise ValueError('controller_permission_mismatch')
        remaining=min(float(spec['timeout_seconds']),end-time.monotonic())
        if remaining<=0:return 124
        stage_started=time.monotonic()
        try:
            receipt=run_owned_stage(spec['command'],environment=spec['environment'],cwd=str(workspace),
                timeout_seconds=remaining,stdout_limit=65536,stderr_limit=65536,
                job_commit_limit_bytes=request['job_commit_limit_bytes'],
                working_set_limit_bytes=request['working_set_limit_bytes'],
                owned_rss_limit_bytes=request['owned_rss_limit_bytes'])
        except StageProcessStopped as exc:
            retain_diagnostic(request,control,index,spec['stage'],exc,deadline=end)
            write_json(control/'event.json',dict(attempt_token=token,index=index,stage=spec['stage'],state='failed',
                                                code=exc.code,cleanup_confirmed=exc.cleanup_confirmed),deadline=end)
            return 124 if exc.exit_code==124 else 1
        retain_diagnostic(request,control,index,spec['stage'],receipt,deadline=end)
        if receipt.returncode:
            write_json(control/'event.json',dict(attempt_token=token,index=index,stage=spec['stage'],state='failed',
                                                code='local_asr_stage_nonzero',cleanup_confirmed=receipt.cleanup_confirmed),deadline=end)
            return 1
        write_json(control/'event.json',dict(attempt_token=token,index=index,stage=spec['stage'],state='completed',
                                            cleanup_confirmed=receipt.cleanup_confirmed,
                                            elapsed_ms=max(0,int((time.monotonic()-stage_started)*1000)),
                                            peak_owned_rss_bytes=receipt.peak_owned_rss_bytes),deadline=end)
        ack=control/f'checkpoint-{index}.json'
        while not ack.exists():
            if time.monotonic()>=end:return 124
            time.sleep(.025)
        if read_json(ack)!=dict(attempt_token=token,index=index,stage=spec['stage']):
            raise ValueError('controller_checkpoint_mismatch')
    write_json(control/'event.json',dict(attempt_token=token,state='finished'),deadline=end)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError,KeyError,OSError,TypeError):
        # No commands, paths, audio or child stderr may enter public errors.
        raise SystemExit(78)
