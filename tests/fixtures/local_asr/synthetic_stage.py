"""Actual model-free stage executable for portable-executor integration tests.

This file is never a production runtime or an inference adapter. It verifies
its synthetic input and writes bounded JSON evidence from a real child process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',required=True)
    parser.add_argument('--request',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--counter',type=Path,required=True)
    parser.add_argument('--previous',type=Path)
    parser.add_argument('--mode',choices=['success','fail-once','always-fail','hang','invalid-json','wrong-model'],default='success')
    parser.add_argument('--marker',type=Path)
    parser.add_argument('--attempt-marker',type=Path)
    options=parser.parse_args()
    request=json.loads(options.request.read_text(encoding='utf-8'))
    audio=Path(request['input_path'])
    observed=hashlib.sha256(audio.read_bytes()).hexdigest()
    if observed != request['payload']['result']['input_sha256']:
        raise RuntimeError('synthetic source hash mismatch')
    if options.previous:
        previous=json.loads(options.previous.read_text(encoding='utf-8'))
        if previous['input_sha256'] != observed:
            raise RuntimeError('synthetic previous stage mismatch')
    options.counter.parent.mkdir(parents=True,exist_ok=True)
    with options.counter.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'stage':options.stage,'pid':os.getpid()})+'\n')
    if options.mode in ('fail-once','always-fail'):
        marker=options.attempt_marker or options.counter.with_suffix('.failed')
        if options.mode=='always-fail' or not marker.exists():
            marker.write_text('synthetic failure reached',encoding='utf-8')
            return 7
    if options.mode=='hang':
        child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(60)'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if options.marker:
            options.marker.write_text(json.dumps([os.getpid(),child.pid]),encoding='utf-8')
        time.sleep(60)
        return 9
    if options.mode=='invalid-json':
        options.output.write_text('{not-valid-json',encoding='utf-8')
        return 0
    if options.stage=='assemble':
        result=request['payload']
        if options.mode=='wrong-model':
            result['artifacts']['identity']['models'][0]['weights_sha256']='9'*64
        result['artifacts']['runtime_receipts'].append({
            'stage':'synthetic_child_execution','receipt_sha256':hashlib.sha256(
                json.dumps({'stage':options.stage,'pid':os.getpid(),'input_sha256':observed},sort_keys=True).encode()).hexdigest(),
            'status':'succeeded','elapsed_ms':None})
    else:
        result={'schema_version':'secretary.synthetic-stage.v1','stage':options.stage,'pid':os.getpid(),
                'input_sha256':observed,'selected_text_sha256':request['payload']['artifacts']['selected_text_sha256']}
    options.output.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True),encoding='utf-8')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
