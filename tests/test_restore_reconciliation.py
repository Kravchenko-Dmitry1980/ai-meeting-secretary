"""R3 actual copies/SQLite transactions; independent owner/runtime ports are synthetic."""
from contextlib import closing, contextmanager
from dataclasses import asdict
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import pytest

from test_team_restore_preview import copy, files, SENSITIVE


def module():
    name = 'secretary.infrastructure.team_restore_reconciliation'
    assert importlib.util.find_spec(name), 'R3 controlled preparation implementation is missing'
    return importlib.import_module(name)


@pytest.fixture
def setup(copy, monkeypatch):
    target = module()
    from secretary.domain.restore_quarantine import digest
    from secretary.infrastructure.restore_operator import VerifiedOperatorApproval
    root = copy.tmp
    anchor = root / '.runtime' / 'team-operator'
    anchor.mkdir(parents=True)
    (root / 'original-control.sqlite3').write_bytes(b'synthetic independent runtime control')
    (root / 'original-lifecycle.json').write_text('{}')

    class SyntheticOperator:
        operator_digest = 'a' * 64
        def __init__(self, project_root):
            assert project_root == root
            self.root = root
            self.anchor = anchor
        def issue(self, commitment, **kwargs):
            return {'commitment': commitment, 'nonce': uuid4().hex}
        def authenticate(self, commitment, receipt):
            assert receipt['commitment'] == commitment
            return VerifiedOperatorApproval(self.operator_digest, digest(receipt),
                hashlib.sha256(receipt['nonce'].encode()).hexdigest(), int(time.time()) + 300)

    @contextmanager
    def scope(path):
        # Test-only substitution. OS custody/principal tests have their own suite.
        yield

    monkeypatch.setattr(target, 'PROJECT_ROOT', root)
    monkeypatch.setattr(target, 'OperatorAuthority', SyntheticOperator)
    monkeypatch.setattr(target, 'protected_scope', scope)
    monkeypatch.setattr(target, 'create_private_directory', lambda path: path.mkdir())
    proof = {'deployment_id': str(uuid4()), 'evidence_sha256': 'b' * 64}
    monkeypatch.setattr(target, 'verify_stopped_runtime', lambda **kwargs: dict(proof))
    return copy, target, proof


def run(setup):
    sample, target, _ = setup
    return target.prepare_restoration(sample.backup, sample.restored.destination,
        maintenance_path=sample.tmp / 'original-control.sqlite3',
        lifecycle_path=sample.tmp / 'original-lifecycle.json', writer=lambda value: None)


def sql(path, statement):
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(statement).fetchall()


def test_actual_three_preparations_complete_last_without_activation(setup):
    sample, _, _ = setup
    archive = files(sample.backup)
    charges = sql(sample.restored.databases['billing'], 'SELECT * FROM billing_charges')
    original = {r: hashlib.sha256(p.read_bytes()).hexdigest() for r,p in sample.restored.databases.items()}
    result = run(setup)
    assert result['state'] == 'complete_prepared_blocked'
    assert result['activation_supported'] is False and result['outbound_enabled'] is False
    assert set(result['preparations']) == {'secretary', 'team', 'billing'}
    assert sql(sample.restored.databases['billing'], 'SELECT * FROM billing_charges') == charges
    for role in ('secretary', 'team', 'billing'):
        path = sample.restored.databases[role]
        assert sql(path, 'SELECT reconciliation_required FROM maintenance_restore_guard') == [(1,)]
        assert sql(path, 'SELECT count(*) FROM restore_quarantine_sets') == [(1,)]
        assert hashlib.sha256(path.read_bytes()).hexdigest() != original[role]
    assert hashlib.sha256(sample.restored.databases['vikunja'].read_bytes()).hexdigest() == original['vikunja']
    assert files(sample.backup) == archive
    assert SENSITIVE not in json.dumps(result)


def test_crash_after_local_commit_keeps_partial_blocked_and_resumes_from_readback(setup, monkeypatch):
    sample, target, _ = setup
    record = target.RestoreControl.record_preparation
    def crash(*args, **kwargs):
        raise RuntimeError('synthetic crash after first local commit')
    monkeypatch.setattr(target.RestoreControl, 'record_preparation', crash)
    with pytest.raises(RuntimeError):
        run(setup)
    assert sql(sample.restored.databases['secretary'], 'SELECT count(*) FROM restore_quarantine_sets') == [(1,)]
    assert not sql(sample.restored.databases['team'], "SELECT name FROM sqlite_master WHERE name='restore_quarantine_sets'")
    monkeypatch.setattr(target.RestoreControl, 'record_preparation', record)
    result = run(setup)
    assert result['state'] == 'complete_prepared_blocked'
    assert sql(sample.restored.databases['secretary'], 'SELECT count(*) FROM restore_quarantine_sets') == [(1,)]


def test_final_decision_failure_preserves_all_preparations_and_guard(setup, monkeypatch):
    _, target, _ = setup
    complete = target.RestoreControl.complete
    monkeypatch.setattr(target.RestoreControl, 'complete', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): run(setup)
    monkeypatch.setattr(target.RestoreControl, 'complete', complete)
    assert run(setup)['state'] == 'complete_prepared_blocked'


def test_completed_repeat_returns_same_decision_without_new_authorization(setup):
    first = run(setup)
    assert run(setup) == first


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_foreign_writer_blocks_before_any_preparation(setup, role):
    sample, target, _ = setup
    before = files(sample.restored.destination)
    with closing(sqlite3.connect(sample.restored.databases[role], isolation_level=None)) as conn:
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(target.ReconciliationError): run(setup)
        conn.rollback()
    assert files(sample.restored.destination) == before


def test_native_wal_header_uses_write_denial_lease_without_normalizing(setup):
    sample, target, _ = setup
    with closing(sqlite3.connect(sample.restored.databases['vikunja'])) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    path = sample.restored.databases['vikunja']
    before = path.read_bytes()
    assert run(setup)['state'] == 'complete_prepared_blocked'
    assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['business', 'asset', 'native', 'metadata'])
def test_changed_complete_inputs_cannot_reuse_decision(setup, change):
    sample, target, _ = setup
    run(setup)
    if change == 'business': sql(sample.restored.databases['secretary'], "UPDATE jobs SET status='completed'")
    elif change == 'native': sql(sample.restored.databases['vikunja'], 'UPDATE files SET size=size+1')
    elif change == 'metadata': (sample.restored.destination / 'restore.json').write_text('{}')
    else:
        assets = [p for p in (sample.restored.destination/'assets').rglob('*') if p.is_file() and p not in sample.restored.databases.values()]
        assert assets
        assets[0].write_bytes(b'changed synthetic asset')
    before = files(sample.restored.destination)
    with pytest.raises((target.ReconciliationError, ValueError)): run(setup)
    assert files(sample.restored.destination) == before


def test_live_runtime_or_unavailable_owner_fails_before_target_write(setup, monkeypatch):
    sample, target, _ = setup
    before = files(sample.restored.destination)
    def refuse(**kwargs): raise target.ReconciliationError('restore_runtime_not_stopped')
    monkeypatch.setattr(target, 'verify_stopped_runtime', refuse)
    with pytest.raises(target.ReconciliationError): run(setup)
    assert files(sample.restored.destination) == before


def test_runtime_proof_changes_before_final_decision_holds_all(setup, monkeypatch):
    sample, target, _ = setup
    calls = 0
    def evidence(**kwargs):
        nonlocal calls
        calls += 1
        return {'deployment_id': str(uuid4()), 'evidence_sha256': ('b' if calls == 1 else 'c') * 64}
    monkeypatch.setattr(target, 'verify_stopped_runtime', evidence)
    with pytest.raises(target.ReconciliationError, match='runtime_changed'): run(setup)
    for role in ('secretary','team','billing'):
        assert sql(sample.restored.databases[role], 'SELECT reconciliation_required FROM maintenance_restore_guard') == [(1,)]


def test_missing_control_never_adopts_existing_prepared_copies(setup):
    sample, target, _ = setup
    run(setup)
    control = next((sample.tmp/'.runtime/team-operator/restores').rglob('control.sqlite3'))
    control.unlink()
    with pytest.raises(target.ReconciliationError, match='control_missing'): run(setup)


def test_immutable_receipts_cannot_be_replaced_after_complete(setup):
    sample, _, _ = setup
    run(setup)
    with pytest.raises(sqlite3.DatabaseError):
        sql(sample.restored.databases['team'], 'DELETE FROM restore_quarantine_sets')
    with pytest.raises(sqlite3.DatabaseError):
        sql(sample.restored.databases['billing'], 'UPDATE maintenance_restore_guard SET reconciliation_required=0')


def test_old_jobs_remain_denied_after_complete(setup):
    sample, _, _ = setup
    run(setup)
    assert sql(sample.restored.databases['secretary'], "SELECT status FROM jobs WHERE id='legacy-job'") == [('queued',)]
    assert sql(sample.restored.databases['secretary'], "SELECT count(*) FROM restore_quarantine_entries WHERE family='jobs' AND disposition='no_replay'")[0][0] >= 1


def test_hidden_rowid_drift_after_commit_without_control_receipt_is_rejected(setup, monkeypatch):
    sample, target, _ = setup
    record = target.RestoreControl.record_preparation
    monkeypatch.setattr(target.RestoreControl, 'record_preparation',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError): run(setup)
    monkeypatch.setattr(target.RestoreControl, 'record_preparation', record)
    # All visible business columns remain identical; R2 multiset alone misses it.
    sql(sample.restored.databases['secretary'], "UPDATE jobs SET rowid=999 WHERE id='legacy-job'")
    with pytest.raises(target.ReconciliationError, match='rowids_changed'): run(setup)
    assert not sql(sample.restored.databases['team'], "SELECT name FROM sqlite_master WHERE name='restore_quarantine_sets'")


def test_controlled_writer_denies_business_dml_and_rolls_back_schema(setup, monkeypatch):
    sample, target, _ = setup
    from secretary.infrastructure.restore_quarantine_preparation import PreparationProgram, SQLStatement
    original = target.preparation_program
    def invalid(*args, **kwargs):
        program = original(*args, **kwargs)
        return PreparationProgram((*program.statements[:4], SQLStatement("UPDATE jobs SET status='completed'")))
    monkeypatch.setattr(target, 'preparation_program', invalid)
    before = files(sample.restored.destination)
    with pytest.raises(target.ReconciliationError): run(setup)
    assert files(sample.restored.destination) == before


def test_statement_deadline_interrupts_short_repeated_sql_before_commit(setup, monkeypatch):
    sample, target, _ = setup
    original = target.preparation_program
    def expired(*args, **kwargs):
        program = original(*args, **kwargs)
        # Every individual INSERT is too short to trigger SQLite's progress
        # callback. A whole batch still needs explicit statementwise checks.
        monkeypatch.setattr(target.time, 'monotonic', lambda: float('inf'))
        return program
    monkeypatch.setattr(target, 'preparation_program', expired)
    before = files(sample.restored.destination)
    with pytest.raises(target.ReconciliationError, match='sql_deadline'): run(setup)
    assert files(sample.restored.destination) == before


@pytest.mark.parametrize('change',['asset','metadata'])
def test_change_after_actual_collector_cannot_become_approval_baseline(setup,monkeypatch,change):
    sample,target,_ = setup
    original = target._initial_rowids
    def mutate(*args,**kwargs):
        rows = original(*args,**kwargs)
        if change == 'metadata':
            path = sample.restored.destination/'restore.json'
            path.write_bytes(path.read_bytes()+b'\n ')
        else:
            path = next(p for p in (sample.restored.destination/'assets').rglob('*')
                        if p.is_file() and p not in sample.restored.databases.values())
            path.write_bytes(b'changed during custody gap')
        return rows
    monkeypatch.setattr(target,'_initial_rowids',mutate)
    originals = {role:path.read_bytes() for role,path in sample.restored.databases.items()}
    with pytest.raises(target.ReconciliationError,match='input_changed|validation_failed'): run(setup)
    assert all(path.read_bytes() == originals[role] for role,path in sample.restored.databases.items())


@pytest.mark.parametrize('change',['asset','metadata'])
def test_retained_write_denial_lease_is_held_through_last_decision(setup,monkeypatch,change):
    sample,target,_ = setup
    if change == 'metadata': path = sample.restored.destination/'restore.json'
    else:
        path = next(p for p in (sample.restored.destination/'assets').rglob('*')
                    if p.is_file() and p not in sample.restored.databases.values())
    before = path.read_bytes()
    original = target.OperatorAuthority.authenticate
    calls = 0
    def attempt(self,*args,**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            # This happens after the final hash check and before complete.
            with pytest.raises(PermissionError): path.write_bytes(b'late write')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(target.OperatorAuthority,'authenticate',attempt)
    assert run(setup)['state'] == 'complete_prepared_blocked'
    assert calls == 2 and path.read_bytes() == before
