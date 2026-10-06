"""Actual R3 copies are read back; operator and stopped-runtime ports are synthetic."""
from contextlib import closing
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
from test_restore_reconciliation import setup, run, sql


NAME = 'secretary.infrastructure.restore_activation_context'


def module():
    assert importlib.util.find_spec(NAME), 'actual R4 prepared-source collector is missing'
    return importlib.import_module(NAME)


@pytest.fixture
def prepared(setup, monkeypatch):
    sample, r3, proof = setup
    decision = run(setup)
    target = module()
    monkeypatch.setattr(target, 'OperatorAuthority', r3.OperatorAuthority)
    monkeypatch.setattr(target, 'protected_scope', r3.protected_scope)
    monkeypatch.setattr(target, 'verify_stopped_runtime', lambda **kwargs: dict(proof))
    return sample, target, decision, proof


def read(prepared, **changes):
    sample, target, _, _ = prepared
    arguments = dict(project_root=sample.tmp, backup_dir=sample.backup,
        restore_dir=sample.restored.destination,
        maintenance_path=sample.tmp / 'original-control.sqlite3',
        lifecycle_path=sample.tmp / 'original-lifecycle.json')
    arguments.update(changes)
    return target.collect_prepared_activation_context(**arguments)


def observed_files(sample):
    # The T12 fixture deliberately keeps its ORIGINAL synthetic native writer
    # running. Compare every collector input, not that unrelated live source.
    output = {}
    for name, root in (('archive', sample.backup), ('restore', sample.restored.destination),
                       ('operator', sample.tmp / '.runtime/team-operator')):
        output.update({name + '/' + key: value for key, value in files(root).items()})
    for name in ('original-control.sqlite3', 'original-lifecycle.json'):
        output[name] = hashlib.sha256((sample.tmp / name).read_bytes()).hexdigest()
    return output


def test_collector_reads_actual_complete_decision_and_all_four_sources(prepared):
    sample, _, decision, _ = prepared
    before = observed_files(sample)
    snapshot = read(prepared)
    assert snapshot.state == 'prepared_sources_verified_blocked'
    assert snapshot.activation_supported is False and snapshot.outbound_enabled is False
    assert snapshot.r3_id == decision['r3_id']
    assert snapshot.r3_decision_sha256 == decision['decision_sha256']
    assert snapshot.r3_binding_sha256 == decision['binding_sha256']
    assert {item.role for item in snapshot.sources} == {'secretary', 'team', 'billing', 'vikunja'}
    assert set(snapshot.ledger_binding) == {'protocol', 'restore_id', 'r3_id',
        'r3_decision_sha256', 'r3_binding_sha256', 'manifest_sha256', 'metadata_sha256',
        'inventory_sha256', 'source_context_sha256'}
    for item in snapshot.sources:
        path = sample.restored.databases[item.role]
        assert item.path == str(path)
        assert item.file_id == (path.stat().st_dev, path.stat().st_ino)
        assert item.baseline_file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert len(item.source_commitment_sha256) == 64 and len(item.preparation_sha256) == 64
    assert read(prepared) == snapshot
    assert observed_files(sample) == before
    assert SENSITIVE not in json.dumps(asdict(snapshot))
    assert 'synthetic-key' not in json.dumps(asdict(snapshot))


def test_post_preparation_hashes_are_compared_to_r3_not_original_archive(prepared):
    sample, _, decision, _ = prepared
    snapshot = read(prepared)
    for item in snapshot.sources:
        if item.role == 'vikunja':
            assert item.baseline_file_sha256 == decision['native_file_sha256']
        else:
            assert item.baseline_file_sha256 == decision['preparations'][item.role]['file_sha256']
            original = sample.backup / 'databases' / (item.role + '.sqlite3')
            assert item.baseline_file_sha256 != hashlib.sha256(original.read_bytes()).hexdigest()


@pytest.mark.parametrize('name', ['inventory.json', 'control.sqlite3'])
def test_missing_independent_r3_file_never_creates_or_adopts_it(prepared, name):
    sample, target, _, _ = prepared
    path = next((sample.tmp / '.runtime/team-operator/restores').rglob(name))
    path.unlink()
    before = observed_files(sample)
    with pytest.raises(target.ActivationContextError, match='maintenance_restore_blocked'):
        read(prepared)
    assert not path.exists() and observed_files(sample) == before


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_changed_source_is_refused_without_mutating_any_store(prepared, role):
    sample, target, _, _ = prepared
    statement = {'secretary': "UPDATE jobs SET status='completed'", 'team':
        "UPDATE team_meta SET value='changed' WHERE key='schema_version'", 'billing':
        'UPDATE billing_charges SET observed_cost_micro=700000', 'vikunja':
        'UPDATE files SET size=size+1'}[role]
    if role == 'team':
        sql(sample.restored.databases[role], 'CREATE TABLE unauthorized_change(value INTEGER)')
    else:
        sql(sample.restored.databases[role], statement)
    before = observed_files(sample)
    with pytest.raises(target.ActivationContextError): read(prepared)
    assert observed_files(sample) == before


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_source_journals_are_refused_without_deleting_or_ignoring_them(prepared, suffix):
    sample, target, _, _ = prepared
    path = Path(str(sample.restored.databases['team']) + suffix)
    path.write_bytes(b'synthetic active SQLite journal')
    before = observed_files(sample)
    with pytest.raises(target.ActivationContextError): read(prepared)
    assert observed_files(sample) == before


@pytest.mark.parametrize('change', ['asset', 'metadata', 'archive', 'inventory', 'scope'])
def test_changed_retained_inputs_or_r3_scope_are_refused(prepared, change):
    sample, target, _, _ = prepared
    if change == 'metadata':
        (sample.restored.destination / 'restore.json').write_text('{}')
    elif change == 'archive':
        (sample.backup / 'databases/team.sqlite3').write_bytes(b'invalid archive')
    elif change == 'asset':
        path = next(path for path in (sample.restored.destination / 'assets').rglob('*')
            if path.is_file() and path not in sample.restored.databases.values())
        path.write_bytes(b'changed synthetic retained asset')
    else:
        path = next((sample.tmp / '.runtime/team-operator/restores').rglob('inventory.json'))
        document = json.loads(path.read_text())
        if change == 'inventory': document['plan']['entries'].clear()
        else: document['scope']['target_id'] = [0, 0]
        path.write_text(json.dumps(document))
    before = observed_files(sample)
    with pytest.raises(target.ActivationContextError): read(prepared)
    assert observed_files(sample) == before


def test_runtime_changes_during_read_are_refused(prepared, monkeypatch):
    _, target, _, proof = prepared
    calls = 0
    def changed(**kwargs):
        nonlocal calls
        calls += 1
        return dict(proof, evidence_sha256=('b' if calls == 1 else 'c') * 64)
    monkeypatch.setattr(target, 'verify_stopped_runtime', changed)
    with pytest.raises(target.ActivationContextError): read(prepared)
    assert calls == 2


def test_operator_is_verified_freshly_and_not_inferred_from_archive(prepared, monkeypatch):
    _, target, _, _ = prepared
    class MissingCurrentOperator:
        def __init__(self, root): raise ValueError(SENSITIVE)
    monkeypatch.setattr(target, 'OperatorAuthority', MissingCurrentOperator)
    with pytest.raises(target.ActivationContextError) as result: read(prepared)
    assert str(result.value) == 'maintenance_restore_blocked'
    assert SENSITIVE not in repr(result.value)


def test_incomplete_r3_never_becomes_verified_context(setup, monkeypatch):
    sample, r3, proof = setup
    complete = r3.RestoreControl.complete
    monkeypatch.setattr(r3.RestoreControl, 'complete', lambda *args, **kwargs:
        (_ for _ in ()).throw(RuntimeError('synthetic failure before final R3 decision')))
    with pytest.raises(RuntimeError): run(setup)
    monkeypatch.setattr(r3.RestoreControl, 'complete', complete)
    target = module()
    monkeypatch.setattr(target, 'OperatorAuthority', r3.OperatorAuthority)
    monkeypatch.setattr(target, 'protected_scope', r3.protected_scope)
    monkeypatch.setattr(target, 'verify_stopped_runtime', lambda **kwargs: dict(proof))
    before = observed_files(sample)
    with pytest.raises(target.ActivationContextError): read((sample, target, None, proof))
    assert observed_files(sample) == before


def test_runtime_original_paths_cannot_point_inside_restore_or_archive(prepared):
    sample, target, _, _ = prepared
    with pytest.raises(target.ActivationContextError):
        read(prepared, maintenance_path=sample.restored.databases['team'])
    with pytest.raises(target.ActivationContextError):
        read(prepared, lifecycle_path=sample.backup / 'manifest.json')


def test_collector_has_no_caller_supplied_decision_or_proof_port(prepared):
    with pytest.raises(TypeError): read(prepared, decision={'outbound_enabled': True})
    with pytest.raises(TypeError): read(prepared, provider_proof=True)


def test_r3_open_existing_reader_never_creates_missing_control(tmp_path):
    from secretary.infrastructure.restore_reconciliation_control import RestoreControl, ControlError
    path = tmp_path / 'missing-control.sqlite3'
    binding = dict(protocol=1, restore_id=str(uuid4()), **{key: 'a' * 64 for key in
        ('plan_sha256', 'input_sha256', 'scope_sha256', 'evidence_sha256',
         'source_paths_sha256', 'operator_authority_sha256')})
    assert hasattr(RestoreControl, 'open_existing'), 'R3 explicit non-creating reader is missing'
    with pytest.raises(ControlError): RestoreControl.open_existing(path, binding)
    assert not path.exists() and list(tmp_path.iterdir()) == []


def test_r3_existing_reader_cannot_consume_a_valid_approval_or_record_a_receipt(tmp_path):
    from secretary.infrastructure.restore_reconciliation_control import RestoreControl, ControlError
    from secretary.infrastructure.restore_operator import VerifiedOperatorApproval
    path = tmp_path / 'existing-control.sqlite3'
    binding = dict(protocol=1, restore_id=str(uuid4()), **{key: 'a' * 64 for key in
        ('plan_sha256', 'input_sha256', 'scope_sha256', 'evidence_sha256',
         'source_paths_sha256', 'operator_authority_sha256')})
    RestoreControl(path, binding)
    reader = RestoreControl.open_existing(path, binding)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    approval = VerifiedOperatorApproval('a' * 64, 'b' * 64, 'c' * 64, int(time.time()) + 100)
    with pytest.raises(ControlError): reader.consume_approval(approval)
    with pytest.raises(ControlError): reader.record_preparation('secretary', 'b' * 64, 'c' * 64)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert reader.snapshot()['approval_digest'] is None
