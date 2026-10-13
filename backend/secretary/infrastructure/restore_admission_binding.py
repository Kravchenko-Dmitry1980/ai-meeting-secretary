"""Pure, non-authorizing R4/R5 commitments; this module never evaluates admission.

The source-generation digest binds immutable evidence commitments, not mutable
business rows. The operation binding is a wire-shaped commitment only: callers
must not treat a valid instance or digest as permission. No storage, source
paths, credentials, provider calls, or activation state are opened here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from uuid import UUID


SOURCE_ROLES = ('secretary', 'team', 'billing', 'vikunja')
_ACTIONS = frozenset(('fresh_work', 'fresh_auth', 'sql_write', 'recovery',
    'native_runtime', 'polza_dispatch', 'vikunja_dispatch', 'max_dispatch'))
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_PARTICIPANT = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_TICKET_KIND = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_TICKET_ACTIONS = {
    'outbound_bot': 'max_dispatch',
    'outbound_notification': 'max_dispatch',
    'outbound_subscription': 'max_dispatch',
    'outbound_task': 'vikunja_dispatch',
    'outbound_polza': 'polza_dispatch',
}
_MAX_COUNTER = 2 ** 63 - 1


class SourceGenerationError(ValueError):
    """A malformed or incomplete source-generation commitment."""

    code = 'source_generation_invalid'

    def __init__(self):
        super().__init__(self.code)


class AdmissionBindingError(ValueError):
    """A malformed or unscoped operation-binding commitment."""

    code = 'admission_binding_invalid'

    def __init__(self):
        super().__init__(self.code)


class SuccessorFenceCandidateError(ValueError):
    """A malformed or non-contiguous successor-fence proposal."""

    code = 'successor_fence_candidate_invalid'

    def __init__(self):
        super().__init__(self.code)


def _fail_generation():
    raise SourceGenerationError() from None


def _fail_binding():
    raise AdmissionBindingError() from None


def _fail_successor_fence():
    raise SuccessorFenceCandidateError() from None


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_canonical(value).encode('ascii')).hexdigest()


@dataclass(frozen=True)
class SourceAuthorityV1:
    """Opaque commitments for one authority; contains no path or business rows."""

    role: str
    source_path_sha256: str
    file_identity_sha256: str
    baseline_file_sha256: str
    r3_preparation_sha256: str
    r4_preparation_sha256: str
    readback_sha256: str

    def __post_init__(self):
        if (type(self.role) is not str or self.role not in SOURCE_ROLES
                or any(not _sha(value) for value in (
                    self.source_path_sha256, self.file_identity_sha256,
                    self.baseline_file_sha256, self.r3_preparation_sha256,
                    self.r4_preparation_sha256, self.readback_sha256))):
            _fail_generation()


@dataclass(frozen=True)
class SourceGenerationV1:
    """Versioned input commitment; valid instances are not activation evidence."""

    deployment_id: str
    restore_id: str
    r3_id: str
    r3_decision_sha256: str
    r3_binding_sha256: str
    epoch_id: str
    manifest_sha256: str
    metadata_sha256: str
    inventory_sha256: str
    scope_sha256: str
    configuration_versions_sha256: str
    sources: tuple[SourceAuthorityV1, ...]
    native_schema_sha256: str
    native_head_sequence: int
    native_head_sha256: str
    native_authentication_sha256: str
    protocol: int = 1

    def __post_init__(self):
        if (type(self.protocol) is not int or self.protocol != 1
                or any(not _uuid(value) for value in (
                    self.deployment_id, self.restore_id, self.r3_id, self.epoch_id))
                or any(not _sha(value) for value in (
                    self.r3_decision_sha256, self.r3_binding_sha256,
                    self.manifest_sha256, self.metadata_sha256,
                    self.inventory_sha256, self.scope_sha256,
                    self.configuration_versions_sha256, self.native_schema_sha256,
                    self.native_head_sha256, self.native_authentication_sha256))
                or type(self.sources) is not tuple or len(self.sources) != len(SOURCE_ROLES)
                or any(type(item) is not SourceAuthorityV1 for item in self.sources)
                or tuple(item.role for item in self.sources) != SOURCE_ROLES
                or type(self.native_head_sequence) is not int
                or not 1 <= self.native_head_sequence <= _MAX_COUNTER):
            _fail_generation()

    @property
    def source_generation_sha256(self):
        return _digest(asdict(self))

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False


@dataclass(frozen=True)
class MaintenanceFenceLineageV1:
    """Opaque archived/current maintenance history commitments; not a fence permit."""

    deployment_id: str
    archive_barrier_id: str
    archive_barrier_fence: int
    archive_event_count: int
    archive_events_sha256: str
    observed_high_water_fence: int
    observed_event_count: int
    observed_events_sha256: str
    stopped_runtime_evidence_sha256: str

    def __post_init__(self):
        if (not _uuid(self.deployment_id) or not _uuid(self.archive_barrier_id)
                or type(self.archive_barrier_fence) is not int
                or not 1 <= self.archive_barrier_fence <= _MAX_COUNTER
                or type(self.archive_event_count) is not int
                or not 1 <= self.archive_event_count <= 10000
                or not _sha(self.archive_events_sha256)
                or type(self.observed_high_water_fence) is not int
                or not self.archive_barrier_fence <= self.observed_high_water_fence <= _MAX_COUNTER
                or type(self.observed_event_count) is not int
                or not self.archive_event_count <= self.observed_event_count <= 10000
                or not _sha(self.observed_events_sha256)
                or not _sha(self.stopped_runtime_evidence_sha256)):
            _fail_generation()

    @property
    def lineage_sha256(self):
        return _digest(asdict(self))


@dataclass(frozen=True)
class R5SuccessorFenceCandidateV1:
    """Blocked next-fence proposal derived from stopped R5 lineage only."""

    maintenance_fence_lineage: MaintenanceFenceLineageV1
    expected_successor_fence: int
    protocol: int = 1

    def __post_init__(self):
        if (type(self.protocol) is not int or self.protocol != 1
                or type(self.maintenance_fence_lineage) is not MaintenanceFenceLineageV1):
            _fail_successor_fence()
        observed = self.maintenance_fence_lineage.observed_high_water_fence
        if (type(self.expected_successor_fence) is not int
                or observed >= _MAX_COUNTER
                or self.expected_successor_fence != observed + 1):
            _fail_successor_fence()

    @classmethod
    def from_lineage(cls, lineage):
        if type(lineage) is not MaintenanceFenceLineageV1:
            _fail_successor_fence()
        observed = lineage.observed_high_water_fence
        if observed >= _MAX_COUNTER:
            _fail_successor_fence()
        return cls(maintenance_fence_lineage=lineage,
            expected_successor_fence=observed + 1)

    @property
    def candidate_sha256(self):
        return _digest(asdict(self))

    def to_prepared_blocked_record(self):
        return {
            'protocol': self.protocol,
            'state': 'prepared_blocked',
            'maintenance_fence_lineage_sha256': self.maintenance_fence_lineage.lineage_sha256,
            'expected_successor_fence': self.expected_successor_fence,
            'candidate_sha256': self.candidate_sha256,
        }

    @classmethod
    def from_prepared_blocked_record(cls, record, *, maintenance_fence_lineage):
        keys = {'protocol', 'state', 'maintenance_fence_lineage_sha256',
                'expected_successor_fence', 'candidate_sha256'}
        if (type(record) is not dict or set(record) != keys
                or type(record['protocol']) is not int or record['protocol'] != 1
                or type(record['state']) is not str or record['state'] != 'prepared_blocked'
                or type(maintenance_fence_lineage) is not MaintenanceFenceLineageV1
                or not _sha(record['maintenance_fence_lineage_sha256'])
                or record['maintenance_fence_lineage_sha256'] != maintenance_fence_lineage.lineage_sha256
                or type(record['expected_successor_fence']) is not int
                or not _sha(record['candidate_sha256'])):
            _fail_successor_fence()
        candidate = cls(maintenance_fence_lineage=maintenance_fence_lineage,
            expected_successor_fence=record['expected_successor_fence'])
        if candidate.candidate_sha256 != record['candidate_sha256']:
            _fail_successor_fence()
        return candidate

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False


@dataclass(frozen=True)
class SourceGenerationV2:
    """R5-aware generation commitment; valid instances still cannot authorize work."""

    base_generation: SourceGenerationV1
    maintenance_fence_lineage: MaintenanceFenceLineageV1
    protocol: int = 2

    def __post_init__(self):
        if (type(self.protocol) is not int or self.protocol != 2
                or type(self.base_generation) is not SourceGenerationV1
                or type(self.maintenance_fence_lineage) is not MaintenanceFenceLineageV1
                or self.base_generation.deployment_id != self.maintenance_fence_lineage.deployment_id):
            _fail_generation()

    @property
    def source_generation_sha256(self):
        return _digest(asdict(self))

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False


@dataclass(frozen=True)
class SuccessorFenceCandidateV1:
    """Deterministic next-fence proposal; never a durable handoff or permit."""

    source_generation: SourceGenerationV2
    expected_successor_fence: int
    protocol: int = 1

    def __post_init__(self):
        if (type(self.protocol) is not int or self.protocol != 1
                or type(self.source_generation) is not SourceGenerationV2):
            _fail_successor_fence()
        observed = self.source_generation.maintenance_fence_lineage.observed_high_water_fence
        if (type(self.expected_successor_fence) is not int
                or observed >= _MAX_COUNTER
                or self.expected_successor_fence != observed + 1):
            _fail_successor_fence()

    @classmethod
    def from_source_generation(cls, source_generation):
        if type(source_generation) is not SourceGenerationV2:
            _fail_successor_fence()
        observed = source_generation.maintenance_fence_lineage.observed_high_water_fence
        if observed >= _MAX_COUNTER:
            _fail_successor_fence()
        return cls(source_generation=source_generation, expected_successor_fence=observed + 1)

    @property
    def candidate_sha256(self):
        return _digest(asdict(self))

    def to_prepared_blocked_record(self):
        """Return a strict restart record; this does not persist or authorize it."""
        return {
            'protocol': self.protocol,
            'state': 'prepared_blocked',
            'source_generation_sha256': self.source_generation.source_generation_sha256,
            'expected_successor_fence': self.expected_successor_fence,
            'candidate_sha256': self.candidate_sha256,
        }

    @classmethod
    def from_prepared_blocked_record(cls, record, *, source_generation):
        keys = {'protocol', 'state', 'source_generation_sha256',
                'expected_successor_fence', 'candidate_sha256'}
        if (type(record) is not dict or set(record) != keys
                or type(record['protocol']) is not int or record['protocol'] != 1
                or type(record['state']) is not str or record['state'] != 'prepared_blocked'
                or type(source_generation) is not SourceGenerationV2
                or not _sha(record['source_generation_sha256'])
                or record['source_generation_sha256'] != source_generation.source_generation_sha256
                or type(record['expected_successor_fence']) is not int
                or not _sha(record['candidate_sha256'])):
            _fail_successor_fence()
        candidate = cls(source_generation=source_generation,
            expected_successor_fence=record['expected_successor_fence'])
        if candidate.candidate_sha256 != record['candidate_sha256']:
            _fail_successor_fence()
        return candidate

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False


@dataclass(frozen=True)
class R4AdmissionBindingV1:
    """Opaque operation-scope commitment; it does not grant or carry a permit."""

    deployment_id: str
    restore_id: str
    r3_id: str
    epoch_id: str
    source_generation_sha256: str
    decision_id: str
    decision_sha256: str
    action: str
    participant_id: str
    run_id: str
    operation_id: str
    barrier_fence: int
    protocol: int = 1

    def __post_init__(self):
        if (type(self.protocol) is not int or self.protocol != 1
                or any(not _uuid(value) for value in (
                    self.deployment_id, self.restore_id, self.r3_id, self.epoch_id,
                    self.decision_id, self.run_id, self.operation_id))
                or any(not _sha(value) for value in (
                    self.source_generation_sha256, self.decision_sha256))
                or type(self.action) is not str or self.action not in _ACTIONS
                or type(self.participant_id) is not str
                or _PARTICIPANT.fullmatch(self.participant_id) is None
                or type(self.barrier_fence) is not int
                or not 1 <= self.barrier_fence <= _MAX_COUNTER):
            _fail_binding()

    @property
    def binding_sha256(self):
        return _digest(asdict(self))

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False


@dataclass(frozen=True)
class MaintenanceTicketV1:
    """Versioned ticket commitment; it is neither durable nor an admission permit."""

    ticket_id: str
    deployment_id: str
    participant_id: str
    run_id: str
    kind: str
    operation_id: str
    fence: int
    admission_binding: R4AdmissionBindingV1
    protocol: int = 1

    def __post_init__(self):
        if (type(self.protocol) is not int or self.protocol != 1
                or not _uuid(self.ticket_id)
                or not _uuid(self.deployment_id)
                or type(self.participant_id) is not str or _PARTICIPANT.fullmatch(self.participant_id) is None
                or not _uuid(self.run_id)
                or type(self.kind) is not str or _TICKET_KIND.fullmatch(self.kind) is None
                or not _uuid(self.operation_id)
                or type(self.fence) is not int or not 1 <= self.fence <= _MAX_COUNTER
                or type(self.admission_binding) is not R4AdmissionBindingV1
                or (self.deployment_id, self.participant_id, self.run_id, self.operation_id, self.fence)
                    != (self.admission_binding.deployment_id, self.admission_binding.participant_id,
                        self.admission_binding.run_id, self.admission_binding.operation_id,
                        self.admission_binding.barrier_fence)
                or self.kind in _TICKET_ACTIONS
                    and self.admission_binding.action != _TICKET_ACTIONS[self.kind]):
            _fail_binding()

    @classmethod
    def from_source_generation(cls, *, ticket_id, deployment_id, participant_id, run_id,
                               kind, operation_id, fence, admission_binding, source_generation):
        ticket = cls(ticket_id=ticket_id, deployment_id=deployment_id,
            participant_id=participant_id, run_id=run_id, kind=kind,
            operation_id=operation_id, fence=fence,
            admission_binding=admission_binding)
        ticket.assert_source_generation(source_generation)
        return ticket

    def assert_source_generation(self, source_generation):
        if type(source_generation) is SourceGenerationV1:
            generation = source_generation
        elif type(source_generation) is SourceGenerationV2:
            generation = source_generation.base_generation
        else:
            _fail_binding()
        binding = self.admission_binding
        if ((binding.deployment_id, binding.restore_id, binding.r3_id, binding.epoch_id,
             binding.source_generation_sha256)
                != (generation.deployment_id, generation.restore_id, generation.r3_id,
                    generation.epoch_id, source_generation.source_generation_sha256)):
            _fail_binding()

    @property
    def ticket_sha256(self):
        return _digest(asdict(self))

    def to_record(self):
        """Serialize opaque commitments only; this record cannot grant permission."""
        return {
            'protocol': self.protocol,
            'ticket_id': self.ticket_id,
            'deployment_id': self.deployment_id,
            'participant_id': self.participant_id,
            'run_id': self.run_id,
            'kind': self.kind,
            'operation_id': self.operation_id,
            'fence': self.fence,
            'admission_binding': asdict(self.admission_binding),
            'ticket_sha256': self.ticket_sha256,
        }

    @classmethod
    def from_record(cls, record, *, source_generation):
        keys = {'protocol', 'ticket_id', 'deployment_id', 'participant_id', 'run_id',
                'kind', 'operation_id', 'fence', 'admission_binding', 'ticket_sha256'}
        binding_keys = {
            'protocol', 'deployment_id', 'restore_id', 'r3_id', 'epoch_id',
            'source_generation_sha256', 'decision_id', 'decision_sha256', 'action',
            'participant_id', 'run_id', 'operation_id', 'barrier_fence',
        }
        if (type(record) is not dict or set(record) != keys
                or type(record['protocol']) is not int or record['protocol'] != 1
                or type(record['admission_binding']) is not dict
                or set(record['admission_binding']) != binding_keys
                or not _sha(record['ticket_sha256'])):
            _fail_binding()
        try:
            binding = R4AdmissionBindingV1(**record['admission_binding'])
            ticket = cls(ticket_id=record['ticket_id'], deployment_id=record['deployment_id'],
                participant_id=record['participant_id'], run_id=record['run_id'],
                kind=record['kind'], operation_id=record['operation_id'],
                fence=record['fence'], admission_binding=binding, protocol=record['protocol'])
        except (TypeError, ValueError):
            _fail_binding()
        ticket.assert_source_generation(source_generation)
        if ticket.ticket_sha256 != record['ticket_sha256']:
            _fail_binding()
        return ticket

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False
