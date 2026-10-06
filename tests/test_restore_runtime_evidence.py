"""Stopped-runtime evidence over synthetic stores; no lifecycle mutations."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.infrastructure.team_maintenance import MaintenanceRepository
from secretary.infrastructure.team_process_identity import DeadProcessProof


def implementation():
    name = 'secretary.infrastructure.restore_runtime_evidence'
    assert importlib.util.find_spec(name), 'Stopped runtime verifier is missing'
    return importlib.import_module(name)


def identity(pid):
    return dict(pid=pid, creation_time=str(134000000000000000 + pid),
                executable_sha256='a' * 64, argv_sha256='b' * 64)


def dead(value):
    return DeadProcessProof(**value, state='exited', observed_at=datetime.now(timezone.utc).isoformat())


def stopped(record):
    return dead({key: record[key] for key in ('pid', 'creation_time', 'executable_sha256')}
                | {'argv_sha256': record['argv_hash']})


@contextmanager
def scope(path):
    yield


@contextmanager
def edit(path):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'synthetic-project'
    root.mkdir()
    control = root / 'control.sqlite'
    deployment, run = str(uuid4()), str(uuid4())
    gate = MaintenanceRepository(control, deployment)
    gate.bind_sources({role: root / (role + '.sqlite') for role in ('secretary', 'team', 'billing', 'vikunja')})
    participants = ['secretary', 'gateway:' + run]
    for index, name in enumerate(participants):
        gate.register_participant(name, run_id=run, identity=identity(10 if index == 0 else 22))
    gate.begin_barrier(owner_id='synthetic-backup')
    records = {}
    for index, name in enumerate(('supervisor', 'vikunja', 'gateway', 'proxy')):
        value = identity(20 + index)
        records[name] = {key: value[key] for key in ('pid', 'creation_time', 'executable_sha256')}
        records[name].update(executable=str(root / (name + '.exe')), argv_hash=value['argv_sha256'],
                             run_id=run, account='synthetic\\fixture')
    native = records['vikunja']
    archive = dict(schema_version=1, state='complete', deployment_id=deployment,
                   required_participants=participants, native_containment_verified=False,
                   native_quiescence=dict(deployment_id=deployment, run_id=run, component='vikunja',
                       barrier_id=str(uuid4()), barrier_fence=1, pid=native['pid'],
                       creation_time=native['creation_time'], executable_sha256=native['executable_sha256'],
                       argv_hash=native['argv_hash'], observed_stopped_at='2026-10-04T00:00:00+00:00'))
    lifecycle = dict(schema_version=1, deployment_id=deployment, run_id=run, desired='stopped',
                     phase='stopped', supervisor=records['supervisor'],
                     components={key: records[key] for key in ('vikunja', 'gateway', 'proxy')}, restarts={})
    path = root / 'lifecycle.json'
    path.write_text(json.dumps(lifecycle), encoding='utf-8')
    return SimpleNamespace(root=root, control=control, path=path, archive=archive,
                           lifecycle=lifecycle, gate=gate, run=run, participants=participants)


def verify(c, **kwargs):
    return implementation()._verify(project_root=c.root, archive_manifest=c.archive,
        maintenance_path=c.control, lifecycle_path=c.path, dead_verifier=kwargs.pop('dead_verifier', dead),
        supervisor_verifier=kwargs.pop('supervisor_verifier', stopped),
        protected_scope=kwargs.pop('protected_scope', scope), **kwargs)


def save(c):
    c.path.write_text(json.dumps(c.lifecycle), encoding='utf-8')


def reject(c, **kwargs):
    module = implementation()
    with pytest.raises(module.RuntimeEvidenceError) as caught:
        verify(c, **kwargs)
    assert str(caught.value) == caught.value.code
    assert str(c.root) not in str(caught.value)


def test_valid_stopped_runtime_is_stable_sanitized_and_read_only(case):
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (case.control, case.path)}
    first, second = verify(case), verify(case)
    assert first == second
    assert first['deployment_id'] == case.archive['deployment_id']
    assert first['participant_count'] == 2 and first['lifecycle_process_count'] == 4
    assert first['active_ticket_count'] == 0
    assert len(first['evidence_sha256']) == 64
    encoded = json.dumps(first)
    assert str(case.root) not in encoded and 'synthetic\\fixture' not in encoded
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    assert not any(Path(str(case.control) + suffix).exists() for suffix in ('-wal', '-shm', '-journal'))


@pytest.mark.parametrize('mode', ['open', 'unknown'])
def test_open_or_unknown_control_mode_refuses(case, mode):
    with edit(case.control) as conn:
        conn.execute('PRAGMA ignore_check_constraints=ON')
        conn.execute('UPDATE maintenance_state SET mode=?', (mode,))
    reject(case)


@pytest.mark.parametrize('mode', ['frozen', 'restore_blocked'])
def test_supported_closed_control_modes(case, mode):
    with edit(case.control) as conn:
        conn.execute('UPDATE maintenance_state SET mode=?', (mode,))
    assert verify(case)['deployment_id'] == case.archive['deployment_id']


def test_new_empty_control_not_authority(case):
    case.control = case.root / 'empty.sqlite'
    sqlite3.connect(case.control).close()
    reject(case)


@pytest.mark.parametrize('table', ['maintenance_participants', 'maintenance_sources'])
def test_missing_inventory_refuses(case, table):
    with edit(case.control) as conn:
        conn.execute('DROP TABLE ' + table)
    reject(case)


@pytest.mark.parametrize('kind', ['outbound_polza', 'capture_meeting', 'sql'])
def test_active_tickets_do_not_expire_into_quiescence(case, kind):
    with edit(case.control) as conn:
        conn.execute('INSERT INTO maintenance_tickets VALUES(?,?,?,?,?,?,?,?)',
            (str(uuid4()), 'secretary', case.run, kind, str(uuid4()), 0, 'active', '2000-01-01'))
    reject(case)


@pytest.mark.parametrize('change', ['control_deployment', 'lifecycle_deployment', 'component_run',
                                   'missing_component', 'missing_supervisor', 'desired_running', 'missing_required'])
def test_foreign_or_incomplete_authority_refuses(case, change):
    if change == 'control_deployment':
        with edit(case.control) as conn:
            conn.execute('UPDATE maintenance_state SET deployment_id=?', (str(uuid4()),))
    elif change == 'lifecycle_deployment': case.lifecycle['deployment_id'] = str(uuid4())
    elif change == 'component_run': case.lifecycle['components']['vikunja']['run_id'] = str(uuid4())
    elif change == 'missing_component': del case.lifecycle['components']['gateway']
    elif change == 'missing_supervisor': del case.lifecycle['supervisor']
    elif change == 'desired_running': case.lifecycle['desired'] = 'running'
    else: case.archive['required_participants'].append('missing')
    save(case)
    reject(case)


@pytest.mark.parametrize('which', ['participant', 'current_native', 'archive_native'])
def test_every_lifetime_must_be_proven_dead(case, which):
    if which == 'archive_native':
        case.archive['native_quiescence']['pid'] = 90
    def unavailable(value):
        if value['pid'] == (10 if which == 'participant' else 90):
            raise ValueError('process_still_alive')
        return dead(value)
    def native_unavailable(record):
        if which == 'current_native' and record['pid'] == 21:
            raise ValueError('process_identity_unavailable')
        return stopped(record)
    reject(case, dead_verifier=unavailable, supervisor_verifier=native_unavailable)


@pytest.mark.parametrize('which', ['dead_verifier', 'supervisor_verifier'])
@pytest.mark.parametrize('bad', ['boolean', 'wrong_identity', 'bad_state', 'bad_time'])
def test_callback_boolean_or_mismatched_proof_never_qualifies(case, which, bad):
    def invalid(value):
        proof = dead(value) if which == 'dead_verifier' else stopped(value)
        if bad == 'boolean': return True
        if bad == 'wrong_identity': return replace(proof, executable_sha256='f' * 64)
        if bad == 'bad_state': return replace(proof, state='alive')
        return replace(proof, observed_at='unknown')
    reject(case, **{which: invalid})


def test_pid_reused_is_typed_old_lifetime_proof(case):
    assert verify(case, dead_verifier=lambda value: replace(dead(value), state='pid_reused'))['participant_count'] == 2


def test_wal_control_reads_latest_closed_mode_not_stale_main_file(case, monkeypatch):
    # Solely bypass the Windows sharing lease to exercise SQLite's actual WAL
    # read while this synthetic test retains its writer; production never does.
    monkeypatch.setattr(implementation(), '_pin', scope)
    writer = sqlite3.connect(case.control)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("UPDATE maintenance_state SET mode='open'")
        writer.commit()
        assert Path(str(case.control) + '-wal').stat().st_size > 0
        reject(case)
    finally:
        writer.close()


def test_rollback_journal_is_not_recovered_or_ignored(case):
    journal = Path(str(case.control) + '-journal')
    journal.write_bytes(b'synthetic unverified recovery')
    reject(case)
    assert journal.read_bytes() == b'synthetic unverified recovery'


@pytest.mark.parametrize('target', ['control', 'lifecycle'])
def test_evidence_changed_during_process_checks_refuses(case, target, monkeypatch):
    monkeypatch.setattr(implementation(), '_pin', scope)
    changed = []
    def mutate(value):
        if not changed:
            changed.append(True)
            if target == 'control':
                with edit(case.control) as conn:
                    conn.execute("UPDATE maintenance_state SET owner_id='changed'")
            else:
                case.lifecycle['phase'] = 'changed'
                save(case)
        return dead(value)
    with pytest.raises(implementation().RuntimeEvidenceError, match='restore_runtime_evidence_changed'):
        verify(case, dead_verifier=mutate)
    assert changed == [True]


@pytest.mark.parametrize('payload', ['{"schema_version":1,"schema_version":1}', '[]', '{', ' ' * (128 * 1024 + 1)],
                         ids=['duplicate', 'array', 'invalid', 'oversized'])
def test_lifecycle_json_bounded_unique_and_object(case, payload):
    case.path.write_text(payload, encoding='utf-8')
    reject(case)


def test_paths_must_be_absolute_project_local(case, tmp_path):
    case.path = Path('lifecycle.json')
    reject(case)
    case.path = tmp_path / 'foreign.json'
    case.path.write_text('{}')
    reject(case)


def test_custody_failure_no_process_queries(case):
    @contextmanager
    def denied(path):
        raise ValueError('private custody required')
        yield
    def unexpected(value):
        pytest.fail('No process read before custody verification')
    reject(case, protected_scope=denied, dead_verifier=unexpected)


@pytest.mark.skipif(os.name != 'nt', reason='Actual FILETIME proof is Windows-only')
def test_actual_current_process_cannot_prove_stopped():
    from secretary.infrastructure.team_process_identity import process_identity
    module = implementation()
    value = process_identity()
    record = {key: value[key] for key in ('pid', 'creation_time', 'executable_sha256')}
    import sys
    record.update(executable=sys.executable, argv_hash=value['argv_sha256'], run_id=str(uuid4()), account='fixture')
    with pytest.raises(ValueError):
        module._verify_supervisor(record)


@pytest.mark.skipif(os.name != 'nt', reason='Actual FILETIME proof is Windows-only')
def test_actual_owned_child_exit_is_typed_proof_without_kill():
    import subprocess
    import sys
    code = ('import json,sys; from secretary.infrastructure.team_process_identity import process_identity; '
            'print(json.dumps(dict(identity=process_identity(),executable=sys.executable)))')
    child = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=15, check=True)
    value = json.loads(child.stdout)
    record = {key: value['identity'][key] for key in ('pid', 'creation_time', 'executable_sha256')}
    record.update(executable=value['executable'], argv_hash=value['identity']['argv_sha256'],
                  run_id=str(uuid4()), account='fixture')
    proof = implementation()._verify_supervisor(record)
    assert type(proof) is DeadProcessProof and proof.state in ('exited', 'pid_reused')
    assert proof.pid == value['identity']['pid']


def test_lifecycle_gateway_must_match_current_registry_run_identity(case):
    case.lifecycle['components']['gateway']['pid'] += 100
    save(case)
    reject(case)


@pytest.mark.parametrize('target', ['control', 'lifecycle'])
def test_retained_windows_file_pin_refuses_write_during_checks(case, target):
    if os.name != 'nt':
        pytest.skip('Windows share modes')
    attempted = []
    def attempt(value):
        if not attempted:
            attempted.append(True)
            with pytest.raises(OSError):
                (case.control if target == 'control' else case.path).write_bytes(b'replace')
        return dead(value)
    assert verify(case, dead_verifier=attempt)['participant_count'] == 2
    assert attempted == [True]


def test_archive_native_verified_even_when_archive_boolean_false(case):
    seen = []
    def record(value):
        seen.append(value['pid'])
        return dead(value)
    assert case.archive['native_containment_verified'] is False
    verify(case, dead_verifier=record)
    assert 21 in seen


def test_unexpected_sidecar_creation_is_not_normalized_away(case, monkeypatch):
    monkeypatch.setattr(implementation(), '_pin', scope)
    created = []
    def sidecar(value):
        if not created:
            created.append(True)
            Path(str(case.control) + '-wal').write_bytes(b'')
        return dead(value)
    with pytest.raises(implementation().RuntimeEvidenceError, match='restore_runtime_evidence_changed'):
        verify(case, dead_verifier=sidecar)
