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


def runtime_settings(sample, deployment_id, *, changes=None):
    from secretary.team_settings import load_team_settings
    data = dict(schema_version=1, deployment_id=deployment_id, project_dir=str(sample.tmp),
        secretary_database_path=str(sample.restored.databases['secretary']),
        team_database_path=str(sample.restored.databases['team']),
        billing_database_path=str(sample.restored.databases['billing']),
        vikunja_database_path=str(sample.restored.databases['vikunja']),
        control_database_path=str(sample.tmp / 'original-control.sqlite3'),
        restore_blocked=True, outbound_enabled=False)
    if changes:
        data.update(changes)
    path = sample.tmp / 'runtime-config.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return load_team_settings(path), path


def runtime_context_module():
    name = 'secretary.infrastructure.restore_runtime_context'
    assert importlib.util.find_spec(name), 'fixed R4 runtime-context resolver is missing'
    return importlib.import_module(name)


def test_collector_reads_actual_complete_decision_and_all_four_sources(prepared):
    sample, _, decision, proof = prepared
    before = observed_files(sample)
    snapshot = read(prepared)
    assert snapshot.state == 'prepared_sources_verified_blocked'
    assert snapshot.activation_supported is False and snapshot.outbound_enabled is False
    assert snapshot.r3_id == decision['r3_id']
    assert snapshot.r3_decision_sha256 == decision['decision_sha256']
    assert snapshot.r3_binding_sha256 == decision['binding_sha256']
    assert snapshot.expected_successor_fence == proof['maintenance_fence_lineage']['high_water_fence'] + 1
    assert len(snapshot.successor_fence_candidate_sha256) == 64
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


def test_runtime_resolver_derives_paths_from_loaded_config_and_r3_inventory(prepared):
    sample, _, _, proof = prepared
    settings, config_path = runtime_settings(sample, proof['deployment_id'])
    before = observed_files(sample)
    result = runtime_context_module().resolve_runtime_restore_context(settings)
    assert result.project_root == sample.tmp
    assert result.backup_dir == sample.backup
    assert result.restore_dir == sample.restored.destination
    assert result.maintenance_path == sample.tmp / 'original-control.sqlite3'
    assert result.lifecycle_path == sample.tmp / 'original-lifecycle.json'
    assert result.config_path == config_path
    assert result.prepared_context == read(prepared)
    assert result.activation_supported is False and result.outbound_enabled is False
    assert observed_files(sample) == before


@pytest.mark.asyncio
async def test_team_runtime_reads_r3_context_before_blocked_restore_startup(prepared):
    from secretary.orchestration.team_runtime import TeamRuntime

    sample, _, _, proof = prepared
    settings, _ = runtime_settings(sample, proof['deployment_id'])
    runtime = TeamRuntime(settings, run_id=str(uuid4()),
        control_dir=sample.tmp / '.runtime' / 'team-control')
    await runtime.start()
    try:
        assert runtime.components.get('restore_context') == {
            'state': 'not_configured',
            'error_code': 'team_restore_context_verified_blocked',
        }
        assert runtime.components['gateway']['error_code'] == 'team_restore_reconciliation_required'
        assert runtime.components['outbound']['error_code'] == 'team_restore_reconciliation_required'
        assert runtime.maintenance is None and runtime.team is None and runtime.provider is None
        assert not runtime._workers and runtime._accepting is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_team_runtime_stays_blocked_when_r3_context_cannot_be_verified(prepared):
    from secretary.orchestration.team_runtime import TeamRuntime

    sample, _, _, proof = prepared
    settings, _ = runtime_settings(sample, proof['deployment_id'])
    inventory = (sample.tmp / '.runtime' / 'team-operator' / 'restores' /
                 sample.restored.restore_id / 'inventory.json')
    inventory.unlink()
    runtime = TeamRuntime(settings, run_id=str(uuid4()),
        control_dir=sample.tmp / '.runtime' / 'team-control-unverified')
    await runtime.start()
    try:
        assert runtime.components.get('restore_context') == {
            'state': 'not_configured',
            'error_code': 'team_restore_context_unavailable',
        }
        assert runtime.components['gateway']['error_code'] == 'team_restore_reconciliation_required'
        assert runtime.components['outbound']['error_code'] == 'team_restore_reconciliation_required'
        assert runtime.maintenance is None and runtime.team is None and runtime.provider is None
        assert not runtime._workers and runtime._accepting is False
    finally:
        await runtime.close()


def test_runtime_resolver_refuses_unloaded_or_stale_settings(prepared):
    from secretary.team_settings import TeamRuntimeSettings
    sample, _, _, proof = prepared
    settings, config_path = runtime_settings(sample, proof['deployment_id'])
    target = runtime_context_module()
    detached = TeamRuntimeSettings.model_validate(settings.model_dump(mode='python'))
    with pytest.raises(target.ActivationContextError):
        target.resolve_runtime_restore_context(detached)
    detached._config_path = config_path
    with pytest.raises(target.ActivationContextError):
        target.resolve_runtime_restore_context(detached)
    config_path.write_text(config_path.read_text(encoding='utf-8').replace(
        '"restore_blocked": true', '"restore_blocked": false'), encoding='utf-8')
    with pytest.raises(target.ActivationContextError):
        target.resolve_runtime_restore_context(settings)


def test_runtime_resolver_refuses_configured_sources_that_differ_from_restore(prepared):
    sample, _, _, proof = prepared
    settings, _ = runtime_settings(sample, proof['deployment_id'], changes={
        'team_database_path': str(sample.tmp / 'unrelated-team.sqlite3')})
    target = runtime_context_module()
    with pytest.raises(target.ActivationContextError):
        target.resolve_runtime_restore_context(settings)


def test_runtime_resolver_checks_config_scope_before_opening_mutated_path(prepared, monkeypatch):
    sample, _, _, proof = prepared
    settings, _ = runtime_settings(sample, proof['deployment_id'])
    outside = sample.tmp.parent / (sample.tmp.name + '-outside-config.json')
    outside.write_text('{}', encoding='utf-8')
    settings._config_path = outside
    target = runtime_context_module()
    opened = []
    original = target._pin_file

    def observed_pin(path, **kwargs):
        opened.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setattr(target, '_pin_file', observed_pin)
    try:
        with pytest.raises(target.ActivationContextError):
            target.resolve_runtime_restore_context(settings)
        assert opened == []
    finally:
        outside.unlink(missing_ok=True)


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
