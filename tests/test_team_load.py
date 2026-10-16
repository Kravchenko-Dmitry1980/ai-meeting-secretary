"""T13 synthetic load/fault acceptance; two owned children, no live network.

Latency observations describe this guarded fixture on the current computer;
they do not qualify a production SLA or MAX phone delivery.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from secretary.application.monthly_budget import MonthlyBudget
from secretary.domain.cloud_budget import AccountUsage
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.domain.team import TaskCommand, TeamForbidden
from team_load_remote_fixture import NOW, DurableRemote, clients, connect, initialize


ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).with_name('team_load_remote_fixture.py')


def test_shared_remote_keeps_committed_faults_and_logs_every_attempt(tmp_path):
    """The fault fixture must not turn unknown results into harmless failures."""
    initialize(tmp_path)
    remote = DurableRemote(tmp_path)
    for task, expected in ((1001, 'rejected_429'), (1002, 'unknown_503'), (1003, 'unknown_timeout')):
        request = httpx.Request('PATCH', f'http://127.0.0.1:34891/api/v2/tasks/{task}',
                                json={'title': 'Synthetic fault proof'})
        for _ in range(2):
            if task == 1003:
                with pytest.raises(httpx.ReadTimeout):
                    remote(request)
            else:
                assert remote(request).status_code == (429 if task == 1001 else 503)
        with connect(tmp_path) as conn:
            assert conn.execute('SELECT COUNT(*) FROM attempts WHERE task=? AND outcome=?', (task, expected)).fetchone()[0] == 2
            assert conn.execute('SELECT COUNT(*) FROM effects WHERE task=?', (task,)).fetchone()[0] == (0 if task == 1001 else 2)
            current = json.loads(conn.execute('SELECT payload FROM tasks WHERE id=?', (task,)).fetchone()[0])
            assert current['title'] == ('Synthetic task 1001' if task == 1001 else 'Synthetic fault proof')


def wait_markers(directory, names, processes, timeout=120):
    deadline = time.monotonic() + timeout
    while not all((directory / name).is_file() for name in names):
        for index, process in enumerate(processes):
            if process.poll() is not None:
                if process.returncode == 0 and (directory / f'result-{index}.json').is_file():
                    continue
                stdout, stderr = process.communicate()
                pytest.fail(f'Owned synthetic child exited early: {process.returncode}\n{stdout}\n{stderr}')
        if time.monotonic() >= deadline:
            pytest.fail('Owned synthetic load barrier timed out')
        time.sleep(.02)


def percentiles(values):
    ordered = sorted(value / 1_000_000 for value in values)
    return dict(count=len(ordered), p50_ms=round(ordered[(len(ordered)-1)//2], 3),
                p95_ms=round(ordered[max(0, (len(ordered)*95+99)//100-1)], 3), max_ms=round(ordered[-1], 3))


def enable_mutation_rendezvous(directory, ready):
    value = dict(schema=1, run_id=uuid4().hex, pids=[item['pid'] for item in ready])
    with (directory / 'mutation-rendezvous.json').open('x', encoding='utf-8') as stream:
        json.dump(value, stream)
    return value


def test_first_mutation_rendezvous_requires_actual_owned_peer_and_runs_once(tmp_path):
    initialize(tmp_path)
    children = []
    try:
        for index in range(2):
            children.append(subprocess.Popen(
                [sys.executable, '-B', str(HELPER), str(tmp_path), '--mutation-only', str(index)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        wait_markers(tmp_path, [f'ready-{i}.json' for i in range(2)], children, timeout=30)
        ready = [json.loads((tmp_path / f'ready-{i}.json').read_text('utf-8')) for i in range(2)]
        # Windows venv launchers can delegate to a different interpreter PID.
        # These fresh helper-owned markers record the actual request processes.
        assert len({item['pid'] for item in ready}) == 2
        config = enable_mutation_rendezvous(tmp_path, ready)
        (tmp_path / 'go-mutation-0').touch()
        markers = [f"first-mutation-{config['run_id']}-{item['pid']}.json" for item in ready]
        wait_markers(tmp_path, markers[:1], children, timeout=30)
        with connect(tmp_path) as conn:
            attempts = list(conn.execute("SELECT * FROM attempts WHERE method<>'GET'"))
            assert len(attempts) == 1 and attempts[0]['finished_ns'] is None
            assert conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0
        # Only now admit the peer. A probabilistic sleep cannot satisfy this gate.
        (tmp_path / 'go-mutation-1').touch()
        wait_markers(tmp_path, [f'result-{i}.json' for i in range(2)], children, timeout=30)
        for process in children:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, f'{stdout}\n{stderr}'
        arrivals = [json.loads((tmp_path / name).read_text('utf-8')) for name in markers]
        with connect(tmp_path) as conn:
            attempts = [dict(row) for row in conn.execute("SELECT * FROM attempts WHERE method<>'GET'")]
            assert len(attempts) == 4 and conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 4
            first = [next(row for row in attempts if row['id'] == item['attempt_id']) for item in arrivals]
            assert [row['pid'] for row in first] == config['pids']
            assert all(item['run_id'] == config['run_id'] for item in arrivals)
            assert first[0]['started_ns'] < first[1]['finished_ns']
            assert first[1]['started_ns'] < first[0]['finished_ns']
        assert len(list(tmp_path.glob('first-mutation-*.json'))) == 2
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=10)


@pytest.mark.parametrize('invalid_pid', [True, -1, '42'])
def test_first_mutation_rendezvous_rejects_invalid_participants(tmp_path, invalid_pid):
    initialize(tmp_path)
    enable_mutation_rendezvous(tmp_path, [{'pid': os.getpid()}, {'pid': invalid_pid}])
    remote = DurableRemote(tmp_path, rendezvous_index=0, rendezvous_timeout=.05)
    with pytest.raises(ValueError, match='rendezvous_participants_invalid'):
        remote(httpx.Request('PATCH', 'http://127.0.0.1:34891/api/v2/tasks/1004',
                             json={'title': 'Synthetic must not dispatch'}))
    with connect(tmp_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0


def test_first_mutation_rendezvous_missing_peer_fails_without_effect(tmp_path):
    initialize(tmp_path)
    enable_mutation_rendezvous(tmp_path, [{'pid': os.getpid()}, {'pid': os.getpid() + 100000}])
    remote = DurableRemote(tmp_path, rendezvous_index=0, rendezvous_timeout=.05)
    with pytest.raises(TimeoutError, match='phase barrier timed out'):
        remote(httpx.Request('PATCH', 'http://127.0.0.1:34891/api/v2/tasks/1004',
                             json={'title': 'Synthetic must not dispatch'}))
    with connect(tmp_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0
        assert conn.execute('SELECT finished_ns FROM attempts').fetchone()[0] is None


def test_first_mutation_rendezvous_rejects_marker_without_actual_peer_attempt(tmp_path):
    initialize(tmp_path)
    peer_pid = os.getpid() + 100000
    config = enable_mutation_rendezvous(tmp_path, [{'pid': os.getpid()}, {'pid': peer_pid}])
    marker = tmp_path / f"first-mutation-{config['run_id']}-{peer_pid}.json"
    marker.write_text(json.dumps(dict(run_id=config['run_id'], pid=peer_pid,
                                     attempt_id=999, started_ns=1)), 'utf-8')
    remote = DurableRemote(tmp_path, rendezvous_index=0, rendezvous_timeout=.05)
    with pytest.raises(AssertionError, match='rendezvous_peer_attempt_invalid'):
        remote(httpx.Request('PATCH', 'http://127.0.0.1:34891/api/v2/tasks/1004',
                             json={'title': 'Synthetic must not dispatch'}))
    with connect(tmp_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0


def test_two_process_shared_authority_load_fault_invariants(tmp_path):
    from team_load_cloud_fixture import init_cloud, validate_cloud
    directory = tmp_path / 'load-authority'
    repo, members = initialize(directory)
    billing = MonthlyBudget(BudgetRepository(directory / 'billing.sqlite3'), clock=lambda: NOW)
    billing.refresh_account(AccountUsage(hashlib.sha256(b'synthetic-team-load-polza-not-real').hexdigest(),
        3000_000000, 3000_000000, 0, reset='monthly', observed_at=NOW))
    init_cloud(directory)
    started = time.perf_counter()
    children = []
    try:
        # A failed second spawn must still clean the first owned child.
        for index in range(2):
            children.append(subprocess.Popen([sys.executable, '-B', str(HELPER), str(directory), str(index)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        wait_markers(directory, [f'ready-{i}.json' for i in range(2)], children)
        ready = [json.loads((directory / f'ready-{i}.json').read_text(encoding='utf-8')) for i in range(2)]
        assert len({item['pid'] for item in ready}) == 2
        rendezvous = enable_mutation_rendezvous(directory, ready)
        (directory / 'go-sync').touch()
        wait_markers(directory, [f'synced-{i}.json' for i in range(2)], children)
        with repo.db.connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM team_projections').fetchone()[0] == 1000
        (directory / 'go-accept').touch()
        wait_markers(directory, [f'accepted-{i}.json' for i in range(2)], children)
        accepted = [json.loads((directory / f'accepted-{i}.json').read_text(encoding='utf-8')) for i in range(2)]
        # Both children really overlap intake, with 25 parallel callers each.
        assert max(item['started_ns'] for item in accepted) < min(item['finished_ns'] for item in accepted)
        commands = [row for item in accepted for row in item['operations']]
        in_flight, peak = 0, 0
        for _, delta in sorted([(row['started_ns'], 1) for row in commands] + [(row['finished_ns'], -1) for row in commands]):
            in_flight += delta
            peak = max(peak, in_flight)
        assert peak >= 25, 'At least one full cohort must overlap actual repository calls'
        operation_ids = {row['command']['operation_id'] for row in commands}
        assert len(commands) == len(operation_ids) == 50
        assert len({row['actor_id'] for row in commands}) == 10
        with repo.db.connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 50
            assert conn.execute("SELECT COUNT(*) FROM team_execution WHERE state='queued'").fetchone()[0] == 50
        (directory / 'go-execute').touch()
        wait_markers(directory, [f'result-{i}.json' for i in range(2)], children, timeout=150)
        for process in children:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, f'{stdout}\n{stderr}'
        results = [json.loads((directory / f'result-{i}.json').read_text(encoding='utf-8')) for i in range(2)]
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()  # Only children created by this exact test.
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=10)
    elapsed = time.perf_counter() - started
    receipts = {row['command']['operation_id']: repo.get_receipt(row['actor_id'], row['command']['operation_id']) for row in commands}
    states = Counter(value.execution_state.state for value in receipts.values())
    assert states == dict(applied=42, conflict=5, rejected=1, uncertain=2), states
    assert all(value.acceptance_receipt.decision == 'accepted' for value in receipts.values())
    executed = [operation for result in results for operation in result['executed']]
    assert len(executed) == len(set(executed)) == 45
    assert all(result['executed'] for result in results), 'Both real worker processes must execute'
    assert sum(result['acl_checks'] for result in results) == 20
    with connect(directory) as conn:
        pages = list(conn.execute("SELECT path,page FROM attempts WHERE method='GET' AND path IN('/projects/7/tasks','/projects/9/tasks') ORDER BY path,page"))
        assert [(row['path'], row['page']) for row in pages] == [(f'/projects/{project}/tasks', page) for project in ('7', '9') for page in range(1, 11)]
        attempts = [dict(row) for row in conn.execute("SELECT * FROM attempts WHERE method<>'GET'")]
        effects = [dict(row) for row in conn.execute('SELECT * FROM effects')]
        assert len(attempts) == 45 and len(effects) == 44
        assert len({row['logical'] for row in attempts}) == len(attempts)
        assert {row['pid'] for row in attempts} == {item['pid'] for item in ready}
        overlap = sum(a['pid'] != b['pid'] and a['started_ns'] < b['finished_ns'] and b['started_ns'] < a['finished_ns'] for i, a in enumerate(attempts) for b in attempts[i+1:])
        assert overlap > 0, 'Provider attempts must overlap across both Python processes'
        arrivals = [json.loads((directory / f"first-mutation-{rendezvous['run_id']}-{pid}.json").read_text('utf-8'))
                    for pid in rendezvous['pids']]
        assert [item['pid'] for item in arrivals] == rendezvous['pids']
        assert all(item['run_id'] == rendezvous['run_id'] for item in arrivals)
        assert all(any(row['id'] == item['attempt_id'] and row['pid'] == item['pid']
                       and row['started_ns'] == item['started_ns'] for row in attempts) for item in arrivals)
        for task in (1041, 1541, 1042, 1542, 1043):
            pair = [row for row in commands if row['command']['task_id'] == str(task)]
            assert Counter(receipts[row['command']['operation_id']].execution_state.state for row in pair) == dict(applied=1, conflict=1)
            winner = next(row for row in pair if receipts[row['command']['operation_id']].execution_state.state == 'applied')
            current = json.loads(conn.execute('SELECT payload FROM tasks WHERE id=?', (task,)).fetchone()[0])
            assert current['title'] == winner['command']['values']['title']
            assert sum(row['task'] == task for row in effects) == 1
        # Partial changes preserve fields outside this command's authority.
        for row in conn.execute('SELECT id,payload FROM tasks'):
            value = json.loads(row['payload'])
            assert value['description'] == '<p>Preserved synthetic HTML</p>'
            assert value['due_date'] == '0001-01-01T00:00:00Z' and value['done'] is False
            assert value['labels'] == [] and len(value['assignees']) == 1
    remote_clients = clients(directory, members)
    try:
        for row in commands:
            value = receipts[row['command']['operation_id']]
            if value.execution_state.state == 'applied':
                fresh = remote_clients[row['command']['project_id']].observe_task(row['command']['task_id'])
                assert value.current.remote_fingerprint == fresh.remote_fingerprint
        visible = 0
        for member in members:
            cursor, seen = None, set()
            while True:
                page = repo.list_projections(member.id, member.project_ids[0], limit=100, after=cursor)
                shown = page[:100]
                assert all(task.project_id == member.project_ids[0] for task in shown)
                assert not seen.intersection(task.task_id for task in shown)
                seen.update(task.task_id for task in shown)
                if len(page) <= 100:
                    break
                cursor = shown[-1].task_id
            assert len(seen) == 500
            visible += len(seen)
            foreign_operation = next(row for row in commands if row['command']['project_id'] not in member.project_ids)
            with pytest.raises(TeamForbidden):
                repo.get_receipt(member.id, foreign_operation['command']['operation_id'])
            foreign_baseline = repo.get_projection(foreign_operation['actor_id'], foreign_operation['command']['task_id'])
            forbidden = TaskCommand(operation_id=str(uuid4()), project_id=foreign_baseline.project_id,
                task_id=foreign_baseline.task_id, expected_revision=foreign_baseline.revision,
                expected_fingerprint=foreign_baseline.remote_fingerprint, action='rename',
                values={'title': 'Synthetic unauthorized mutation'})
            refusal = repo.accept_command(member.id, forbidden, expected_actor_revision=member.revision)
            assert refusal.acceptance_receipt.decision == 'rejected'
            assert refusal.execution_state.state == 'rejected' and refusal.current is None
    finally:
        for client in remote_clients.values():
            client._http.close()
    cloud = validate_cloud(directory, [result['cloud'] for result in results])
    evidence = dict(phase='T13 offline load/fault', qualified_live=False, synthetic_users=10,
        real_owned_python_processes=2, shared_authorities=['Team', 'Billing', 'durable simulated remote'],
        provider_tasks=1000, complete_provider_pages=len(pages), scoped_user_visible_tasks=visible,
        accepted_commands=50, peak_concurrent_acceptance_calls=peak, unauthorized_command_rejections=10,
        final_states=dict(states), observed_provider_side_effects=44, verified_applied_commands=42,
        overlapping_cross_process_attempt_pairs=overlap, elapsed_seconds=round(elapsed, 3),
        scan_seconds=[round(result['scan_seconds'], 3) for result in results],
        acceptance=percentiles([row['acceptance_ns'] for row in commands]),
        execution=percentiles([value for result in results for value in result['execution_ns']]),
        violations=dict(project_leaks=0, lost_accepted_commands=0, duplicate_side_effects=0,
            unnoticed_overwrites=0, budget_bypass=0), cloud=cloud,
        limitation='MockTransport; supported Team writers only. No production SLA, external native-write CAS, real MAX delivery, hardware or paid-cloud qualification.')
    report = ROOT / '.runtime/team-rollout' / ('t13-load-' + uuid4().hex)
    report.mkdir()
    (report / 'metrics.json').write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding='utf-8')
    (report / 'source-hashes.json').write_text(json.dumps({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__), HELPER, Path(__file__).with_name('team_load_cloud_fixture.py'))}, indent=2), encoding='utf-8')
    print('\nT13 sanitized load evidence: ' + str(report))
