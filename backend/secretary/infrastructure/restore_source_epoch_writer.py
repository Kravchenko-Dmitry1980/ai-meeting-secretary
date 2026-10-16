"""Controlled R4 preparation of all four inactive sources, never activation.

The independent proposal precedes source commits. Original R3/V1 provenance is
retained; a crash resumes exact preparations, not a rebased/new epoch. Fresh
Windows operator consent authorizes local metadata only. Business-owner/provider
grants, final activation, advancing native watermark and work lineage are still
separate R4 requirements. No paths, proof DTOs or flags can grant admission here.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from secretary.domain.restore_quarantine import AUTHORITIES, SOURCE_ROLES, digest
from . import restore_activation_context as contexts
from .restore_activation_ledger import RestoreActivationLedger
from .restore_operator import OperatorAuthority, create_private_directory, protected_scope, _local, _win32_path
from .restore_runtime_evidence import verify_stopped_runtime
from .restore_source_preparation_control import SourcePreparationControl
from .restore_source_epoch import NewSourceEpochBinding, source_epoch_program, read_source_epoch
from .restore_native_history import NativeHistoryBinding, native_schema_descriptor, build_install_plan
from .restore_native_preparation import (
    NativePreparationBinding, prepare_native_source_in_connection, read_prepared_native,
    native_business_rowids, build_hold_plan,
)
from .restore_quarantine_preparation import read_preparation, _schema_objects, _business_snapshot
from .team_restore_reconciliation import (
    _read, _load_plan, _identity, _pin_file, _retained_files, _rowids, _deadline,
)
from .team_backup import _safe, _hash, _inactive_source, _validate_archive


class SourceEpochWriterError(ValueError):
    code = 'maintenance_restore_blocked'

    def __init__(self):
        super().__init__(self.code)


def _fail():
    raise SourceEpochWriterError() from None


def _canonical_equal(left, right):
    return digest(left) == digest(right)


def _sql_binding(snapshot, role, *, rowids):
    binding = snapshot['binding']
    context = binding['context']
    source = next(row for row in context['sources'] if row['role'] == role)
    return NewSourceEpochBinding(restore_id=context['restore_id'], r3_id=context['r3_id'],
        ledger_id=binding['ledger_id'], generation_id=binding['generation_id'],
        epoch_id=binding['epoch_id'], preparation_id=snapshot['preparation_id'],
        preparation_sha256=snapshot['binding_sha256'], role=role,
        source_commitment_sha256=source['source_commitment_sha256'],
        r3_preparation_sha256=source['preparation_sha256'],
        baseline_file_sha256=source['baseline_file_sha256'],
        source_path_sha256=digest(source['path']), file_id=tuple(source['file_id']),
        baseline_rowids_sha256=rowids)


def _native_binding(snapshot):
    binding, context = snapshot['binding'], snapshot['binding']['context']
    source = next(row for row in context['sources'] if row['role'] == 'vikunja')
    baseline = binding['native_baseline']
    history = NativeHistoryBinding(deployment_id=binding['runtime_deployment_id'],
        epoch_id=binding['epoch_id'], decision_id=snapshot['preparation_id'],
        decision_sha256=snapshot['binding_sha256'],
        expected_native_schema_sha256=baseline['schema_sha256'],
        expected_migration_ids=tuple(baseline['migration_ids']))
    return NativePreparationBinding(native=history, restore_id=context['restore_id'],
        r3_id=context['r3_id'], generation_id=binding['generation_id'], ledger_id=binding['ledger_id'],
        source_commitment_sha256=source['source_commitment_sha256'],
        r3_preparation_sha256=source['preparation_sha256'],
        baseline_file_sha256=source['baseline_file_sha256'], source_path_sha256=digest(source['path']),
        file_id=tuple(source['file_id']), baseline_rowids_sha256=baseline['rowids_sha256'])


def _native_business(conn, prepared, source):
    deadline = _deadline(conn)
    objects = _schema_objects(conn, deadline)
    exact = {(kind, name, table, sql) for name, kind, table, sql in prepared.metadata_schema}
    filtered = [row for row in objects if row not in exact]
    actual = _business_snapshot(conn, filtered, deadline, excluded_tables=prepared.metadata_tables)
    if actual != (source.schema_sha256, source.logical_sha256, source.tables, source.rows):
        _fail()


class _PreparedSourceObserver:
    """Fixed internal resume validator; public baseline reader cannot select it."""
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def observe(self, path, role, plan, envelope, r3, source,
                observed_hash, baseline_hash, r3_preparation_hash):
        context = self.snapshot['binding']['context']
        old = next(row for row in context['sources'] if row['role'] == role)
        if (old['path'] != str(path) or old['file_id'] != _identity(path)
                or old['baseline_file_sha256'] != baseline_hash
                or old['preparation_sha256'] != r3_preparation_hash
                or context['r3_id'] != r3['r3_id']):
            _fail()
        _inactive_source(path)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True,
                                    timeout=.2, isolation_level=None)) as conn:
            conn.execute('BEGIN')
            _deadline(conn)
            self.inspect(conn, role, plan, envelope, observed_hash)

    def inspect(self, conn, role, plan, envelope, observed_hash):
        record = self.snapshot['preparations'].get(role)
        source = next(row for row in self.snapshot['binding']['context']['sources'] if row['role'] == role)
        if record is not None and record['file_sha256'] != observed_hash:
            _fail()
        if role in AUTHORITIES:
            binding = _sql_binding(self.snapshot, role, rowids=envelope['rowids'][role])
            receipt = read_source_epoch(conn, binding=binding, quarantine_plan=plan)
            if receipt is None:
                actual = read_preparation(conn, plan=plan, authority=role)
                if (record is not None or observed_hash != source['baseline_file_sha256']
                        or actual is None or digest(asdict(actual)) != source['preparation_sha256']
                        or _rowids(conn) != envelope['rowids'][role]):
                    _fail()
                return None
            actual_hash = digest(asdict(receipt))
            if record is not None and record['receipt_sha256'] != actual_hash:
                _fail()
            return actual_hash, None
        binding = _native_binding(self.snapshot)
        known = conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'secretary_native_%' "
                             "OR name LIKE 'secretary_r4_native_%' LIMIT 1024").fetchall()
        if not known:
            if record is not None or observed_hash != source['baseline_file_sha256']:
                _fail()
            descriptor = native_schema_descriptor(conn)
            baseline = self.snapshot['binding']['native_baseline']
            if (descriptor.schema_sha256 != baseline['schema_sha256']
                    or tuple(descriptor.migration_ids) != tuple(baseline['migration_ids'])
                    or native_business_rowids(conn) != baseline['rowids_sha256']):
                _fail()
            return None
        prepared = read_prepared_native(conn, binding=binding)
        _native_business(conn, prepared, next(item for item in plan.input.sources if item.authority == role))
        head = asdict(prepared.native_snapshot.head)
        if record is not None and (record['receipt_sha256'] != prepared.receipt_sha256
                                  or not _canonical_equal(record['native_head'], head)):
            _fail()
        return prepared.receipt_sha256, head


def _baseline(arguments, snapshot=None):
    if snapshot is None:
        return contexts.collect_prepared_activation_context(**arguments)
    actual = contexts._collect(**arguments, _source_observer=_PreparedSourceObserver(snapshot))
    if not _canonical_equal(asdict(actual), snapshot['binding']['context']):
        _fail()
    return actual


def _locator(path):
    # Identification only; the strict ledger class and actual R3 context must
    # validate these rows before they can select the fixed companion location.
    _safe(path, file=True)
    _inactive_source(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True,
                                 timeout=.2, isolation_level=None)) as conn:
        _deadline(conn)
        row = conn.execute('SELECT binding FROM ledger_binding LIMIT 2').fetchall()
        if len(row) != 1 or type(row[0][0]) is not str or len(row[0][0]) > 32768:
            _fail()
        binding = json.loads(row[0][0])
    return RestoreActivationLedger.open_existing(path, binding)


def prepare_source_epochs(project_root, backup_dir, restore_dir, *, maintenance_path,
                          lifecycle_path, reader=input, writer=print):
    """Explicit interactive local preparation; never starts native or providers."""
    try:
        arguments = dict(project_root=project_root, backup_dir=backup_dir, restore_dir=restore_dir,
            maintenance_path=maintenance_path, lifecycle_path=lifecycle_path)
        return _prepare(arguments, reader=reader, writer=writer)
    except SourceEpochWriterError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError, sqlite3.Error,
            UnicodeError, OverflowError, RecursionError):
        _fail()


def _prepare(arguments, *, reader, writer):
    root, archive, target = (_safe(_local(arguments[key])) for key in ('project_root', 'backup_dir', 'restore_dir'))
    metadata = _read(target / 'restore.json')
    restore_id = metadata['restore_id']
    if type(restore_id) is not str or str(UUID(restore_id)) != restore_id:
        _fail()
    operator = OperatorAuthority(root)
    anchor = root / '.runtime/team-operator'
    if operator.anchor != anchor:
        _fail()
    main_dir = anchor / 'activations' / restore_id
    main_path = main_dir / 'activation.sqlite3'
    with ExitStack() as stack:
        for directory in (target, anchor, anchor / 'activations', main_dir):
            stack.enter_context(protected_scope(directory))
        stack.enter_context(_pin_file(main_path, deny_write=True))
        ledger = _locator(main_path)
        original_ledger = ledger.snapshot()
        if (original_ledger['epoch'] is None or not original_ledger['preparation_complete']
                or original_ledger['state'] != 'epoch_prepared_blocked'):
            _fail()
        epoch_id = original_ledger['epoch']['epoch_id']
        base, directory = main_dir / 'source-epochs', main_dir / 'source-epochs' / epoch_id
        path = directory / 'source-epochs.sqlite3'
        control = SourcePreparationControl.open_existing(path) if os.path.lexists(_win32_path(path)) else None
        snapshot = control.snapshot() if control is not None else None
        bound_operator_digest = operator.operator_digest
        baseline = _baseline(arguments, snapshot)
        if operator.operator_digest != bound_operator_digest:
            _fail()
        expected_sources = {row.role: dict(source_commitment_sha256=row.source_commitment_sha256,
            baseline_file_sha256=row.baseline_file_sha256, preparation_sha256=row.preparation_sha256)
            for row in baseline.sources}
        if (original_ledger['binding'] != baseline.ledger_binding
                or original_ledger['sources'] != expected_sources
                or original_ledger['epoch']['scope_sha256'] != baseline.scope_sha256):
            _fail()
        paths = {row.role: Path(row.path) for row in baseline.sources}
        envelope = _read(anchor / 'restores' / restore_id / 'inventory.json')
        plan = _load_plan(envelope['plan'])
        manifest = _validate_archive(archive)
        runtime_args = dict(project_root=root, archive_manifest=manifest,
            maintenance_path=_safe(_local(arguments['maintenance_path']), file=True),
            lifecycle_path=_safe(_local(arguments['lifecycle_path']), file=True))
        proof = verify_stopped_runtime(**runtime_args)
        if proof['evidence_sha256'] != baseline.runtime_evidence_sha256:
            _fail()
        retained = _retained_files(archive, target, paths)
        retained_dirs = {archive, target / 'assets', anchor / 'restores', anchor / 'restores' / restore_id}
        for raw in retained:
            retained_dirs.update(parent for parent in Path(raw).parents
                if parent.is_relative_to(archive) or parent.is_relative_to(target))
        for directory_path in sorted(retained_dirs):
            stack.enter_context(_pin_file(directory_path, directory=True))
        for raw in sorted(retained):
            stack.enter_context(_pin_file(Path(raw), deny_write=True))
        for retained_path in (anchor / 'restores' / restore_id / 'inventory.json',
                              anchor / 'restores' / restore_id / 'control.sqlite3',
                              runtime_args['maintenance_path'], runtime_args['lifecycle_path']):
            stack.enter_context(_pin_file(retained_path, deny_write=True))
        for source_path in paths.values():
            stack.enter_context(_pin_file(source_path))
            _inactive_source(source_path)
        connections = {}
        for role in SOURCE_ROLES:
            conn = stack.enter_context(closing(sqlite3.connect(paths[role].as_uri() + '?mode=rw',
                uri=True, isolation_level=None, timeout=.2)))
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN EXCLUSIVE')
            _deadline(conn)
            connections[role] = conn
            stack.callback(lambda c=conn: c.rollback() if c in connections.values() and c.in_transaction else None)
        if control is None:
            native = connections['vikunja']
            descriptor = native_schema_descriptor(native)
            native_baseline = dict(schema_sha256=descriptor.schema_sha256,
                migration_ids=descriptor.migration_ids, rowids_sha256=native_business_rowids(native))
            binding = dict(protocol=1, context=asdict(baseline), ledger_id=original_ledger['ledger_id'],
                generation_id=original_ledger['generation_hold']['generation_id'], epoch_id=epoch_id,
                capabilities=original_ledger['epoch']['capabilities'],
                runtime_deployment_id=proof['deployment_id'], native_baseline=native_baseline)
            created = False
            for directory_path in (base, directory):
                if not os.path.lexists(_win32_path(directory_path)):
                    create_private_directory(directory_path)
                    if directory_path == directory: created = True
                stack.enter_context(protected_scope(directory_path))
            if not created:
                _fail()
            control = SourcePreparationControl.create(path, binding)
            snapshot = control.snapshot()
        else:
            for directory_path in (base, directory):
                stack.enter_context(protected_scope(directory_path))
            expected = dict(protocol=1, context=asdict(baseline), ledger_id=original_ledger['ledger_id'],
                generation_id=original_ledger['generation_hold']['generation_id'], epoch_id=epoch_id,
                capabilities=original_ledger['epoch']['capabilities'], runtime_deployment_id=proof['deployment_id'],
                native_baseline=snapshot['binding']['native_baseline'])
            if not _canonical_equal(snapshot['binding'], expected):
                _fail()
        stack.enter_context(_pin_file(path))
        observer = _PreparedSourceObserver(snapshot)
        for role, conn in connections.items():
            observer.inspect(conn, role, plan, envelope, _hash(paths[role]))
        if (_retained_files(archive, target, paths) != retained
                or digest(envelope) != baseline.inventory_sha256
                or verify_stopped_runtime(**runtime_args) != proof):
            _fail()
        commitment = dict(protocol=1, purpose='source_epoch_preparation',
            preparation_id=snapshot['preparation_id'], binding_sha256=snapshot['binding_sha256'],
            binding=snapshot['binding'])
        approval = operator.issue(commitment, reader=reader, writer=writer)
        def authenticate():
            verified = operator.authenticate(commitment, approval)
            if verified.operator_digest != bound_operator_digest:
                _fail()
            return verified
        control.consume_approval(authenticate())
        for role in SOURCE_ROLES:
            conn, source_path = connections[role], paths[role]
            _deadline(conn)
            observed = observer.inspect(conn, role, plan, envelope, _hash(source_path))
            authenticate()
            if observed is None:
                if role == 'vikunja':
                    native_binding = _native_binding(snapshot)
                    history_plan = build_install_plan(native_binding.native)
                    hold_plan = build_hold_plan(native_binding)
                    # Normalize the native record order to the source SQL
                    # program's (type,name,target,SQL) closed schema order.
                    schema = tuple((kind, name, table, sql) for name, kind, table, sql
                        in (*history_plan.protocol_schema, *hold_plan.protocol_schema))
                    conn.set_authorizer(_metadata_authorizer(schema))
                    try:
                        prepare_native_source_in_connection(conn, binding=native_binding)
                    finally:
                        conn.set_authorizer(None)
                else:
                    binding = _sql_binding(snapshot, role, rowids=envelope['rowids'][role])
                    program = source_epoch_program(binding)
                    conn.set_authorizer(_metadata_authorizer(program.protocol_schema))
                    try:
                        for statement in program.statements:
                            conn.execute(statement.sql, statement.params)
                        read_source_epoch(conn, binding=binding, quarantine_plan=plan)
                    finally:
                        conn.set_authorizer(None)
                authenticate()
                conn.commit()
            if conn.in_transaction: conn.rollback()
            conn.close()
            connections.pop(role)
            stack.enter_context(_pin_file(source_path, deny_write=True))
            _inactive_source(source_path)
            durable = stack.enter_context(closing(sqlite3.connect(source_path.as_uri() + '?mode=ro&immutable=1',
                uri=True, isolation_level=None, timeout=.2)))
            durable.execute('BEGIN')
            _deadline(durable)
            connections[role] = durable
            current = observer.inspect(durable, role, plan, envelope, _hash(source_path))
            if current is None:
                _fail()
            receipt_hash, head = current
            authenticate()
            control.record_preparation(role, receipt_hash, _hash(source_path), native_head=head)
            if verify_stopped_runtime(**runtime_args) != proof:
                _fail()
        final = control.snapshot()
        final_observer = _PreparedSourceObserver(final)
        for role in SOURCE_ROLES:
            _inactive_source(paths[role])
            _deadline(connections[role])
            final_observer.inspect(connections[role], role, plan, envelope, _hash(paths[role]))
        if (final['state'] != 'all_prepared_blocked' or len(final['preparations']) != 4
                or _retained_files(archive, target, paths) != retained
                or _read(anchor / 'restores' / restore_id / 'inventory.json') != envelope
                or ledger.snapshot() != original_ledger
                or verify_stopped_runtime(**runtime_args) != proof):
            _fail()
        authenticate()
        # Reconstruct authoritative immutable R3 context again after all four
        # independent appends, using actual closed preparation readers.
        if _baseline(arguments, final) != baseline:
            _fail()
        return final


def _metadata_authorizer(schema):
    objects = {name: (kind, table, sql) for kind, name, table, sql in schema}
    tables = {name for name, (kind, _, _) in objects.items() if kind == 'table'}
    def authorize(action, arg1, arg2, database, trigger):
        # The native closed reader uses SELECT 1 FROM temp.sqlite_master to
        # reject aliases before any unqualified main protocol/business reads.
        # Allow that catalog existence read only; TEMP work remains denied.
        if (action == sqlite3.SQLITE_READ and arg1 == 'sqlite_master'
                and arg2 == '' and database == 'temp' and trigger is None):
            return sqlite3.SQLITE_OK
        if database not in (None, 'main'):
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            return sqlite3.SQLITE_OK if arg1 in (*tables, 'sqlite_master') else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_CREATE_TABLE:
            return sqlite3.SQLITE_OK if arg1 in tables else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_CREATE_INDEX:
            return sqlite3.SQLITE_OK if arg2 in tables else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_CREATE_TRIGGER:
            return sqlite3.SQLITE_OK if arg1 in objects and objects[arg1][:2] == ('trigger', arg2) else sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                      sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and arg2 is None:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return authorize
