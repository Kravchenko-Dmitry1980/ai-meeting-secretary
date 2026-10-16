"""Actual inactive prepared sources; owner and stopped-runtime ports are synthetic."""
from dataclasses import FrozenInstanceError, asdict
from contextlib import closing
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import sqlite3

import pytest

from test_restore_source_epoch_writer import (
    copy, setup, prepared, staged, perform, companion, observed_files, io_path,
)
from test_restore_activation_staging import ledger_path


NAME = 'secretary.infrastructure.restore_fresh_evidence'


def module():
    assert importlib.util.find_spec(NAME), 'actual fresh prepared-source evidence collector is missing'
    return importlib.import_module(NAME)


@pytest.fixture
def sources(staged, monkeypatch):
    sample = staged[0][0]
    sample.r3_source_bytes = {role: path.read_bytes() for role, path in sample.restored.databases.items()}
    preparation = perform(staged)
    sample, context, _, proof = staged[0]
    target = module()
    monkeypatch.setattr(target, 'protected_scope', context.protected_scope)
    monkeypatch.setattr(target, 'verify_stopped_runtime', lambda **kwargs: dict(proof))
    return sample, preparation, target


def observe(sources, **changes):
    sample, _, target = sources
    arguments = dict(project_root=sample.tmp, backup_dir=sample.backup,
        restore_dir=sample.restored.destination,
        maintenance_path=sample.tmp / 'original-control.sqlite3',
        lifecycle_path=sample.tmp / 'original-lifecycle.json')
    arguments.update(changes)
    return target.prepared_restore_evidence(**arguments)


def test_fresh_evidence_requires_actual_existing_four_source_readbacks(sources):
    sample, preparation, target = sources
    before = observed_files(sample)
    with observe(sources) as evidence:
        assert evidence.restore_id == preparation['binding']['context']['restore_id']
        assert evidence.epoch_id == preparation['binding']['epoch_id']
        assert evidence.preparation_id == preparation['preparation_id']
        assert evidence.capabilities == tuple(preparation['binding']['capabilities'])
        assert evidence.scope_sha256 == preparation['binding']['context']['scope_sha256']
        assert evidence.binding_sha256 == preparation['binding_sha256']
        assert evidence.runtime_deployment_id == preparation['binding']['runtime_deployment_id']
        assert evidence.source_paths == tuple((role, str(path)) for role, path in sample.restored.databases.items())
        assert len(evidence.snapshot_sha256) == 64
        assert len(evidence.context_sha256) == 64
        assert len(evidence.native_head_sha256) == 64
        assert evidence.verify_current() is None
        liabilities = evidence.read_liabilities()
        assert len(liabilities) == 1
        row = liabilities[0]
        assert row.status == 'uncertain' and row.reserved_micro == 500000
        assert row.observed_cost_micro is None
        assert row.provider_request_id is None and row.provider_job_id is None
        assert 'payload' not in asdict(row)
        with pytest.raises(FrozenInstanceError): evidence.epoch_id = 'caller epoch'
        with pytest.raises(TypeError): target.SourceEvidence(snapshot_sha256='a' * 64)
    assert observed_files(sample) == before
    with pytest.raises(target.FreshEvidenceError, match='fresh_evidence_closed'):
        evidence.verify_current()
    with pytest.raises(target.FreshEvidenceError, match='fresh_evidence_closed'):
        evidence.read_liabilities()


def test_public_collector_accepts_only_explicit_locations():
    signature = inspect.signature(module().prepared_restore_evidence)
    assert tuple(signature.parameters) == ('project_root', 'backup_dir', 'restore_dir',
        'maintenance_path', 'lifecycle_path')


@pytest.mark.parametrize('which', ['ledger', 'proposal', 'r3', 'inventory', 'billing'])
def test_missing_existing_authority_never_creates_repairs_or_adopts(sources, which):
    sample, _, target = sources
    paths = dict(ledger=ledger_path(sample), proposal=companion(sample),
        r3=next((sample.tmp / '.runtime/team-operator/restores').rglob('control.sqlite3')),
        inventory=next((sample.tmp / '.runtime/team-operator/restores').rglob('inventory.json')),
        billing=sample.restored.databases['billing'])
    path = io_path(paths[which])
    path.unlink()
    before = observed_files(sample)
    with pytest.raises(target.FreshEvidenceError):
        with observe(sources): pytest.fail('missing authority admitted')
    assert not path.exists() and observed_files(sample) == before


def test_partial_actual_source_preparations_remain_held(staged, monkeypatch):
    sample, context, _, proof = staged[0]
    writer = staged[1]
    record = writer.SourcePreparationControl.record_preparation
    def crash(self, role, *args, **kwargs):
        if role == 'team': raise RuntimeError('synthetic interrupted preparation')
        return record(self, role, *args, **kwargs)
    monkeypatch.setattr(writer.SourcePreparationControl, 'record_preparation', crash)
    with pytest.raises(RuntimeError): perform(staged)
    monkeypatch.setattr(writer.SourcePreparationControl, 'record_preparation', record)
    target = module()
    monkeypatch.setattr(target, 'protected_scope', context.protected_scope)
    monkeypatch.setattr(target, 'verify_stopped_runtime', lambda **kwargs: dict(proof))
    before = observed_files(sample)
    with pytest.raises(target.FreshEvidenceError, match='fresh_evidence_incomplete'):
        with observe((sample, None, target)): pytest.fail('partial preparation admitted')
    assert observed_files(sample) == before


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_source_byte_drift_and_r3_rollback_are_refused(sources, role):
    sample, _, target = sources
    path = sample.restored.databases[role]
    current = path.read_bytes()
    for replacement in (current + b'synthetic unauthorized bytes', sample.r3_source_bytes[role]):
        path.write_bytes(replacement)
        before = observed_files(sample)
        with pytest.raises(target.FreshEvidenceError):
            with observe(sources): pytest.fail('source drift or rollback admitted')
        assert observed_files(sample) == before


@pytest.mark.parametrize('which', ['team', 'vikunja', 'proposal'])
@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_journals_are_refused_not_deleted_or_ignored(sources, which, suffix):
    sample, _, target = sources
    original = companion(sample) if which == 'proposal' else sample.restored.databases[which]
    journal = io_path(Path(str(original) + suffix))
    journal.write_bytes(b'synthetic active SQLite journal')
    before = observed_files(sample)
    with pytest.raises(target.FreshEvidenceError):
        with observe(sources): pytest.fail('active journal admitted')
    assert journal.read_bytes() == b'synthetic active SQLite journal'
    assert observed_files(sample) == before


@pytest.mark.parametrize('which', ['team', 'vikunja', 'proposal', 'ledger'])
def test_unknown_schemas_are_refused_without_repair(sources, which):
    sample, _, target = sources
    path = {'ledger': ledger_path(sample), 'proposal': companion(sample)}.get(which,
        sample.restored.databases.get(which))
    uri = path.as_uri() + '?mode=rw'
    # Companion can exceed MAX_PATH; use its existing tested private URI helper.
    if which == 'proposal':
        control = target.SourcePreparationControl.open_existing(path)
        uri = control._sqlite_uri(write=True)
    with closing(sqlite3.connect(uri, uri=True)) as conn, conn:
        conn.execute('CREATE TABLE synthetic_unknown_schema(value INTEGER)')
    before = observed_files(sample)
    with pytest.raises(target.FreshEvidenceError):
        with observe(sources): pytest.fail('unknown schema admitted')
    assert observed_files(sample) == before


def test_replaced_same_bytes_source_does_not_rebase_file_identity(sources):
    sample, _, target = sources
    path = sample.restored.databases['team']
    old_id = path.stat().st_ino
    alternate = path.with_suffix('.replacement')
    alternate.write_bytes(path.read_bytes())
    alternate.replace(path)
    assert path.stat().st_ino != old_id
    before = observed_files(sample)
    with pytest.raises(target.FreshEvidenceError):
        with observe(sources): pytest.fail('substituted source admitted')
    assert observed_files(sample) == before


def test_restore_metadata_cannot_redirect_proposal_or_source_paths(sources):
    sample, _, target = sources
    path = sample.restored.destination / 'restore.json'
    document = json.loads(path.read_text())
    document['databases']['team'] = str(sample.restored.databases['billing'])
    path.write_text(json.dumps(document))
    before = observed_files(sample)
    with pytest.raises(target.FreshEvidenceError):
        with observe(sources): pytest.fail('redirected sources admitted')
    assert observed_files(sample) == before


def test_actual_windows_no_write_no_delete_custody_lives_across_yield(sources):
    sample, _, _ = sources
    original = observed_files(sample)
    with observe(sources) as evidence:
        assets = [path for path in (sample.restored.destination / 'assets').rglob('*') if path.is_file()]
        paths = (*sample.restored.databases.values(), ledger_path(sample), companion(sample),
            next((sample.tmp / '.runtime/team-operator/restores').rglob('control.sqlite3')),
            sample.restored.destination / 'restore.json', sample.backup / 'manifest.json',
            sample.tmp / 'original-control.sqlite3', sample.tmp / 'original-lifecycle.json', *assets)
        for path in paths:
            with pytest.raises(OSError):
                with io_path(path).open('r+b') as stream: stream.write(b'unauthorized')
            with pytest.raises(OSError): io_path(path).unlink()
        assert evidence.verify_current() is None
    assert observed_files(sample) == original


def test_late_new_asset_is_detected_on_exit_without_cleanup(sources):
    sample, _, target = sources
    added = sample.restored.destination / 'assets/synthetic-new-file.txt'
    with pytest.raises(target.FreshEvidenceError, match='fresh_evidence_changed'):
        with observe(sources): added.write_text('synthetic new retained artifact')
    assert added.read_text() == 'synthetic new retained artifact'


def test_runtime_is_verified_after_yield_even_when_body_raises(sources, monkeypatch):
    _, _, target = sources
    with observe(sources) as evidence:
        proof = target.verify_stopped_runtime()
    with pytest.raises(target.FreshEvidenceError, match='fresh_evidence_changed'):
        with observe(sources):
            monkeypatch.setattr(target, 'verify_stopped_runtime',
                lambda **kwargs: dict(proof, evidence_sha256='c' * 64))
            raise RuntimeError('synthetic caller failure')


def test_current_caller_error_is_preserved_after_successful_exit_revalidation(sources):
    class CallerError(ValueError): pass
    failure = CallerError('synthetic downstream denial')
    with pytest.raises(CallerError) as caught:
        with observe(sources) as evidence: raise failure
    assert caught.value is failure
    with pytest.raises(sources[2].FreshEvidenceError, match='fresh_evidence_closed'):
        evidence.verify_current()


def test_billing_reader_rejects_temp_alias_before_liability_query(sources, monkeypatch):
    sample, _, target = sources
    original = sqlite3.connect
    def alias(database, *args, **kwargs):
        conn = original(database, *args, **kwargs)
        if 'billing.sqlite3' in str(database):
            conn.execute('CREATE TEMP TABLE billing_charges(operation_id TEXT)')
        return conn
    with observe(sources) as evidence:
        with monkeypatch.context() as scoped:
            scoped.setattr(target.sqlite3, 'connect', alias)
            with pytest.raises(target.FreshEvidenceError): evidence.read_liabilities()
        assert evidence.verify_current() is None


def test_liability_protocol_is_bounded_and_never_releases_unknown_attempts(sources, monkeypatch):
    sample, _, target = sources
    with observe(sources) as evidence:
        before = observed_files(sample)
        monkeypatch.setattr(target, '_MAX_LIABILITIES', 0)
        with pytest.raises(target.FreshEvidenceError, match='fresh_evidence_liabilities_bounded'):
            evidence.read_liabilities()
        assert observed_files(sample) == before
