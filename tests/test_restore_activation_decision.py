"""R4 decision journal: isolated Windows-local synthetic evidence only."""
import importlib
import importlib.util
import inspect
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_restore_source_epoch_writer import copy, setup, prepared, perform, staged
from test_restore_business_owner import SyntheticAdapter, issue, policy_fields, respond

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows custody contract')
NAME = 'secretary.infrastructure.restore_activation_decision'


def implementation():
    assert importlib.util.find_spec(NAME) is not None, 'durable R4 blocked-decision writer is missing'
    return importlib.import_module(NAME)


def test_decision_writer_is_explicit_and_has_no_caller_injected_authority_or_grant():
    target = implementation()
    assert tuple(inspect.signature(target.record_blocked_activation_decision).parameters) == (
        'project_root', 'backup_dir', 'restore_dir', 'maintenance_path', 'lifecycle_path',
        'config_path', 'owner_receipt')
    assert callable(target.current_activation_consent_commitment)
    assert callable(target.ActivationDecisionLedger.open_existing)
    assert not hasattr(target, 'activate_restored_system')
    assert not hasattr(target.ActivationDecisionLedger, 'activate')


@pytest.fixture
def decision_case(staged, monkeypatch):
    prepared, _ = staged
    sample, source_collector, _, runtime_proof = prepared
    source_preparation = perform(staged)
    fresh = importlib.import_module('secretary.infrastructure.restore_fresh_evidence')
    provider = importlib.import_module('secretary.infrastructure.restore_provider_observations')
    target = implementation()
    monkeypatch.setattr(fresh, 'protected_scope', source_collector.protected_scope)
    monkeypatch.setattr(fresh, 'verify_stopped_runtime', lambda **kwargs: dict(runtime_proof))
    monkeypatch.setattr(provider, 'protected_scope', source_collector.protected_scope)

    owner_module = importlib.import_module('secretary.infrastructure.restore_business_owner')
    clock = [datetime.now(timezone.utc)]
    authority = owner_module._test_authority(sample.tmp, adapter=SyntheticAdapter(), clock=lambda: clock[0])
    policy = owner_module.OwnerPolicy(**policy_fields())
    enrollment = []
    authority.enroll(policy, reader=respond(enrollment, 'ENROLL BUSINESS OWNER'), writer=enrollment.append)
    monkeypatch.setattr(target, 'BusinessOwnerAuthority', lambda root: authority)

    config = dict(schema_version=1, deployment_id=source_preparation['binding']['runtime_deployment_id'],
        project_dir=str(sample.tmp), control_database_path=str(sample.tmp / 'new-control.sqlite3'),
        polza_api_key='synthetic-polza-token', max_bot_token='synthetic-max-token', max_bot_id='11',
        restore_blocked=True, outbound_enabled=False)
    config.update({role + '_database_path': str(path) for role, path in sample.restored.databases.items()})
    config_path = sample.tmp / 'synthetic-decision-config.json'
    config_path.write_text(__import__('json').dumps(config), encoding='utf-8')
    arguments = dict(project_root=sample.tmp, backup_dir=sample.backup,
        restore_dir=sample.restored.destination,
        maintenance_path=sample.tmp / 'original-control.sqlite3',
        lifecycle_path=sample.tmp / 'original-lifecycle.json', config_path=config_path)
    return sample, target, authority, clock, arguments


def _receipt(case):
    _, target, authority, _, arguments = case
    commitment = target.current_activation_consent_commitment(**arguments)
    return commitment, issue(authority, commitment)


def _decision_path(sample, commitment):
    return (sample.tmp / '.runtime/team-operator/activations' / commitment['restore_id'] /
        'decisions-v1' / 'decision.sqlite3')


def test_current_owner_nonce_and_blocked_decision_commit_atomically_and_exact_retry_is_idempotent(decision_case):
    sample, target, _, _, arguments = decision_case
    commitment, receipt = _receipt(decision_case)
    result = target.record_blocked_activation_decision(**arguments, owner_receipt=receipt)
    assert result['state'] == 'activation_blocked'
    assert result['activation_supported'] is False and result['outbound_enabled'] is False
    assert {'shared_permission_predicate_missing', 'native_authenticated_get_missing',
            'fresh_execution_lineage_missing', 'provider_observations_missing',
            'billing_liabilities_unresolved'} <= set(result['blockers'])
    path = _decision_path(sample, commitment)
    ledger = target.ActivationDecisionLedger.open_existing(path, target._ledger_binding(commitment))
    first = ledger.snapshot()
    assert len(first['decisions']) == 1 and len(first['used_owner_nonce_hashes']) == 1
    before = path.read_bytes()
    replay = target.record_blocked_activation_decision(**arguments, owner_receipt=receipt)
    after = ledger.snapshot()
    assert replay['decision_id'] == result['decision_id']
    assert len(after['decisions']) == 1 and len(after['used_owner_nonce_hashes']) == 1
    assert path.read_bytes() == before


def test_fresh_owner_decision_after_provider_scope_change_appends_to_same_epoch_ledger(decision_case):
    sample, target, authority, _, arguments = decision_case
    first_commitment, first_receipt = _receipt(decision_case)
    first = target.record_blocked_activation_decision(**arguments, owner_receipt=first_receipt)
    config_path = arguments['config_path']
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['polza_api_key'] = 'synthetic-rotated-polza-token'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    second_commitment = target.current_activation_consent_commitment(**arguments)
    assert second_commitment['restore_id'] == first_commitment['restore_id']
    assert second_commitment['epoch_id'] == first_commitment['epoch_id']
    assert second_commitment != first_commitment
    second_receipt = issue(authority, second_commitment)
    second = target.record_blocked_activation_decision(**arguments, owner_receipt=second_receipt)
    snapshot = target.ActivationDecisionLedger.open_existing(
        _decision_path(sample, first_commitment), target._ledger_binding(second_commitment)).snapshot()
    assert first['decision_id'] != second['decision_id']
    assert len(snapshot['decisions']) == 2 and snapshot['current']['decision_id'] == second['decision_id']
    assert all(record['payload']['state'] == 'activation_blocked' for record in snapshot['decisions'])


def test_expired_owner_receipt_is_rejected_before_decision_store_creation(decision_case):
    sample, target, _, clock, arguments = decision_case
    commitment, receipt = _receipt(decision_case)
    clock[0] = datetime.fromtimestamp(receipt['expires_at'] + 1, timezone.utc)
    with pytest.raises(target.ActivationDecisionError, match='activation_decision_consent_invalid'):
        target.record_blocked_activation_decision(**arguments, owner_receipt=receipt)
    assert not _decision_path(sample, commitment).exists()


def test_changed_source_after_consent_is_rejected_before_decision_store_creation(decision_case):
    sample, target, _, _, arguments = decision_case
    commitment, receipt = _receipt(decision_case)
    source = sample.restored.databases['team']
    original = source.read_bytes()
    source.write_bytes(original + b'synthetic source drift')
    with pytest.raises(target.ActivationDecisionError):
        target.record_blocked_activation_decision(**arguments, owner_receipt=receipt)
    assert not _decision_path(sample, commitment).exists()


def test_provider_freshness_change_during_new_decision_rolls_back_both_rows(decision_case, monkeypatch):
    sample, target, _, _, arguments = decision_case
    commitment, receipt = _receipt(decision_case)
    original = target._fresh_provider_blockers
    calls = 0

    def drift(snapshot, resources, liabilities):
        nonlocal calls
        calls += 1
        blockers = original(snapshot, resources, liabilities)
        if calls == 2:
            return tuple(sorted((*blockers, 'provider_observation_stale')))
        return blockers

    monkeypatch.setattr(target, '_fresh_provider_blockers', drift)
    with pytest.raises(target.ActivationDecisionError, match='activation_decision_source_changed'):
        target.record_blocked_activation_decision(**arguments, owner_receipt=receipt)
    assert calls == 2
    snapshot = target.ActivationDecisionLedger.open_existing(
        _decision_path(sample, commitment), target._ledger_binding(commitment)).snapshot()
    assert snapshot['decisions'] == () and snapshot['used_owner_nonce_hashes'] == ()


@pytest.mark.parametrize('change', ['signature', 'commitment_scope'])
def test_invalid_or_wrong_scope_owner_receipt_is_rejected_without_store(decision_case, change):
    sample, target, authority, _, arguments = decision_case
    commitment, receipt = _receipt(decision_case)
    if change == 'signature':
        receipt = {**receipt, 'signature': '0' * 64}
    else:
        wrong = {**commitment, 'resource_scope_sha256': 'e' * 64}
        receipt = issue(authority, wrong)
    with pytest.raises(target.ActivationDecisionError, match='activation_decision_consent_invalid'):
        target.record_blocked_activation_decision(**arguments, owner_receipt=receipt)
    assert not _decision_path(sample, commitment).exists()


def test_interruption_between_decision_and_nonce_inserts_rolls_back_both(tmp_path):
    target = implementation()
    from secretary.infrastructure.restore_operator import create_private_directory
    from secretary.infrastructure.restore_business_owner import VerifiedBusinessOwnerConsent
    folder = tmp_path / 'private-decision-store'
    create_private_directory(folder)
    bound = dict(protocol=1, purpose='restore_activation_consent',
        restore_id='11111111-1111-4111-8111-111111111111',
        epoch_id='22222222-2222-4222-8222-222222222222',
        preparation_id='33333333-3333-4333-8333-333333333333',
        source_snapshot_sha256='a' * 64, binding_sha256='b' * 64,
        scope_sha256='c' * 64, provider_observations_sha256='d' * 64,
        resource_scope_sha256='e' * 64, capabilities=['fresh_work'], owner_policy_sha256='f' * 64)
    path = folder / 'decision.sqlite3'
    ledger_binding = target._ledger_binding(bound)
    ledger = target.ActivationDecisionLedger.create(path, ledger_binding)
    consent = VerifiedBusinessOwnerConsent('1' * 64, 'f' * 64, '2' * 64, '3' * 64,
        int(time.time()) + 300)
    original_validate, calls = ledger._validate, 0
    def interrupt(conn):
        nonlocal calls
        value = original_validate(conn)
        calls += 1
        if calls == 3:
            raise RuntimeError('synthetic interruption before commit')
        return value
    blockers = tuple(sorted(target._FIXED_BLOCKERS))
    ledger._validate = interrupt
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        ledger._append_blocked(consent, bound, blockers, precommit=lambda replay: None)
    reopened = target.ActivationDecisionLedger.open_existing(path, ledger_binding).snapshot()
    assert reopened['decisions'] == () and reopened['used_owner_nonce_hashes'] == ()
    assert reopened['activation_supported'] is False and reopened['outbound_enabled'] is False


def test_concurrent_exact_nonce_replay_appends_only_one_decision(tmp_path):
    target = implementation()
    from secretary.infrastructure.restore_operator import create_private_directory
    from secretary.infrastructure.restore_business_owner import VerifiedBusinessOwnerConsent
    folder = tmp_path / 'private-concurrent-decision-store'
    create_private_directory(folder)
    bound = dict(protocol=1, purpose='restore_activation_consent',
        restore_id='11111111-1111-4111-8111-111111111111',
        epoch_id='22222222-2222-4222-8222-222222222222',
        preparation_id='33333333-3333-4333-8333-333333333333',
        source_snapshot_sha256='a' * 64, binding_sha256='b' * 64,
        scope_sha256='c' * 64, provider_observations_sha256='d' * 64,
        resource_scope_sha256='e' * 64, capabilities=['fresh_work'], owner_policy_sha256='f' * 64)
    path = folder / 'decision.sqlite3'
    ledger_binding = target._ledger_binding(bound)
    ledger = target.ActivationDecisionLedger.create(path, ledger_binding)
    consent = VerifiedBusinessOwnerConsent('1' * 64, 'f' * 64, '2' * 64, '3' * 64,
        int(time.time()) + 300)
    blockers = tuple(sorted(target._FIXED_BLOCKERS))
    def append():
        return ledger._append_blocked(consent, bound, blockers, precommit=lambda replay: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: append(), range(2)))
    assert results[0] == results[1]
    snapshot = target.ActivationDecisionLedger.open_existing(path, ledger_binding).snapshot()
    assert len(snapshot['decisions']) == 1 and len(snapshot['used_owner_nonce_hashes']) == 1


def test_same_owner_nonce_replay_after_provider_expiry_returns_original_blocked_decision(tmp_path):
    target = implementation()
    from secretary.infrastructure.restore_operator import create_private_directory
    from secretary.infrastructure.restore_business_owner import VerifiedBusinessOwnerConsent
    folder = tmp_path / 'private-provider-expiry-replay-store'
    create_private_directory(folder)
    bound = dict(protocol=1, purpose='restore_activation_consent',
        restore_id='11111111-1111-4111-8111-111111111111',
        epoch_id='22222222-2222-4222-8222-222222222222',
        preparation_id='33333333-3333-4333-8333-333333333333',
        source_snapshot_sha256='a' * 64, binding_sha256='b' * 64,
        scope_sha256='c' * 64, provider_observations_sha256='d' * 64,
        resource_scope_sha256='e' * 64, capabilities=['fresh_work'], owner_policy_sha256='f' * 64)
    path = folder / 'decision.sqlite3'
    ledger_binding = target._ledger_binding(bound)
    ledger = target.ActivationDecisionLedger.create(path, ledger_binding)
    consent = VerifiedBusinessOwnerConsent('1' * 64, 'f' * 64, '2' * 64, '3' * 64,
        int(time.time()) + 300)
    blockers = tuple(sorted(target._FIXED_BLOCKERS))
    original = ledger._append_blocked(consent, bound, blockers, precommit=lambda replay: None)
    replay = ledger._append_blocked(consent, bound,
        tuple(sorted((*blockers, 'provider_observation_stale'))), precommit=lambda is_replay: None)
    snapshot = ledger.snapshot()
    assert replay == original
    assert len(snapshot['decisions']) == 1 and snapshot['current'] == original
    assert len(snapshot['used_owner_nonce_hashes']) == 1


def test_same_owner_nonce_with_changed_commitment_conflicts_without_second_record(tmp_path):
    target = implementation()
    from secretary.infrastructure.restore_operator import create_private_directory
    from secretary.infrastructure.restore_business_owner import VerifiedBusinessOwnerConsent
    folder = tmp_path / 'private-conflicting-commitment-store'
    create_private_directory(folder)
    bound = dict(protocol=1, purpose='restore_activation_consent',
        restore_id='11111111-1111-4111-8111-111111111111',
        epoch_id='22222222-2222-4222-8222-222222222222',
        preparation_id='33333333-3333-4333-8333-333333333333',
        source_snapshot_sha256='a' * 64, binding_sha256='b' * 64,
        scope_sha256='c' * 64, provider_observations_sha256='d' * 64,
        resource_scope_sha256='e' * 64, capabilities=['fresh_work'], owner_policy_sha256='f' * 64)
    path = folder / 'decision.sqlite3'
    ledger_binding = target._ledger_binding(bound)
    ledger = target.ActivationDecisionLedger.create(path, ledger_binding)
    consent = VerifiedBusinessOwnerConsent('1' * 64, 'f' * 64, '2' * 64, '3' * 64,
        int(time.time()) + 300)
    blockers = tuple(sorted(target._FIXED_BLOCKERS))
    original = ledger._append_blocked(consent, bound, blockers, precommit=lambda replay: None)
    changed_commitment = {**bound, 'provider_observations_sha256': '9' * 64}
    with pytest.raises(target.ActivationDecisionError, match='activation_decision_consent_conflict'):
        ledger._append_blocked(consent, changed_commitment, blockers,
            precommit=lambda replay: None)
    snapshot = ledger.snapshot()
    assert len(snapshot['decisions']) == 1 and snapshot['current'] == original
    assert len(snapshot['used_owner_nonce_hashes']) == 1


def test_provider_freshness_and_synthetic_status_never_remove_fixed_blocks():
    target = implementation()
    resources = dict(polza_key_sha256='a' * 64, max_token_sha256=None, max_bot_id=None)
    issued = datetime.now(timezone.utc)
    from secretary.domain.restore_quarantine import digest
    payload = dict(provider='polza', operation='polza_key_usage', credential_sha256='a' * 64,
        request_sha256=digest(['polza', 'polza_key_usage', None]),
        resource_sha256=digest([resources, None, None]), state='observed', error_code=None,
        expires_at=(issued.replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%S.%fZ')),
        evidence_kind='synthetic_get')
    snapshot = dict(state='provider_observations_blocked', observations=({'payload': payload},))
    blockers = set(target._fresh_provider_blockers(snapshot, resources, ()))
    assert 'provider_observation_synthetic' in blockers
    assert 'provider_observation_stale' in blockers
    assert target._FIXED_BLOCKERS <= blockers


def test_provider_observation_missing_expected_get_remains_incomplete():
    target = implementation()
    resources = dict(polza_key_sha256='a' * 64, max_token_sha256=None, max_bot_id=None)
    snapshot = dict(state='provider_observations_blocked', observations=())
    blockers = set(target._fresh_provider_blockers(snapshot, resources, ()))
    assert 'provider_observation_incomplete' in blockers
    assert target._FIXED_BLOCKERS <= blockers


def test_corrupt_schema_and_journal_sidecar_are_refused_without_repair(tmp_path):
    target = implementation()
    from secretary.infrastructure.restore_operator import create_private_directory
    folder = tmp_path / 'private-decision-schema'
    create_private_directory(folder)
    bound = dict(protocol=1, purpose='restore_activation_consent',
        restore_id='11111111-1111-4111-8111-111111111111',
        epoch_id='22222222-2222-4222-8222-222222222222',
        preparation_id='33333333-3333-4333-8333-333333333333',
        source_snapshot_sha256='a' * 64, binding_sha256='b' * 64,
        scope_sha256='c' * 64, provider_observations_sha256='d' * 64,
        resource_scope_sha256='e' * 64, capabilities=['fresh_work'], owner_policy_sha256='f' * 64)
    path = folder / 'decision.sqlite3'
    ledger_binding = target._ledger_binding(bound)
    target.ActivationDecisionLedger.create(path, ledger_binding)
    sidecar = Path(str(path) + '-journal')
    sidecar.write_bytes(b'synthetic incomplete journal')
    before = path.read_bytes()
    with pytest.raises(target.ActivationDecisionError):
        target.ActivationDecisionLedger.open_existing(path, ledger_binding)
    assert sidecar.read_bytes() == b'synthetic incomplete journal' and path.read_bytes() == before
