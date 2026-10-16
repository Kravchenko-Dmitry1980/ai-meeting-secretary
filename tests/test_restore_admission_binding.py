"""Pure, non-authorizing v1 R4 generation and operation binding contracts."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest


def module():
    from secretary.infrastructure import restore_admission_binding
    return restore_admission_binding


def h(value):
    import hashlib
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def generation():
    m = module()
    authorities = tuple(m.SourceAuthorityV1(
        role=role,
        source_path_sha256=h(role + ':path'),
        file_identity_sha256=h(role + ':file-id'),
        baseline_file_sha256=h(role + ':baseline'),
        r3_preparation_sha256=h(role + ':r3-preparation'),
        r4_preparation_sha256=h(role + ':r4-preparation'),
        readback_sha256=h(role + ':readback'),
    ) for role in m.SOURCE_ROLES)
    return m.SourceGenerationV1(
        deployment_id=str(uuid4()),
        restore_id=str(uuid4()),
        r3_id=str(uuid4()),
        r3_decision_sha256=h('r3-decision'),
        r3_binding_sha256=h('r3-binding'),
        epoch_id=str(uuid4()),
        manifest_sha256=h('manifest'),
        metadata_sha256=h('metadata'),
        inventory_sha256=h('inventory'),
        scope_sha256=h('scope'),
        configuration_versions_sha256=h('configuration-versions'),
        sources=authorities,
        native_schema_sha256=h('native-schema'),
        native_head_sequence=1,
        native_head_sha256=h('native-head'),
        native_authentication_sha256=h('authenticated-readback'),
    )


def fence_lineage(deployment_id=None, **changes):
    m = module()
    fields = dict(
        deployment_id=deployment_id or str(uuid4()),
        archive_barrier_id=str(uuid4()),
        archive_barrier_fence=5,
        archive_event_count=12,
        archive_events_sha256=h('archived maintenance events'),
        observed_high_water_fence=7,
        observed_event_count=16,
        observed_events_sha256=h('current maintenance events'),
        stopped_runtime_evidence_sha256=h('stopped runtime evidence'),
    )
    fields.update(changes)
    return m.MaintenanceFenceLineageV1(**fields)


def generation_with_fence_lineage():
    m = module()
    lineage = fence_lineage()
    base = replace(generation(), deployment_id=lineage.deployment_id)
    return m.SourceGenerationV2(base_generation=base, maintenance_fence_lineage=lineage)


def test_r5_successor_candidate_uses_captured_lineage_and_is_non_authorizing():
    m = module()
    lineage = fence_lineage()
    candidate = m.R5SuccessorFenceCandidateV1.from_lineage(lineage)
    assert candidate.expected_successor_fence == lineage.observed_high_water_fence + 1
    assert candidate.activation_supported is False
    assert candidate.outbound_enabled is False
    assert not hasattr(candidate, 'permit')


def test_r5_successor_candidate_record_round_trips_and_rejects_missing_stale_or_conflicting_data():
    m = module()
    lineage = fence_lineage()
    candidate = m.R5SuccessorFenceCandidateV1.from_lineage(lineage)
    record = candidate.to_prepared_blocked_record()
    encoded = json.dumps(record, sort_keys=True, separators=(',', ':'))
    resumed = m.R5SuccessorFenceCandidateV1.from_prepared_blocked_record(
        json.loads(encoded), maintenance_fence_lineage=lineage)
    assert resumed.candidate_sha256 == candidate.candidate_sha256

    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.R5SuccessorFenceCandidateV1.from_prepared_blocked_record(
            None, maintenance_fence_lineage=lineage)
    stale = replace(lineage, observed_high_water_fence=lineage.observed_high_water_fence + 1)
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.R5SuccessorFenceCandidateV1.from_prepared_blocked_record(
            record, maintenance_fence_lineage=stale)
    conflict = dict(record, expected_successor_fence=record['expected_successor_fence'] + 1)
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.R5SuccessorFenceCandidateV1.from_prepared_blocked_record(
            conflict, maintenance_fence_lineage=lineage)


def test_r5_successor_candidate_fails_closed_at_counter_limit():
    m = module()
    terminal = replace(fence_lineage(), observed_high_water_fence=2**63 - 1)
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.R5SuccessorFenceCandidateV1.from_lineage(terminal)


def test_source_generation_digest_is_stable_and_versioned():
    original = generation()
    assert original.source_generation_sha256 == replace(original).source_generation_sha256
    assert len(original.source_generation_sha256) == 64
    assert original.protocol == 1
    assert original.activation_supported is False
    assert original.outbound_enabled is False


def test_r5_lineage_is_committed_to_source_generation_v2_without_authority():
    original = generation_with_fence_lineage()
    assert original.source_generation_sha256 == replace(original).source_generation_sha256
    assert original.protocol == 2
    assert original.maintenance_fence_lineage.observed_high_water_fence == 7
    assert original.activation_supported is False
    assert original.outbound_enabled is False


def test_successor_fence_candidate_uses_only_observed_high_water_and_is_non_authorizing():
    m = module()
    source_generation = generation_with_fence_lineage()
    candidate = m.SuccessorFenceCandidateV1.from_source_generation(source_generation)
    recomputed = m.SuccessorFenceCandidateV1.from_source_generation(source_generation)

    assert candidate.expected_successor_fence == source_generation.maintenance_fence_lineage.observed_high_water_fence + 1
    assert candidate.candidate_sha256 == recomputed.candidate_sha256
    assert candidate.activation_supported is False
    assert candidate.outbound_enabled is False
    assert not hasattr(candidate, 'permit')


@pytest.mark.parametrize('source_generation', (None,))
def test_successor_fence_candidate_rejects_missing_lineage(source_generation):
    m = module()
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.SuccessorFenceCandidateV1.from_source_generation(source_generation)


def test_successor_fence_candidate_rejects_v1_generation_without_r5_lineage():
    m = module()
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.SuccessorFenceCandidateV1.from_source_generation(generation())


@pytest.mark.parametrize('candidate_fence', (6, 9))
def test_successor_fence_candidate_rejects_stale_or_conflicting_fence(candidate_fence):
    m = module()
    source_generation = generation_with_fence_lineage()
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.SuccessorFenceCandidateV1(source_generation=source_generation,
            expected_successor_fence=candidate_fence)


def test_successor_fence_candidate_digest_tracks_new_source_generation():
    m = module()
    original = m.SuccessorFenceCandidateV1.from_source_generation(generation_with_fence_lineage())
    changed_generation = replace(original.source_generation,
        base_generation=replace(original.source_generation.base_generation,
            native_head_sequence=original.source_generation.base_generation.native_head_sequence + 1))
    changed = m.SuccessorFenceCandidateV1.from_source_generation(changed_generation)
    assert changed.candidate_sha256 != original.candidate_sha256


def test_successor_fence_candidate_fails_closed_when_counter_cannot_advance():
    m = module()
    source_generation = generation_with_fence_lineage()
    lineage = replace(source_generation.maintenance_fence_lineage,
        observed_high_water_fence=2**63 - 1)
    terminal = replace(source_generation, maintenance_fence_lineage=lineage)
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.SuccessorFenceCandidateV1.from_source_generation(terminal)


def test_successor_fence_prepared_blocked_record_round_trips_after_restart():
    import json
    m = module()
    source_generation = generation_with_fence_lineage()
    candidate = m.SuccessorFenceCandidateV1.from_source_generation(source_generation)
    serialized = json.dumps(candidate.to_prepared_blocked_record(), sort_keys=True, separators=(',', ':'))
    reloaded = m.SuccessorFenceCandidateV1.from_prepared_blocked_record(
        json.loads(serialized), source_generation=source_generation)

    assert reloaded.candidate_sha256 == candidate.candidate_sha256
    assert reloaded.to_prepared_blocked_record() == candidate.to_prepared_blocked_record()
    assert reloaded.activation_supported is False
    assert reloaded.outbound_enabled is False


def test_successor_fence_prepared_blocked_record_rejects_missing_record():
    m = module()
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.SuccessorFenceCandidateV1.from_prepared_blocked_record(
            None, source_generation=generation_with_fence_lineage())


def test_successor_fence_prepared_blocked_record_rejects_stale_source_generation():
    m = module()
    original = generation_with_fence_lineage()
    candidate = m.SuccessorFenceCandidateV1.from_source_generation(original)
    changed = replace(original,
        base_generation=replace(original.base_generation,
            native_head_sequence=original.base_generation.native_head_sequence + 1))
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.SuccessorFenceCandidateV1.from_prepared_blocked_record(
            candidate.to_prepared_blocked_record(), source_generation=changed)


@pytest.mark.parametrize('field,value', (
    ('expected_successor_fence', 9),
    ('state', 'active'),
    ('candidate_sha256', h('conflicting candidate')),
    ('unexpected', 'field'),
))
def test_successor_fence_prepared_blocked_record_rejects_conflicts(field, value):
    m = module()
    candidate = m.SuccessorFenceCandidateV1.from_source_generation(generation_with_fence_lineage())
    record = candidate.to_prepared_blocked_record()
    record[field] = value
    with pytest.raises(m.SuccessorFenceCandidateError, match='successor_fence_candidate_invalid'):
        m.SuccessorFenceCandidateV1.from_prepared_blocked_record(
            record, source_generation=candidate.source_generation)


@pytest.mark.parametrize('field,value', (
    ('archive_barrier_fence', 6),
    ('archive_events_sha256', h('changed archived events')),
    ('observed_high_water_fence', 8),
    ('observed_event_count', 17),
    ('observed_events_sha256', h('changed current events')),
    ('stopped_runtime_evidence_sha256', h('changed stopped evidence')),
))
def test_any_r5_fence_lineage_change_rekeys_the_source_generation(field, value):
    m = module()
    original = generation_with_fence_lineage()
    lineage = replace(original.maintenance_fence_lineage, **{field: value})
    changed = replace(original, maintenance_fence_lineage=lineage)
    assert changed.source_generation_sha256 != original.source_generation_sha256


@pytest.mark.parametrize('change', ('lower_high_water', 'shorter_observed_log', 'foreign_deployment', 'boolean_fence'))
def test_r5_fence_lineage_rejects_stale_or_mismatched_handoff(change):
    m = module()
    original = generation_with_fence_lineage()
    lineage = original.maintenance_fence_lineage
    if change == 'lower_high_water':
        with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
            replace(lineage, observed_high_water_fence=lineage.archive_barrier_fence - 1)
    elif change == 'shorter_observed_log':
        with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
            replace(lineage, observed_event_count=lineage.archive_event_count - 1)
    elif change == 'foreign_deployment':
        with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
            replace(original, maintenance_fence_lineage=replace(lineage, deployment_id=str(uuid4())))
    else:
        with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
            replace(lineage, observed_high_water_fence=True)


@pytest.mark.parametrize('field', (
    'r3_decision_sha256', 'r3_binding_sha256', 'manifest_sha256',
    'metadata_sha256', 'inventory_sha256', 'scope_sha256',
    'configuration_versions_sha256', 'native_schema_sha256', 'native_head_sha256',
    'native_authentication_sha256',
))
def test_each_top_level_source_generation_input_changes_commitment(field):
    original = generation()
    changed = replace(original, **{field: h(field + ':changed')})
    assert changed.source_generation_sha256 != original.source_generation_sha256


def test_native_advancing_head_is_part_of_source_generation():
    original = generation()
    changed = replace(original, native_head_sequence=original.native_head_sequence + 1)
    assert changed.source_generation_sha256 != original.source_generation_sha256


@pytest.mark.parametrize('field', (
    'source_path_sha256', 'file_identity_sha256', 'baseline_file_sha256',
    'r3_preparation_sha256', 'r4_preparation_sha256', 'readback_sha256',
))
def test_each_authority_identity_and_preparation_changes_commitment(field):
    original = generation()
    source = replace(original.sources[0], **{field: h(field + ':changed')})
    changed = replace(original, sources=(source, *original.sources[1:]))
    assert changed.source_generation_sha256 != original.source_generation_sha256


def test_generation_requires_exact_ordered_four_authorities():
    m = module()
    original = generation()
    with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
        replace(original, sources=tuple(reversed(original.sources)))
    with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
        replace(original, sources=original.sources[:-1])
    with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
        replace(original, sources=(*original.sources[:-1], original.sources[0]))


def test_generation_rejects_unadvanced_or_noncanonical_evidence():
    m = module()
    original = generation()
    with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
        replace(original, native_head_sequence=0)
    with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
        replace(original, protocol=2)
    with pytest.raises(m.SourceGenerationError, match='source_generation_invalid'):
        replace(original, manifest_sha256='A' * 64)


def test_operation_binding_commits_current_generation_and_exact_operation_scope():
    m = module()
    source_generation = generation()
    binding = m.R4AdmissionBindingV1(
        deployment_id=source_generation.deployment_id,
        restore_id=source_generation.restore_id,
        r3_id=source_generation.r3_id,
        epoch_id=source_generation.epoch_id,
        source_generation_sha256=source_generation.source_generation_sha256,
        decision_id=str(uuid4()),
        decision_sha256=h('current blocked decision'),
        action='sql_write',
        participant_id='synthetic-worker',
        run_id=str(uuid4()),
        operation_id=str(uuid4()),
        barrier_fence=7,
    )
    assert len(binding.binding_sha256) == 64
    assert binding.protocol == 1
    assert binding.activation_supported is False
    assert binding.outbound_enabled is False
    assert not hasattr(binding, 'permit')

    changed_operation = replace(binding, operation_id=str(uuid4()))
    changed_fence = replace(binding, barrier_fence=binding.barrier_fence + 1)
    changed_generation = replace(binding, source_generation_sha256=h('stale generation'))
    assert changed_operation.binding_sha256 != binding.binding_sha256
    assert changed_fence.binding_sha256 != binding.binding_sha256
    assert changed_generation.binding_sha256 != binding.binding_sha256


@pytest.mark.parametrize('field,value', (
    ('action', 'activation_grant'),
    ('participant_id', '../synthetic'),
    ('barrier_fence', 0),
    ('decision_sha256', 'z' * 64),
))
def test_operation_binding_rejects_invalid_action_or_scope(field, value):
    m = module()
    source_generation = generation()
    values = dict(
        deployment_id=source_generation.deployment_id,
        restore_id=source_generation.restore_id,
        r3_id=source_generation.r3_id,
        epoch_id=source_generation.epoch_id,
        source_generation_sha256=source_generation.source_generation_sha256,
        decision_id=str(uuid4()),
        decision_sha256=h('decision'),
        action='sql_write',
        participant_id='synthetic-worker',
        run_id=str(uuid4()),
        operation_id=str(uuid4()),
        barrier_fence=7,
    )
    values[field] = value
    with pytest.raises(m.AdmissionBindingError, match='admission_binding_invalid'):
        m.R4AdmissionBindingV1(**values)


def test_binding_protocol_exposes_only_opaque_scope_not_raw_proof_or_payload():
    from dataclasses import fields
    m = module()
    names = {item.name for item in fields(m.R4AdmissionBindingV1)}
    assert names == {
        'protocol', 'deployment_id', 'restore_id', 'r3_id', 'epoch_id',
        'source_generation_sha256', 'decision_id', 'decision_sha256', 'action',
        'participant_id', 'run_id', 'operation_id', 'barrier_fence',
    }
    assert not any('token' in name or 'payload' in name or 'transcript' in name for name in names)


def admission_binding_for(source_generation, **changes):
    m = module()
    base_generation = (source_generation.base_generation
        if type(source_generation) is m.SourceGenerationV2 else source_generation)
    values = dict(
        deployment_id=base_generation.deployment_id,
        restore_id=base_generation.restore_id,
        r3_id=base_generation.r3_id,
        epoch_id=base_generation.epoch_id,
        source_generation_sha256=source_generation.source_generation_sha256,
        decision_id=str(uuid4()),
        decision_sha256=h('blocked decision'),
        action='max_dispatch',
        participant_id='synthetic-worker',
        run_id=str(uuid4()),
        operation_id=str(uuid4()),
        barrier_fence=7,
    )
    values.update(changes)
    return m.R4AdmissionBindingV1(**values)


def versioned_ticket(source_generation, *, ticket_id=None, kind='outbound_bot',
                     admission_binding=None, **changes):
    binding = admission_binding or admission_binding_for(source_generation)
    values = dict(
        ticket_id=ticket_id or str(uuid4()),
        deployment_id=binding.deployment_id,
        participant_id=binding.participant_id,
        run_id=binding.run_id,
        kind=kind,
        operation_id=binding.operation_id,
        fence=binding.barrier_fence,
        admission_binding=binding,
        source_generation=source_generation,
    )
    values.update(changes)
    return module().MaintenanceTicketV1.from_source_generation(**values)


def test_versioned_ticket_round_trips_only_for_the_same_generation():
    m = module()
    source_generation = generation()
    ticket = versioned_ticket(source_generation)
    record = ticket.to_record()
    encoded = json.dumps(record, sort_keys=True, separators=(',', ':'))
    resumed = m.MaintenanceTicketV1.from_record(
        json.loads(encoded), source_generation=source_generation)

    assert resumed == ticket
    assert resumed.ticket_sha256 == ticket.ticket_sha256
    assert resumed.activation_supported is False
    assert resumed.outbound_enabled is False
    assert not hasattr(resumed, 'permit')


def test_versioned_ticket_round_trips_r5_generation_and_rejects_newer_fence_lineage():
    m = module()
    source_generation = generation_with_fence_lineage()
    ticket = versioned_ticket(source_generation)
    record = ticket.to_record()
    resumed = m.MaintenanceTicketV1.from_record(record, source_generation=source_generation)
    assert resumed == ticket

    stale = replace(source_generation,
        maintenance_fence_lineage=replace(source_generation.maintenance_fence_lineage,
            observed_high_water_fence=source_generation.maintenance_fence_lineage.observed_high_water_fence + 1))
    with pytest.raises(m.AdmissionBindingError, match='admission_binding_invalid'):
        m.MaintenanceTicketV1.from_record(record, source_generation=stale)


@pytest.mark.parametrize('damage', ('missing_binding', 'legacy_unbound', 'stale_generation', 'tampered_ticket'))
def test_versioned_ticket_refuses_missing_stale_or_unbound_records(damage):
    m = module()
    source_generation = generation()
    ticket = versioned_ticket(source_generation)
    record = ticket.to_record()
    generation_for_read = source_generation
    if damage == 'missing_binding':
        del record['admission_binding']
    elif damage == 'legacy_unbound':
        record = {
            'id': ticket.ticket_id,
            'participant_id': ticket.admission_binding.participant_id,
            'run_id': ticket.admission_binding.run_id,
            'kind': ticket.kind,
            'operation_id': ticket.admission_binding.operation_id,
            'fence': ticket.fence,
        }
    elif damage == 'stale_generation':
        changed_base = replace(source_generation, manifest_sha256=h('changed manifest'))
        generation_for_read = changed_base
    else:
        record['ticket_id'] = str(uuid4())

    with pytest.raises(m.AdmissionBindingError, match='admission_binding_invalid'):
        m.MaintenanceTicketV1.from_record(record, source_generation=generation_for_read)


@pytest.mark.parametrize(('damage', 'expected_code'), (
    ('valid', 'ticket_accepted_non_authorizing'),
    ('missing_binding', 'admission_binding_invalid'),
    ('legacy_unbound', 'admission_binding_invalid'),
    ('stale_generation', 'admission_binding_invalid'),
))
def test_ticket_validation_round_trips_across_processes_and_rejects_invalid_records(damage, expected_code):
    m = module()
    source_generation = generation()
    ticket = versioned_ticket(source_generation)
    payload = json.dumps({
        'generation': asdict(source_generation),
        'record': ticket.to_record(),
        'damage': damage,
    }, sort_keys=True, separators=(',', ':'))
    backend = Path(__file__).resolve().parents[1] / 'backend'
    script = r"""
import json, socket, sqlite3, sys
from dataclasses import replace
sys.path.insert(0, sys.argv[1])
from secretary.infrastructure.restore_admission_binding import (
    AdmissionBindingError, MaintenanceTicketV1, SourceAuthorityV1, SourceGenerationV1,
)
payload = json.loads(sys.argv[2])
def forbidden_side_effect(*args, **kwargs):
    raise RuntimeError('ticket contract attempted external or durable I/O')
socket.socket = forbidden_side_effect
sqlite3.connect = forbidden_side_effect
generation_data = payload['generation']
generation_data['sources'] = tuple(SourceAuthorityV1(**item) for item in generation_data['sources'])
generation = SourceGenerationV1(**generation_data)
record = payload['record']
damage = payload['damage']
if damage == 'valid':
    pass
elif damage == 'missing_binding':
    del record['admission_binding']
elif damage == 'legacy_unbound':
    record = {'id': record['ticket_id'], 'participant_id': record['participant_id'],
        'run_id': record['run_id'], 'kind': record['kind'],
        'operation_id': record['operation_id'], 'fence': record['fence']}
elif damage == 'stale_generation':
    import hashlib
    generation = replace(generation, manifest_sha256=hashlib.sha256(b'stale manifest').hexdigest())
try:
    ticket = MaintenanceTicketV1.from_record(record, source_generation=generation)
except AdmissionBindingError as error:
    print(error.code)
    raise SystemExit(10)
if ticket.activation_supported is not False or ticket.outbound_enabled is not False or hasattr(ticket, 'permit'):
    print('ticket_authority_invariant_failed')
    raise SystemExit(11)
print('ticket_accepted_non_authorizing')
"""
    child = subprocess.run([sys.executable, '-B', '-c', script, str(backend), payload],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=20, check=False)

    if expected_code == 'ticket_accepted_non_authorizing':
        assert child.returncode == 0, child.stderr[-1200:]
    else:
        assert child.returncode == 10, child.stderr[-1200:]
    assert child.stdout.strip() == expected_code


@pytest.mark.parametrize('field,value', (
    ('deployment_id', str(uuid4())),
    ('participant_id', 'different-worker'),
    ('run_id', str(uuid4())),
    ('operation_id', str(uuid4())),
    ('fence', 8),
    ('kind', '../outbound_bot'),
))
def test_versioned_ticket_rejects_scope_mismatch(field, value):
    m = module()
    source_generation = generation()
    values = dict(
        ticket_id=str(uuid4()),
        deployment_id=source_generation.deployment_id,
        participant_id='synthetic-worker',
        run_id=str(uuid4()),
        kind='outbound_bot',
        operation_id=str(uuid4()),
        fence=7,
        admission_binding=admission_binding_for(source_generation),
        source_generation=source_generation,
    )
    values[field] = value
    with pytest.raises(m.AdmissionBindingError, match='admission_binding_invalid'):
        m.MaintenanceTicketV1.from_source_generation(**values)


def test_versioned_ticket_rejects_known_dispatch_kind_action_mismatch():
    m = module()
    source_generation = generation()
    binding = admission_binding_for(source_generation, action='sql_write')
    with pytest.raises(m.AdmissionBindingError, match='admission_binding_invalid'):
        versioned_ticket(source_generation, kind='outbound_bot', admission_binding=binding)
