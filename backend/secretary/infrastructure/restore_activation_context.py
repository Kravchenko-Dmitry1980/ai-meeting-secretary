"""Read back the actual inactive R3 baseline for a future R4 transition.

This collector accepts explicit locations, never caller evidence or a Boolean
grant. It opens no ordinary repositories, creates no files, issues no consent,
and does not enable activation. Current Windows operator custody is independent
of the archived business-owner row; fresh business-owner/provider authorization
and final source preparations remain separate requirements for activation.

Whole-file hashes here are initial provenance, not a perpetual live invariant.
Windows write/delete-denial handles retain the sources while immutable observers
read them. The returned DTO is a diagnostic commitment and carries no lease or
permission. A later writer must reacquire custody and revalidate this baseline.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from secretary.domain.restore_quarantine import AUTHORITIES, SOURCE_ROLES, digest
from .restore_operator import OperatorAuthority, protected_scope, _local
from .restore_reconciliation_control import RestoreControl
from .restore_runtime_evidence import verify_stopped_runtime
from .restore_quarantine_preparation import read_preparation
from .restore_quarantine_json import JSON_RULES_VERSION
from .team_restore_quarantine import _catalogue_hash
from .team_restore_reconciliation import (
    _read, _load_plan, _identity, _pin_file, _retained_files, _rowids, _deadline,
)
from .team_backup import _safe, _hash, _inactive_source, _validate_archive

_SHA = re.compile('[0-9a-f]{64}')
_ENVELOPE_KEYS = frozenset(('version', 'scope', 'plan', 'rowids', 'retained',
    'runtime_evidence_sha256'))


class ActivationContextError(ValueError):
    """Stable sanitized error, without paths, sensitive rows or raw SQL."""
    code = 'maintenance_restore_blocked'

    def __init__(self):
        super().__init__(self.code)


def _fail():
    raise ActivationContextError() from None


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _path(value, *, file=False):
    return _safe(_local(value), file=file)


@dataclass(frozen=True)
class PreparedSourceContext:
    role: str
    path: str
    file_id: tuple[int, int]
    baseline_file_sha256: str
    preparation_sha256: str
    source_commitment_sha256: str


@dataclass(frozen=True)
class PreparedActivationContext:
    protocol: int
    restore_id: str
    r3_id: str
    r3_decision_sha256: str
    r3_binding_sha256: str
    manifest_sha256: str
    metadata_sha256: str
    inventory_sha256: str
    source_context_sha256: str
    scope_sha256: str
    runtime_evidence_sha256: str
    sources: tuple[PreparedSourceContext, ...]

    @property
    def state(self):
        return 'prepared_sources_verified_blocked'

    @property
    def activation_supported(self):
        return False

    @property
    def outbound_enabled(self):
        return False

    @property
    def ledger_binding(self):
        # A new detached dict cannot mutate the frozen collector result.
        return {key: getattr(self, key) for key in ('protocol', 'restore_id', 'r3_id',
            'r3_decision_sha256', 'r3_binding_sha256', 'manifest_sha256',
            'metadata_sha256', 'inventory_sha256', 'source_context_sha256')}


def collect_prepared_activation_context(project_root, backup_dir, restore_dir, *,
        maintenance_path, lifecycle_path) -> PreparedActivationContext:
    """Verify existing R3 decision/receipts/sources under actual inactive custody."""
    try:
        return _collect(project_root, backup_dir, restore_dir,
            maintenance_path=maintenance_path, lifecycle_path=lifecycle_path)
    except ActivationContextError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
            UnicodeError, OverflowError, RecursionError):
        _fail()


def _collect(project_root, backup_dir, restore_dir, *, maintenance_path, lifecycle_path,
             _source_observer=None):
    root, archive, target = (_path(item) for item in (project_root, backup_dir, restore_dir))
    if (not all(item.is_dir() for item in (root, archive, target))
            or archive == target or archive.is_relative_to(target) or target.is_relative_to(archive)
            or archive == root or target == root
            or not archive.is_relative_to(root) or not target.is_relative_to(root)):
        _fail()
    metadata_path = target / 'restore.json'
    metadata = _read(metadata_path, 4 * 1024 * 1024)
    if type(metadata) is not dict or set(metadata.get('databases', {})) != set(SOURCE_ROLES):
        _fail()
    restore_id = metadata.get('restore_id')
    if type(restore_id) is not str or str(UUID(restore_id)) != restore_id:
        _fail()
    paths = {role: _path(metadata['databases'][role], file=True) for role in SOURCE_ROLES}
    original = tuple(_path(item, file=True) for item in (maintenance_path, lifecycle_path))
    if (len(set(paths.values())) != 4 or len(set(original)) != 2
            or any(not path.is_relative_to(target) for path in paths.values())
            or any(not path.is_relative_to(root) or path.is_relative_to(target)
                or path.is_relative_to(archive) for path in original)):
        _fail()
    operator = OperatorAuthority(root)
    anchor = _path(operator.anchor)
    if (anchor != root / '.runtime/team-operator' or not anchor.is_dir()
            or any(path.is_relative_to(anchor) for path in (*paths.values(), archive, target))):
        _fail()
    base, directory = anchor / 'restores', anchor / 'restores' / restore_id
    inventory_path, control_path = directory / 'inventory.json', directory / 'control.sqlite3'
    # Explicit existing-only validation before any connection. No mkdir/creator.
    for path in (inventory_path, control_path): _path(path, file=True)
    with ExitStack() as stack:
        for directory_path in (target, anchor, base, directory):
            stack.enter_context(protected_scope(directory_path))
        for path in (*paths.values(), *original, inventory_path, control_path,
                     archive / 'manifest.json', metadata_path):
            stack.enter_context(_pin_file(path, deny_write=True))
        retained = _retained_files(archive, target, paths)
        retained_dirs = {archive, target / 'assets'}
        for raw in retained:
            retained_dirs.update(parent for parent in Path(raw).parents
                if parent.is_relative_to(archive) or parent.is_relative_to(target))
        for directory_path in sorted(retained_dirs):
            stack.enter_context(_pin_file(directory_path, directory=True))
        for raw in sorted(retained):
            stack.enter_context(_pin_file(Path(raw), deny_write=True))
        manifest = _validate_archive(archive)
        runtime_args = dict(project_root=root, archive_manifest=manifest,
            maintenance_path=original[0], lifecycle_path=original[1])
        evidence = verify_stopped_runtime(**runtime_args)
        if type(evidence) is not dict or not _sha(evidence.get('evidence_sha256')):
            _fail()
        envelope = _read(inventory_path)
        if (type(envelope) is not dict or set(envelope) != _ENVELOPE_KEYS
                or type(envelope['version']) is not int or envelope['version'] != 1):
            _fail()
        scope = dict(root=str(root), root_id=_identity(root), archive=str(archive),
            archive_id=_identity(archive), target=str(target), target_id=_identity(target),
            files={role: [str(path), _identity(path)] for role, path in paths.items()},
            runtime=[str(path) for path in original])
        if (envelope['scope'] != scope or type(envelope['rowids']) is not dict
                or set(envelope['rowids']) != set(AUTHORITIES)
                or any(not _sha(value) for value in envelope['rowids'].values())
                or envelope['retained'] != retained
                or envelope['runtime_evidence_sha256'] != evidence['evidence_sha256']):
            _fail()
        plan = _load_plan(envelope['plan'])
        manifest_hash, metadata_hash = _hash(archive / 'manifest.json'), _hash(metadata_path)
        if (plan.input.restore_id != restore_id or plan.input.manifest_sha256 != manifest_hash
                or plan.input.metadata_sha256 != metadata_hash
                or plan.input.catalogue_sha256 != _catalogue_hash(JSON_RULES_VERSION)):
            _fail()
        r3_binding = dict(protocol=1, restore_id=restore_id, plan_sha256=plan.plan_sha256,
            input_sha256=plan.input.input_sha256, scope_sha256=digest(scope),
            evidence_sha256=digest(envelope),
            source_paths_sha256=digest({role: str(path) for role, path in paths.items()}),
            operator_authority_sha256=operator.operator_digest)
        control = RestoreControl.open_existing(control_path, r3_binding)
        current = control.snapshot()
        decision = current['decision']
        if (decision is None or decision['state'] != 'complete_prepared_blocked'
                or set(decision['preparations']) != set(AUTHORITIES)):
            _fail()
        prepared = []
        for role in SOURCE_ROLES:
            path = paths[role]
            _inactive_source(path)
            actual_hash = _hash(path)
            observed_hash = actual_hash
            source = next(item for item in plan.input.sources if item.authority == role)
            if _source_observer is not None:
                # Internal controlled R4 resume only. Public baseline collection
                # never supplies this observer and remains byte-exact R3. The
                # observer reads actual exact metadata, business/R2/rowids and
                # independent recorded readbacks; it cannot mint a new baseline.
                from .restore_source_epoch_writer import _PreparedSourceObserver
                if type(_source_observer) is not _PreparedSourceObserver:
                    _fail()
                if role in AUTHORITIES:
                    record = decision['preparations'][role]
                    actual_hash = record['file_sha256']
                    preparation_hash = record['receipt_sha256']
                else:
                    actual_hash = decision['native_file_sha256']
                    preparation_hash = digest(dict(protocol=1, r3_id=current['r3_id'],
                        native_initial_file_sha256=actual_hash))
                _source_observer.observe(path, role, plan, envelope, current,
                    source, observed_hash, actual_hash, preparation_hash)
            elif role in AUTHORITIES:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1',
                        uri=True, timeout=.2, isolation_level=None)) as conn:
                    conn.execute('BEGIN')
                    _deadline(conn)
                    receipt = read_preparation(conn, plan=plan, authority=role)
                    if (receipt is None or receipt.prepared_by_R3_id != current['r3_id']
                            or _rowids(conn) != envelope['rowids'][role]):
                        _fail()
                    preparation_hash = digest(asdict(receipt))
                if decision['preparations'][role] != dict(receipt_sha256=preparation_hash,
                        file_sha256=actual_hash):
                    _fail()
            else:
                if actual_hash != source.file_sha256 or actual_hash != decision['native_file_sha256']:
                    _fail()
                preparation_hash = digest(dict(protocol=1, r3_id=current['r3_id'],
                    native_initial_file_sha256=actual_hash))
            commitment = dict(role=role, path=str(path), file_id=_identity(path),
                baseline_file_sha256=actual_hash, preparation_sha256=preparation_hash,
                original_source=asdict(source), restore_id=restore_id, r3_id=current['r3_id'])
            prepared.append(PreparedSourceContext(role, str(path), tuple(_identity(path)),
                actual_hash, preparation_hash, digest(commitment)))
            _inactive_source(path)
            if _hash(path) != observed_hash: _fail()
        # Reopen observers after all four readbacks and verify the fixed external
        # runtime port again. Earlier hashes or the returned DTO are not custody.
        if (control.snapshot() != current or _read(inventory_path) != envelope
                or _retained_files(archive, target, paths) != retained
                or verify_stopped_runtime(**runtime_args).get('evidence_sha256') != evidence['evidence_sha256']
                or operator.operator_digest != r3_binding['operator_authority_sha256']):
            _fail()
        context_hash = digest(dict(protocol=1, scope=scope, sources=[asdict(item) for item in prepared],
            retained=retained, runtime_evidence_sha256=evidence['evidence_sha256'],
            r3_decision_sha256=decision['decision_sha256'], inventory_sha256=digest(envelope)))
        return PreparedActivationContext(1, restore_id, current['r3_id'],
            decision['decision_sha256'], current['binding_sha256'], manifest_hash, metadata_hash,
            digest(envelope), context_hash, digest(scope), evidence['evidence_sha256'], tuple(prepared))
