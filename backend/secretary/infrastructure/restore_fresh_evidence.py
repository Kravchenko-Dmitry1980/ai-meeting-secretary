"""Leased read-only R4 source observations, never activation authority.

Only actual fixed R3/V1/source-preparation stores select the proposed epoch.
Inactive bytes are held for this observation lifetime and verified on exit;
they are initial evidence, not a perpetual live-file invariant. No settings,
credentials, repositories, providers, migrations or processes are opened here.
Windows custody excludes other writers, not malicious same-user Python code.
"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
import sqlite3
from uuid import UUID

from secretary.domain.restore_quarantine import SOURCE_ROLES, digest
from .restore_operator import protected_scope, _local, _win32_path
from .restore_runtime_evidence import verify_stopped_runtime
from .restore_source_preparation_control import SourcePreparationControl
from .restore_source_epoch_writer import _baseline, _locator
from .team_backup import _safe, _hash, _inactive_source, _validate_archive
from .team_restore_reconciliation import _read, _identity, _pin_file, _retained_files, _deadline

_MAX_LIABILITIES = 128
_CODES = frozenset(('fresh_evidence_invalid', 'fresh_evidence_closed',
    'fresh_evidence_changed', 'fresh_evidence_incomplete', 'fresh_evidence_liabilities_bounded'))


class FreshEvidenceError(ValueError):
    """Stable code only; no raw SQL, source paths, billing rows or credentials."""
    def __init__(self, code='fresh_evidence_invalid'):
        self.code = code if type(code) is str and code in _CODES else 'fresh_evidence_invalid'
        super().__init__(self.code)


class _CallerBodyError(Exception):
    def __init__(self, error): self.error = error


def _fail(code='fresh_evidence_invalid'):
    raise FreshEvidenceError(code) from None


@dataclass(frozen=True)
class BillingLiability:
    operation_id: str
    key_tag: str
    provider_request_id: str | None
    provider_job_id: str | None
    status: str
    reserved_micro: int
    observed_cost_micro: int | None


@dataclass(frozen=True, init=False)
class SourceEvidence:
    snapshot_sha256: str
    restore_id: str
    epoch_id: str
    preparation_id: str
    scope_sha256: str
    capabilities: tuple[str, ...]
    context_sha256: str
    native_head_sha256: str
    binding_sha256: str
    runtime_deployment_id: str
    source_paths: tuple[tuple[str, str], ...]
    _session: object = field(repr=False, compare=False)

    def __init__(self, *args, **kwargs):
        raise TypeError('source_evidence_requires_live_collector')

    def verify_current(self):
        """Re-observe actual fixed stores/files/runtime while retained custody lives."""
        self._session.verify_current()

    def read_liabilities(self) -> tuple[BillingLiability, ...]:
        """Read bounded unresolved billing attempts; never clear or reconcile them."""
        return self._session.read_liabilities()


class _Observation:
    def __init__(self, arguments, baseline, ledger, control, original_ledger, proposal,
                 paths, archive, target, files, directories, retained, runtime_args, runtime):
        self.arguments, self.baseline = arguments, baseline
        self.ledger, self.control = ledger, control
        self.original_ledger, self.proposal = original_ledger, proposal
        self.paths, self.archive, self.target = paths, archive, target
        self.files, self.directories, self.retained = files, directories, retained
        self.runtime_args, self.runtime = runtime_args, runtime
        self.active = True

    def verify_current(self):
        if not self.active: _fail('fresh_evidence_closed')
        try:
            for path in (*self.paths.values(), self.ledger._path, self.control._path,
                         Path(self.runtime_args['maintenance_path'])):
                _inactive_source(_io(path))
            if (self.ledger.snapshot() != self.original_ledger
                    or self.control.snapshot() != self.proposal
                    or _baseline(self.arguments, self.proposal) != self.baseline
                    or _retained_files(self.archive, self.target, self.paths) != self.retained
                    or verify_stopped_runtime(**self.runtime_args) != self.runtime
                    or _file_facts(tuple(Path(path) for path in self.files)) != self.files
                    or {str(path): _identity(_io(path)) for path in self.directories} != self.directories):
                _fail('fresh_evidence_changed')
        except FreshEvidenceError: raise
        except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
                UnicodeError, OverflowError, RecursionError):
            _fail('fresh_evidence_changed')

    def read_liabilities(self):
        self.verify_current()
        path = self.paths['billing']
        try:
            _inactive_source(path)
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1',
                    uri=True, timeout=.2, isolation_level=None)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                _deadline(conn)
                conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024)
                databases = conn.execute('PRAGMA database_list').fetchall()
                if (len(databases) != 1 or databases[0][1] != 'main'
                        or conn.execute('SELECT 1 FROM temp.sqlite_master LIMIT 1').fetchone()):
                    _fail()
                rows = conn.execute('SELECT operation_id,key_tag,provider_request_id,provider_job_id,'
                    'status,reserved_micro,observed_cost_micro FROM main.billing_charges '
                    "WHERE status IN ('reserved','submitted','uncertain') ORDER BY operation_id LIMIT ?",
                    (_MAX_LIABILITIES + 1,)).fetchall()
                if len(rows) > _MAX_LIABILITIES: _fail('fresh_evidence_liabilities_bounded')
                result = tuple(_liability(row) for row in rows)
            self.verify_current()
            return result
        except FreshEvidenceError: raise
        except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
                UnicodeError, OverflowError, RecursionError):
            _fail()


def _text(value, *, optional=False):
    return (optional and value is None or type(value) is str and 0 < len(value) <= 256
        and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _liability(row):
    operation, key, request, job, status, reserved, cost = row
    if (not _text(operation) or str(UUID(operation)) != operation or not _text(key)
            or not _text(request, optional=True) or not _text(job, optional=True)
            or status not in ('reserved', 'submitted', 'uncertain')
            or type(reserved) is not int or not 0 <= reserved < 2**63
            or cost is not None and (type(cost) is not int or not 0 <= cost < 2**63)):
        _fail()
    return BillingLiability(*row)


def _file_facts(paths):
    return {str(path): dict(file_id=_identity(_io(path)), sha256=_hash(_io(path)))
        for path in sorted(set(paths))}


def _io(path):
    # Only an already validated ordinary drive-local path receives private
    # Win32 spelling. Public paths, commitments and SQLite URIs remain local.
    return Path(_win32_path(_local(path)))


@contextmanager
def prepared_restore_evidence(project_root, backup_dir, restore_dir, *, maintenance_path,
                              lifecycle_path):
    """Yield immutable diagnostics under actual existing-only Windows source custody."""
    observation = None
    try:
        arguments = dict(project_root=project_root, backup_dir=backup_dir, restore_dir=restore_dir,
            maintenance_path=maintenance_path, lifecycle_path=lifecycle_path)
        root, archive, target = (_safe(_local(arguments[key]))
            for key in ('project_root', 'backup_dir', 'restore_dir'))
        if (not all(path.is_dir() for path in (root, archive, target))
                or archive == target or archive.is_relative_to(target) or target.is_relative_to(archive)
                or archive == root or target == root
                or not archive.is_relative_to(root) or not target.is_relative_to(root)):
            _fail()
        with ExitStack() as stack:
            stack.enter_context(protected_scope(target))
            stack.enter_context(_pin_file(target / 'restore.json', deny_write=True))
            metadata = _read(target / 'restore.json', 4 * 1024 * 1024)
            restore_id = metadata['restore_id']
            if type(restore_id) is not str or str(UUID(restore_id)) != restore_id: _fail()
            anchor = root / '.runtime/team-operator'
            main_dir = anchor / 'activations' / restore_id
            for directory in (anchor, anchor / 'activations', main_dir):
                stack.enter_context(protected_scope(directory))
            ledger_path = main_dir / 'activation.sqlite3'
            stack.enter_context(_pin_file(ledger_path, deny_write=True))
            ledger = _locator(ledger_path)
            original = ledger.snapshot()
            if (original['state'] != 'epoch_prepared_blocked' or not original['preparation_complete']
                    or original['epoch'] is None): _fail('fresh_evidence_incomplete')
            epoch_id = original['epoch']['epoch_id']
            base = main_dir / 'source-epochs'
            directory = base / epoch_id
            for scope in (base, directory): stack.enter_context(protected_scope(scope))
            control_path = directory / 'source-epochs.sqlite3'
            stack.enter_context(_pin_file(control_path, deny_write=True))
            control = SourcePreparationControl.open_existing(control_path)
            proposal = control.snapshot()
            if (proposal['state'] != 'all_prepared_blocked'
                    or set(proposal['preparations']) != set(SOURCE_ROLES)):
                _fail('fresh_evidence_incomplete')
            baseline = _baseline(arguments, proposal)
            bound = proposal['binding']
            expected_sources = {row.role: dict(source_commitment_sha256=row.source_commitment_sha256,
                baseline_file_sha256=row.baseline_file_sha256, preparation_sha256=row.preparation_sha256)
                for row in baseline.sources}
            if (original['binding'] != baseline.ledger_binding or original['sources'] != expected_sources
                    or original['epoch']['scope_sha256'] != baseline.scope_sha256
                    or bound['ledger_id'] != original['ledger_id']
                    or bound['generation_id'] != original['generation_hold']['generation_id']
                    or bound['epoch_id'] != epoch_id
                    or tuple(bound['capabilities']) != tuple(original['epoch']['capabilities'])):
                _fail()
            paths = {row.role: Path(row.path) for row in baseline.sources}
            retained = _retained_files(archive, target, paths)
            r3_dir = anchor / 'restores' / restore_id
            for scope in (anchor / 'restores', r3_dir): stack.enter_context(protected_scope(scope))
            original_paths = tuple(_safe(_local(arguments[key]), file=True)
                for key in ('maintenance_path', 'lifecycle_path'))
            fixed_files = (ledger_path, control_path, r3_dir / 'control.sqlite3',
                r3_dir / 'inventory.json', *original_paths, *paths.values(), *(Path(raw) for raw in retained))
            directories = {root, archive, target, target / 'assets', anchor, anchor / 'restores', r3_dir,
                anchor / 'activations', main_dir, base, directory}
            for raw in retained:
                directories.update(parent for parent in Path(raw).parents
                    if parent.is_relative_to(archive) or parent.is_relative_to(target))
            for scope in sorted(directories):
                stack.enter_context(_pin_file(scope, directory=True))
            for path in sorted(set(fixed_files)):
                stack.enter_context(_pin_file(path, deny_write=True))
            runtime_args = dict(project_root=root, archive_manifest=_validate_archive(archive),
                maintenance_path=original_paths[0], lifecycle_path=original_paths[1])
            runtime = verify_stopped_runtime(**runtime_args)
            if (runtime.get('evidence_sha256') != baseline.runtime_evidence_sha256
                    or runtime.get('deployment_id') != bound['runtime_deployment_id']): _fail()
            file_facts = _file_facts(fixed_files)
            directory_facts = {str(path): _identity(_io(path)) for path in sorted(directories)}
            observation = _Observation(arguments, baseline, ledger, control, original, proposal,
                paths, archive, target, file_facts, directory_facts, retained, runtime_args, runtime)
            observation.verify_current()
            evidence = object.__new__(SourceEvidence)
            values = dict(snapshot_sha256=digest(dict(protocol=1, purpose='fresh_restore_source_observation',
                ledger=original, proposal=proposal, files=file_facts, directories=directory_facts,
                context=asdict(baseline), runtime=runtime)), restore_id=restore_id,
                epoch_id=epoch_id, preparation_id=proposal['preparation_id'], scope_sha256=baseline.scope_sha256,
                capabilities=tuple(bound['capabilities']), context_sha256=digest(asdict(baseline)),
                native_head_sha256=digest(proposal['preparations']['vikunja']['native_head']),
                binding_sha256=proposal['binding_sha256'],
                runtime_deployment_id=bound['runtime_deployment_id'],
                source_paths=tuple((role, str(paths[role])) for role in SOURCE_ROLES), _session=observation)
            for name, value in values.items(): object.__setattr__(evidence, name, value)
            body_error = None
            try:
                yield evidence
            except BaseException as error:
                body_error = error
            finally:
                try: observation.verify_current()
                finally: observation.active = False
            if body_error is not None: raise _CallerBodyError(body_error) from None
    except _CallerBodyError as error: raise error.error from None
    except FreshEvidenceError: raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
            UnicodeError, OverflowError, RecursionError):
        _fail()
    finally:
        if observation is not None: observation.active = False
