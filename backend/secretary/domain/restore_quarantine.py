"""Immutable legacy deny evidence. Missing evidence is never a dispatch grant."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from uuid import UUID

PROTOCOL_VERSION = 1
AUTHORITIES = ('secretary', 'team', 'billing')
SOURCE_ROLES = (*AUTHORITIES, 'vikunja')
DISPOSITIONS = frozenset({'no_replay', 'authority_invalid', 'evidence_only'})
RELATIONS = frozenset({'source_reference', 'derives_from', 'evidence_for',
                      'reserves_resource', 'funded_by', 'authorized_by'})


class RestoreQuarantineError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def typed(values):
    output = []
    for value in values:
        if value is None: output.append(('null', None))
        elif type(value) is int: output.append(('int', str(value)))
        elif type(value) is str: output.append(('str', value))
        elif type(value) is bytes: output.append(('blob', value.hex()))
        elif type(value) is float and math.isfinite(value): output.append(('float', value.hex()))
        else: raise RestoreQuarantineError('restore_quarantine_value_invalid')
    return output


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise RestoreQuarantineError('restore_quarantine_input_invalid')


def _name(value):
    if not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9_]{0,127}', value):
        raise RestoreQuarantineError('restore_quarantine_input_invalid')


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value: raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RestoreQuarantineError('restore_quarantine_input_invalid') from None


def key_digest(authority, family, values, *, kind='row'):
    if authority not in SOURCE_ROLES: raise RestoreQuarantineError('restore_quarantine_input_invalid')
    _name(family)
    if not isinstance(kind, str) or not re.fullmatch(r'row|semantic:[a-z][a-z0-9_]{0,63}', kind):
        raise RestoreQuarantineError('restore_quarantine_input_invalid')
    if not isinstance(values, (tuple, list)) or not values:
        raise RestoreQuarantineError('restore_quarantine_input_invalid')
    return digest([PROTOCOL_VERSION, authority, family, kind, typed(values)])


@dataclass(frozen=True, order=True)
class Origin:
    authority: str
    family: str
    key_hash: str
    key_kind: str = 'row'

    def __post_init__(self):
        if self.authority not in AUTHORITIES: raise RestoreQuarantineError('restore_quarantine_input_invalid')
        _name(self.family); _sha(self.key_hash)
        if not isinstance(self.key_kind, str) or not re.fullmatch(r'row|semantic:[a-z][a-z0-9_]{0,63}', self.key_kind):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')


@dataclass(frozen=True)
class ReferenceSpec:
    source_role: str
    source_family: str
    source_columns: tuple[str, ...]
    target_role: str
    target_family: str
    target_columns: tuple[str, ...]
    relation: str

    def __post_init__(self):
        if (self.source_role not in AUTHORITIES or self.target_role not in AUTHORITIES
                or not isinstance(self.relation, str) or self.relation not in RELATIONS
                or type(self.source_columns) is not tuple or type(self.target_columns) is not tuple
                or not self.source_columns or len(self.source_columns) != len(self.target_columns)):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')
        for value in (self.source_family, self.target_family, *self.source_columns, *self.target_columns):
            _name(value)


@dataclass(frozen=True)
class SourceWatermark:
    authority: str
    file_sha256: str
    logical_sha256: str
    schema_sha256: str
    tables: int
    rows: int

    def __post_init__(self):
        if self.authority not in SOURCE_ROLES: raise RestoreQuarantineError('restore_quarantine_input_invalid')
        for value in (self.file_sha256, self.logical_sha256, self.schema_sha256): _sha(value)
        if any(type(value) is not int or value < 0 for value in (self.tables, self.rows)):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')


@dataclass(frozen=True)
class QuarantineInput:
    restore_id: str
    manifest_sha256: str
    metadata_sha256: str
    bindings_sha256: str
    preview_sha256: str
    catalogue_sha256: str
    sources: tuple[SourceWatermark, ...]

    def __post_init__(self):
        _uuid(self.restore_id)
        for value in (self.manifest_sha256, self.metadata_sha256, self.bindings_sha256,
                      self.preview_sha256, self.catalogue_sha256): _sha(value)
        if (type(self.sources) is not tuple or len(self.sources) != 4
                or any(type(item) is not SourceWatermark for item in self.sources)
                or {item.authority for item in self.sources} != set(SOURCE_ROLES)):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')

    @property
    def input_sha256(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class QuarantineEntry:
    origin: Origin
    row_sha256: str
    state_sha256: str
    disposition: str

    def __post_init__(self):
        if (type(self.origin) is not Origin or not isinstance(self.disposition, str)
                or self.disposition not in DISPOSITIONS):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')
        _sha(self.row_sha256); _sha(self.state_sha256)


@dataclass(frozen=True, order=True)
class QuarantineLink:
    child: Origin
    parent: Origin
    relation: str

    def __post_init__(self):
        if (type(self.child) is not Origin or type(self.parent) is not Origin
                or not isinstance(self.relation, str) or self.relation not in RELATIONS):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')


@dataclass(frozen=True)
class QuarantinePlan:
    input: QuarantineInput
    entries: tuple[QuarantineEntry, ...]
    links: tuple[QuarantineLink, ...]
    # Fixed immutable counts, including empty families. No mutable mapping in seal.
    family_counts: tuple[tuple[str, str, int], ...]

    def __post_init__(self):
        if type(self.input) is not QuarantineInput or any(type(v) is not tuple for v in
                                                         (self.entries, self.links, self.family_counts)):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')
        if (any(type(item) is not QuarantineEntry for item in self.entries)
                or any(type(item) is not QuarantineLink for item in self.links)
                or any(type(item) is not tuple or len(item) != 3 for item in self.family_counts)):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')
        origins = {item.origin for item in self.entries}
        if len(origins) != len(self.entries): raise RestoreQuarantineError('restore_quarantine_duplicate_key')
        if len(set(self.links)) != len(self.links) or any(link.child not in origins or link.parent not in origins for link in self.links):
            raise RestoreQuarantineError('restore_quarantine_lineage_invalid')
        counts = {}
        for role, family, count in self.family_counts:
            _name(family)
            if role not in AUTHORITIES or type(count) is not int or count < 0 or (role, family) in counts:
                raise RestoreQuarantineError('restore_quarantine_input_invalid')
            counts[role, family] = count
        if self.family_counts != tuple(sorted(self.family_counts)):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')
        actual = {key: 0 for key in counts}
        for item in self.entries:
            key = (item.origin.authority, item.origin.family)
            if key not in actual: raise RestoreQuarantineError('restore_quarantine_catalogue_unsupported')
            if item.origin.key_kind == 'row': actual[key] += 1
        if actual != counts: raise RestoreQuarantineError('restore_quarantine_coverage_invalid')
        if self.entries != tuple(sorted(self.entries, key=lambda item: item.origin)) or self.links != tuple(sorted(self.links)):
            raise RestoreQuarantineError('restore_quarantine_input_invalid')

    @property
    def coverage(self):
        return MappingProxyType({role: MappingProxyType({family: count for r, family, count in self.family_counts if r == role})
                                 for role in AUTHORITIES})

    @property
    def plan_sha256(self):
        return digest({'input': asdict(self.input), 'entries': [asdict(item) for item in self.entries],
                       'links': [asdict(item) for item in self.links], 'coverage': self.family_counts})

    def disposition(self, origin):
        if type(origin) is not Origin: raise RestoreQuarantineError('restore_lineage_unbound')
        return next((item.disposition for item in self.entries if item.origin == origin), None)

    def assert_no_legacy_authority(self, authorization_parents):
        if type(authorization_parents) is not tuple or not authorization_parents:
            raise RestoreQuarantineError('restore_lineage_unbound')
        for origin in authorization_parents:
            if self.disposition(origin) is not None:
                raise RestoreQuarantineError('restore_legacy_work_blocked')
        # This is only absence of a known deny; R3/R4 must provide fresh authority.

    def report(self):
        return {'protocol_version': PROTOCOL_VERSION, 'state': 'quarantine_planned',
                'activation_supported': False, 'outbound_enabled': False,
                'restore_id': self.input.restore_id, 'input_sha256': self.input.input_sha256,
                'plan_sha256': self.plan_sha256, 'entries': len(self.entries), 'links': len(self.links),
                'coverage': {role: dict(counts) for role, counts in self.coverage.items()},
                'pending_requirements': ['controlled_r3_preparation', 'complete_r3_decision',
                                         'r4_activation_and_fresh_lineage', 'r5_next_generation',
                                         'owner_provider_and_manual_restore']}
