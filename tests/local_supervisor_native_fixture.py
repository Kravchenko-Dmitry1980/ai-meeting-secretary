"""Synthetic-only native P5 acceptance. No production lifecycle commands."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
BASE = None
PYTHON = ROOT / '.venv/Scripts/python.exe'
sys.path.insert(0, str(ROOT / 'scripts'))
import local_supervisor as module
from secretary.infrastructure.instance_lock import DataDirectoryLock
from secretary.infrastructure.team_process_identity import process_identity, verify_dead_identity

RESULTS = {}
HANDLES = []
MAIN_HASH = None
MAIN_HEALTHY = False


def save():
    (BASE / 'receipt.json').write_text(json.dumps(RESULTS, indent=2), encoding='utf-8')


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        value = sock.getsockname()[1]
    assert value != 8765
    return value


def state(handle):
    return module.read_state(handle['runtime'])


def wait_for(handle, predicate, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = state(handle)
        if current and predicate(current):
            return current
        if handle['process'].poll() is not None:
            raise AssertionError(f"premature exit: {current}")
        time.sleep(.025)
    raise AssertionError(f"timeout: {state(handle)}")


def start(name, requested_port=None):
    directory = BASE / name
    directory.mkdir()
    data = directory / 'data'
    data.mkdir()
    runtime = directory / 'owner'
    runtime.mkdir()
    selected = requested_port or port()
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATHEXT', 'COMSPEC',
        'APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH'}}
    env['PYTHONUTF8'] = '1'
    env['PYTHONPATH'] = str(ROOT / 'backend')
    command = [str(PYTHON), '-B', str(ROOT / 'scripts/local_supervisor.py'), 'run',
               '--data-dir', str(data), '--port', str(selected), '--runtime-root', str(runtime)]
    with (directory / 'stdout.log').open('xb') as out, (directory / 'stderr.log').open('xb') as err:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
    handle = {'process': process, 'runtime': runtime, 'data': data, 'port': selected}
    HANDLES.append(handle)
    return handle


def stop(handle):
    result = module.request_stop(handle['runtime'])
    handle['process'].wait(timeout=5)
    assert result['state'] == 'stopped', result
    assert handle['process'].returncode == 0
    return result


def assert_dead(record):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            verify_dead_identity(record)
            return
        except ValueError as exc:
            assert str(exc) == 'process_still_alive', str(exc)
        time.sleep(.1)
    raise AssertionError('owned fixture process survived')


def crash_owned(handle, record):
    # Fresh actual identity and own process ancestry before native termination.
    assert process_identity(record['pid']) == record
    target = psutil.Process(record['pid'])
    current = state(handle)
    owner_pid = current['supervisor_identity']['pid']
    assert record['pid'] == owner_pid or owner_pid in [parent.pid for parent in target.parents()]
    assert owner_pid == handle['process'].pid or handle['process'].pid in [parent.pid for parent in psutil.Process(owner_pid).parents()]
    target.kill()
    assert_dead(record)


def powershell(command):
    return subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)


def lease_both_orders():
    runtime = BASE / 'lease'
    runtime.mkdir()
    path = str(runtime / 'local-owner.lock').replace("'", "''")
    open_code = f"$f=[IO.File]::Open('{path}',[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)"
    with module.owner_lease(runtime):
        child = powershell("try {" + open_code + ";$f.Dispose();exit 0} catch {exit 41}")
        assert child.wait(timeout=10) == 41
    ready = runtime / 'ready'
    done = runtime / 'done'
    child = powershell(open_code + f";try {{[IO.File]::WriteAllText('{ready}','ready');while(-not [IO.File]::Exists('{done}')){{Start-Sleep -Milliseconds 100}}}} finally {{$f.Dispose()}}")
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(.05)
        assert ready.exists()
        try:
            with module.owner_lease(runtime):
                raise AssertionError('Python acquired PowerShell lease')
        except module.SupervisorError as exc:
            assert exc.code == 'local_owner_busy'
    finally:
        done.write_text('done')
        child.wait(timeout=5)
    RESULTS['lease_both_orders'] = 'PASS'
    save()
    print('lease_both_orders PASS', flush=True)


def main():
    lease_both_orders()
    first = start('graceful')
    current = wait_for(first, lambda row: row['state'] == 'running')
    assert module.status(first['runtime'])['supervisor_alive']
    identities = current['attempt_identities']
    stop(first)
    for item in identities:
        assert_dead(item)
    module.prepare_manual(first['runtime'])
    RESULTS['graceful_stop_and_manual_handoff'] = {'status': 'PASS', 'distinct_launcher_listener': len(identities) == 2}
    save()
    print('graceful PASS', flush=True)

    second = start('launcher-crash-stop-backoff')
    current = wait_for(second, lambda row: row['state'] == 'running')
    assert current['attempt_identities'][0] != current['listener_identity']
    crash_owned(second, current['attempt_identities'][0])
    after = wait_for(second, lambda row: row['state'] == 'backoff')
    assert after['restart_count'] == 1
    assert_dead(current['listener_identity'])
    stop(second)
    assert state(second)['restart_count'] == 1
    RESULTS['launcher_crash_drained_then_backoff_stop'] = 'PASS'
    save()
    print('launcher-crash/backoff-stop PASS', flush=True)

    third = start('bounded-server-crashes')
    attempts = []
    for number in range(4):
        current = wait_for(third, lambda row: row['state'] == 'running' and row['restart_count'] == number)
        attempts.append(current['run_id'])
        crash_owned(third, current['listener_identity'])
    third['process'].wait(timeout=10)
    final = module.status(third['runtime'])
    assert final['state'] == 'failed' and final['error'] == 'restart_limit_exhausted'
    assert final['restart_count'] == 3 and len(set(attempts)) == 4
    for record in final['attempt_identities']:
        assert_dead(record)
    RESULTS['bounded_server_crashes'] = {'status': 'PASS', 'launches': 4, 'restart_count': 3, 'configured_delays_seconds': [1, 2, 4]}
    save()
    print('bounded-server-crashes PASS', flush=True)

    fourth = start('supervisor-crash')
    current = wait_for(fourth, lambda row: row['state'] == 'running')
    crash_owned(fourth, current['supervisor_identity'])
    fourth['process'].wait(timeout=5)
    for record in current['attempt_identities']:
        assert_dead(record)
    assert module.status(fourth['runtime'])['state'] == 'owner_exited'
    RESULTS['supervisor_crash_containment'] = 'PASS'
    save()
    print('supervisor-crash PASS', flush=True)

    with socket.socket() as foreign:
        foreign.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        foreign.bind(('127.0.0.1', 0))
        foreign.listen()
        fifth = start('foreign-port', foreign.getsockname()[1])
        fifth['process'].wait(timeout=15)
        current = state(fifth)
        assert current['state'] == 'failed' and current['error'] == 'port_occupied'
        assert current['attempt_identities'] == []
        assert foreign.fileno() >= 0
    RESULTS['foreign_port_refused'] = 'PASS'

    # Create exact synthetic directory before child and hold only its data lease.
    directory = BASE / 'locked-data'
    directory.mkdir()
    synthetic_data = directory / 'data'
    with DataDirectoryLock(synthetic_data):
        # start() creates a new directory; make separate launcher setup explicitly.
        runtime = directory / 'owner'
        runtime.mkdir()
        env = os.environ.copy()
        env['PYTHONPATH'] = str(ROOT / 'backend')
        with (directory / 'stdout.log').open('xb') as out, (directory / 'stderr.log').open('xb') as err:
            process = subprocess.Popen([str(PYTHON), '-B', str(ROOT / 'scripts/local_supervisor.py'), 'run',
                '--data-dir', str(synthetic_data), '--port', str(port()), '--runtime-root', str(runtime)],
                cwd=ROOT, env=env, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        locked = {'process': process, 'runtime': runtime, 'data': synthetic_data}
        HANDLES.append(locked)
        process.wait(timeout=15)
        assert state(locked)['error'] == 'data_directory_in_use'
        assert state(locked)['attempt_identities'] == []
    RESULTS['data_lease_refused'] = 'PASS'
    path = ROOT / '.runtime/processes.json'
    after_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    assert after_hash == MAIN_HASH
    if MAIN_HEALTHY:
        with httpx.Client(timeout=3, trust_env=False) as client:
            assert client.get('http://127.0.0.1:8765/health').json()['status'] == 'ok'
    RESULTS['production_state_unchanged'] = True
    RESULTS['completed'] = True
    save()
    print('all-native-checks PASS', flush=True)


def cli():
    global BASE, MAIN_HASH, MAIN_HEALTHY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True,
                        help='Run owned synthetic Windows processes; never use the live data directory')
    parser.parse_args()
    if not __debug__:
        parser.error('Optimized Python is not supported for native acceptance')
    if os.name != 'nt':
        parser.error('Windows is required')
    if os.environ.get('SECRETARY_OFFLINE_RETENTION_RUN'):
        parser.error('Run this fixture explicitly outside the offline pytest guard')
    BASE = module.runtime_path(ROOT / '.runtime' / ('p5-local-native-' + uuid4().hex))
    BASE.mkdir()
    path = ROOT / '.runtime/processes.json'
    MAIN_HASH = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    with httpx.Client(timeout=2, trust_env=False) as client:
        try:
            MAIN_HEALTHY = client.get('http://127.0.0.1:8765/health').json().get('status') == 'ok'
        except (httpx.HTTPError, ValueError):
            pass
    print(f'Fixture: {BASE}', flush=True)
    try:
        main()
    except BaseException as exc:
        RESULTS['failure_type'] = type(exc).__name__
        RESULTS['failure'] = str(exc)
        save()
        raise
    finally:
        for handle in HANDLES:
            current = state(handle)
            if current and handle['process'].poll() is None:
                try:
                    module.request_stop(handle['runtime'])
                    handle['process'].wait(timeout=5)
                except Exception:
                    # Test-only emergency containment of this exact verified owner.
                    crash_owned(handle, current['supervisor_identity'])
            elif current is None and handle['process'].poll() is None:
                # This is a verified live handle, never a stale PID. No unknown
                # descendant is touched or a failed scenario restarted blindly.
                RESULTS['cleanup_blocked'] = 'live_fixture_owner_without_receipt'
                RESULTS['live_fixture_launcher_pid'] = handle['process'].pid
                save()
                raise RuntimeError('live_fixture_owner_without_receipt')


if __name__ == '__main__':
    cli()
