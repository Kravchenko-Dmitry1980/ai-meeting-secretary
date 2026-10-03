"""Verify real Windows start/stop, foreign-port preservation, and restart persistence.

Only this project's tracked server and this check's own helper are controlled.
Leaves Secretary running. No cloud or recording action is performed.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    work = root / '.runtime' / 'cwd with spaces'
    work.mkdir(parents=True, exist_ok=True)
    state = root / '.runtime' / 'processes.json'
    results = []

    def run_script(name, *args, expect=0):
        # Windows PowerShell 5 Start-Process can pass unrelated inheritable PIPE
        # handles to a detached server. File-backed logs avoid waiting for an EOF
        # from a long-lived descendant after the script itself has already exited.
        out_path = work / f'{len(results)}-{name}.stdout.log'
        err_path = work / f'{len(results)}-{name}.stderr.log'
        with out_path.open('w', encoding='utf-8') as out, err_path.open('w', encoding='utf-8') as err:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(root / 'scripts' / name), *args], cwd=work, stdout=out, stderr=err, text=True, timeout=60)
        result.stdout = out_path.read_text(encoding='utf-8', errors='replace')
        result.stderr = err_path.read_text(encoding='utf-8', errors='replace')
        if expect == 0:
            assert result.returncode == 0, (name, result.stdout, result.stderr)
        else:
            assert result.returncode != 0, (name, result.stdout, result.stderr)
        results.append({'script': name, 'exit_code': result.returncode, 'scope': 'own Secretary process'})
        return result

    run_script('stop.ps1')
    run_script('stop.ps1')
    # Bind an ephemeral test port before handing the socket to our helper.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        test_port = probe.getsockname()[1]
    # This stdlib-only socket helper uses the already installed base interpreter
    # directly: the Windows venv redirector would otherwise leave its child alive
    # when its own wrapper is terminated. The Secretary server uses .venv.
    helper_python = getattr(sys, '_base_executable', sys.executable)
    helper = subprocess.Popen([helper_python, '-c', f'import socket,time;s=socket.socket();s.bind(("127.0.0.1",{test_port}));s.listen();print("READY",flush=True);time.sleep(60)'], cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert helper.stdout.readline().strip() == 'READY'
        result = run_script('start.ps1', '-NoBrowser', '-Port', str(test_port), expect=1)
        assert helper.poll() is None, 'Occupied-port helper was terminated by Secretary'
        assert 'occupied' in (result.stdout + result.stderr).lower()
        results.append({'occupied_port': test_port, 'helper_preserved': True})
    finally:
        # Popen handle belongs to this check only; no PID enumeration or foreign process stop.
        helper.terminate()
        helper.wait(timeout=10)
    run_script('start.ps1', '-NoBrowser')
    first = json.loads(state.read_text(encoding='utf-8-sig'))
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=5) as client:
        before = client.get('/api/v1/meetings').raise_for_status().json()
        assert client.get('/health').raise_for_status().json()['status'] == 'ok'
    run_script('start.ps1', '-NoBrowser')
    assert json.loads(state.read_text(encoding='utf-8-sig'))['pid'] == first['pid'], 'Repeated start created a duplicate'
    run_script('doctor.ps1')
    run_script('stop.ps1')
    run_script('start.ps1', '-NoBrowser')
    with httpx.Client(base_url='http://127.0.0.1:8765', timeout=5) as client:
        after = client.get('/api/v1/meetings').raise_for_status().json()
    assert {item['id'] for item in before} == {item['id'] for item in after}
    results.append({'working_directory_contains_spaces': True, 'repeat_start_same_pid': True, 'meetings_preserved_after_restart': len(after), 'server_left_running': True})
    report = {'status': 'PASS', 'checks': results}
    target = root / '.runtime' / 'lifecycle-check.json'
    target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
