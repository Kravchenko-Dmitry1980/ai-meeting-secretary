"""Durable blocked R4 staging from actual R3 copies, with real private ledger."""
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json

import pytest

from test_team_restore_preview import copy, files
from test_restore_reconciliation import setup
from test_restore_activation_context import prepared, read, observed_files


def module():
    name = 'secretary.infrastructure.restore_activation_staging'
    assert importlib.util.find_spec(name), 'R4 actual baseline staging is missing'
    return importlib.import_module(name)


def stage(prepared, capabilities=('fresh_work', 'sql_write')):
    sample, _, _, _ = prepared
    return module().prepare_activation_epoch(sample.tmp, sample.backup, sample.restored.destination,
        maintenance_path=sample.tmp / 'original-control.sqlite3',
        lifecycle_path=sample.tmp / 'original-lifecycle.json', capabilities=capabilities)


def ledger_path(sample):
    return sample.tmp / '.runtime/team-operator/activations' / sample.restored.restore_id / 'activation.sqlite3'


def test_actual_four_source_baseline_is_recorded_under_an_independent_blocked_epoch(prepared):
    sample, _, _, _ = prepared
    context = read(prepared)
    original = files(sample.restored.destination), files(sample.backup)
    result = stage(prepared)
    assert result['state'] == 'epoch_prepared_blocked'
    assert result['outbound_enabled'] is False and result['activation_supported'] is False
    assert result['binding'] == context.ledger_binding
    assert set(result['sources']) == {'secretary', 'team', 'billing', 'vikunja'}
    assert result['missing_source_roles'] == ()
    for source in context.sources:
        record = result['sources'][source.role]
        assert record['source_commitment_sha256'] == source.source_commitment_sha256
        assert record['baseline_file_sha256'] == source.baseline_file_sha256
        assert record['preparation_sha256'] == source.preparation_sha256
    assert ledger_path(sample).is_file()
    assert (files(sample.restored.destination), files(sample.backup)) == original


def test_repeat_keeps_same_epoch_generation_and_file_bytes(prepared):
    sample, _, _, _ = prepared
    first = stage(prepared)
    before = ledger_path(sample).read_bytes()
    assert stage(prepared) == first
    assert ledger_path(sample).read_bytes() == before


def test_crash_after_two_source_commits_resumes_same_blocked_epoch(prepared, monkeypatch):
    sample, _, _, _ = prepared
    from secretary.infrastructure.restore_activation_ledger import RestoreActivationLedger
    original = RestoreActivationLedger.record_source
    def crash(self, epoch, role, *args):
        original(self, epoch, role, *args)
        if role == 'team': raise RuntimeError('synthetic crash after durable second source')
    monkeypatch.setattr(RestoreActivationLedger, 'record_source', crash)
    with pytest.raises(RuntimeError): stage(prepared)
    context = read(prepared)
    partial = RestoreActivationLedger.open_existing(ledger_path(sample), context.ledger_binding).snapshot()
    assert set(partial['sources']) == {'secretary', 'team'}
    assert partial['outbound_enabled'] is False
    monkeypatch.setattr(RestoreActivationLedger, 'record_source', original)
    resumed = stage(prepared)
    assert resumed['epoch'] == partial['epoch']
    assert resumed['generation_hold'] == partial['generation_hold']
    assert set(resumed['sources']) == {'secretary', 'team', 'billing', 'vikunja'}


def test_changed_source_refuses_staging_before_ledger_creation(prepared):
    sample, target, _, _ = prepared
    sample.restored.databases['vikunja'].write_bytes(b'changed synthetic native baseline')
    before = observed_files(sample)
    with pytest.raises(target.ActivationContextError): stage(prepared)
    assert not ledger_path(sample).exists()
    assert observed_files(sample) == before
    assert not (sample.tmp / '.runtime/team-operator/activations').exists()


def test_changed_existing_ledger_is_refused_without_repair(prepared):
    sample, _, _, _ = prepared
    stage(prepared)
    path = ledger_path(sample)
    path.write_bytes(b'corrupt synthetic activation ledger')
    before = path.read_bytes()
    from secretary.infrastructure.restore_activation_ledger import ActivationLedgerError
    with pytest.raises(ActivationLedgerError): stage(prepared)
    assert path.read_bytes() == before


def test_lost_ledger_in_existing_restore_generation_is_never_silently_recreated(prepared):
    sample, _, _, _ = prepared
    stage(prepared)
    path = ledger_path(sample)
    path.unlink()
    from secretary.infrastructure.restore_activation_ledger import ActivationLedgerError
    with pytest.raises(ActivationLedgerError): stage(prepared)
    assert not path.exists()


def test_partial_initialization_directory_without_ledger_stays_blocked(prepared):
    sample, _, _, _ = prepared
    from secretary.infrastructure.restore_operator import create_private_directory
    path = ledger_path(sample)
    create_private_directory(path.parent.parent)
    create_private_directory(path.parent)
    from secretary.infrastructure.restore_activation_ledger import ActivationLedgerError
    with pytest.raises(ActivationLedgerError): stage(prepared)
    assert not path.exists()


def test_other_initializer_creating_directory_before_custody_never_grants_creation(prepared, monkeypatch):
    sample, _, _, _ = prepared
    target = module()
    path = ledger_path(sample)
    from secretary.infrastructure.restore_operator import create_private_directory
    from secretary.infrastructure.restore_activation_ledger import ActivationLedgerError
    create_private_directory(path.parent.parent)
    original = target._staging_scope
    @contextmanager
    def interleaved(directory):
        with original(directory):
            if directory == path.parent.parent:
                # Actual competing mkdir then crash at the exact initialization
                # gap. No permission flag or prebuilt collector DTO is forged.
                create_private_directory(path.parent)
            yield
    monkeypatch.setattr(target, '_staging_scope', interleaved)
    with pytest.raises(ActivationLedgerError): stage(prepared)
    assert path.parent.is_dir() and not path.exists()


@pytest.mark.parametrize('capabilities', [('max_raw_intake',), ('max_subscription_reconcile',),
    ['fresh_work'], ('sql_write', 'fresh_work'), ()])
def test_invalid_or_separately_disabled_capabilities_fail_before_any_staging_write(prepared, capabilities):
    sample, _, _, _ = prepared
    before = observed_files(sample)
    from secretary.infrastructure.restore_activation_ledger import ActivationLedgerError
    with pytest.raises(ActivationLedgerError): stage(prepared, capabilities)
    assert not ledger_path(sample).exists()
    assert observed_files(sample) == before
    assert not (sample.tmp / '.runtime/team-operator/activations').exists()


def test_changed_baseline_after_recording_is_refused_and_stays_blocked(prepared, monkeypatch):
    sample, target, _, _ = prepared
    from secretary.infrastructure.restore_activation_ledger import RestoreActivationLedger
    original = RestoreActivationLedger.record_source
    def change(self, epoch, role, *args):
        original(self, epoch, role, *args)
        if role == 'vikunja':
            path = sample.restored.destination / 'restore.json'
            document = json.loads(path.read_text())
            document['outbound_enabled'] = True
            path.write_text(json.dumps(document))
    monkeypatch.setattr(RestoreActivationLedger, 'record_source', change)
    with pytest.raises(target.ActivationContextError): stage(prepared)
    # The old diagnostic context is used only to read a blocked ledger, never
    # as permission. A fake changed metadata flag cannot enable anything.
    path = ledger_path(sample)
    with __import__('sqlite3').connect(path) as conn:
        assert conn.execute('SELECT state FROM activation_epoch').fetchall() == [('epoch_prepared_blocked',)]
        assert conn.execute('SELECT count(*) FROM activation_sources').fetchone() == (4,)
