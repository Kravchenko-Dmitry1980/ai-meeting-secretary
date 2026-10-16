"""Explicit R3 preparation of inactive copies. No activation or external replay.

Three SQLite commits are intentionally separate. Independent control records a
last complete decision only after durable readback. Every failure retains the
original guards. Same-user local code is trusted; this is not a hostile-host
sandbox or a census of every process on Windows.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
import ctypes
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from secretary.domain.restore_quarantine import (
    AUTHORITIES, SOURCE_ROLES, Origin, QuarantineEntry, QuarantineInput,
    QuarantineLink, QuarantinePlan, RestoreQuarantineError, SourceWatermark,
    digest, encoded, typed,
)
from .restore_operator import OperatorAuthority, protected_scope, create_private_directory, _win32_path
from .restore_reconciliation_control import RestoreControl
from .restore_runtime_evidence import verify_stopped_runtime
from .restore_quarantine_preparation import (
    preparation_program, read_preparation, validate_preparation_bounds, PROTOCOL_TABLES,
)
from .team_backup import BackupError, _safe, _hash, _inactive_source, _validate_archive
from .team_restore_preview import reconciliation_preview
from .team_restore_quarantine import collect_quarantine, _catalogue_hash
from .restore_quarantine_json import JSON_RULES_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAX_INVENTORY_BYTES = 128 * 1024 * 1024
MAX_ROWS = 100000
MAX_SECONDS = 30


class ReconciliationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise ReconciliationError('restore_reconciliation_' + code)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value: _fail('document_invalid')
        value[key] = item
    return value


def _read(path, limit=MAX_INVENTORY_BYTES):
    path = _safe(path, file=True)
    if path.stat().st_size > limit: _fail('document_invalid')
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda value: _fail('document_invalid'))
    except (UnicodeError, TypeError, ValueError, RecursionError):
        _fail('document_invalid')


def _write_new(path, value):
    raw = encoded(value).encode('utf-8')
    if len(raw) > MAX_INVENTORY_BYTES: _fail('inventory_too_large')
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _load_plan(value):
    try:
        if set(value) != {'input', 'entries', 'links', 'family_counts'}: _fail('inventory_invalid')
        source = dict(value['input'])
        source['sources'] = tuple(SourceWatermark(**row) for row in source['sources'])
        entries = tuple(QuarantineEntry(Origin(**row['origin']), row['row_sha256'],
            row['state_sha256'], row['disposition']) for row in value['entries'])
        if any(set(row) != {'origin','row_sha256','state_sha256','disposition'} for row in value['entries']):
            _fail('inventory_invalid')
        links = tuple(QuarantineLink(Origin(**row['child']), Origin(**row['parent']),
            row['relation']) for row in value['links'])
        if any(set(row) != {'child','parent','relation'} for row in value['links']): _fail('inventory_invalid')
        plan = QuarantinePlan(QuarantineInput(**source), entries, links,
                              tuple(tuple(row) for row in value['family_counts']))
        validate_preparation_bounds(plan)
        return plan
    except (TypeError, KeyError, RestoreQuarantineError, RecursionError):
        _fail('inventory_invalid')


def _identity(path):
    info = _safe(path).stat()
    return [info.st_dev, info.st_ino]


@contextmanager
def _pin_file(path, *, deny_write=False, directory=False):
    """Retain a no-delete Windows handle; SQLite provides writer exclusion."""
    native_path = _win32_path(path)
    native_observer = Path(native_path)
    before = _identity(_safe(native_observer, file=not directory))
    if os.name != 'nt': _fail('windows_required')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
        ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = kernel.CreateFileW(native_path, 0x80000000, 1 if deny_write else 3, None, 3, flags, None)
    if handle in (None, ctypes.c_void_p(-1).value): _fail('file_busy')
    try:
        if _identity(native_observer) != before: _fail('scope_changed')
        yield
        if _identity(native_observer) != before: _fail('scope_changed')
    finally:
        kernel.CloseHandle(handle)


def _retained_files(archive, target, paths):
    output = {}
    sqlite_files = {candidate for path in paths.values()
                    for candidate in (path,*(Path(str(path)+suffix) for suffix in ('-wal','-shm','-journal')))}
    for root in (archive, target / 'assets'):
        for path in sorted(root.rglob('*')):
            _safe(path)
            if path.is_file() and path not in sqlite_files:
                if len(output) >= 100000: _fail('inventory_too_large')
                output[str(path)] = _hash(path)
    output[str(target / 'restore.json')] = _hash(target / 'restore.json')
    return output


def _rowids(conn):
    """Preserve hidden row IDs/order, which R2 logical multiset hashes omit."""
    _deadline(conn)
    rows = size = 0
    value = hashlib.sha256()
    tables = sorted((row[1],row[4]) for row in conn.execute('PRAGMA table_list')
                    if row[0] == 'main' and row[2] == 'table'
                    and row[1] not in (*PROTOCOL_TABLES, 'sqlite_schema'))
    if len(tables) > 512: _fail('inventory_too_large')
    for table, without_rowid in tables:
        if without_rowid: continue  # R2 verifies the complete PK logical multiset.
        value.update(encoded(table).encode())
        name = '"' + table.replace('"','""') + '"'
        for row in conn.execute('SELECT _rowid_,* FROM ' + name + ' ORDER BY _rowid_'):
            raw = encoded(typed(row)).encode()
            rows += 1; size += len(raw)
            if rows > MAX_ROWS or size > 64 * 1024 * 1024: _fail('inventory_too_large')
            value.update(len(raw).to_bytes(8,'big')); value.update(raw)
    return value.hexdigest()


def _deadline(conn):
    deadline = time.monotonic() + MAX_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return deadline


def _initial_rowids(paths,plan):
    output = {}
    for role in AUTHORITIES:
        path = paths[role]
        source = next(item for item in plan.input.sources if item.authority == role)
        _inactive_source(path)
        if _hash(path) != source.file_sha256: _fail('input_changed')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as conn:
            output[role] = _rowids(conn)
        _inactive_source(path)
        if _hash(path) != source.file_sha256: _fail('input_changed')
    return output


def _authorizer(action, arg1, arg2, database, trigger):
    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_OK if arg1 in (*PROTOCOL_TABLES, 'sqlite_master') else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_CREATE_TABLE:
        return sqlite3.SQLITE_OK if arg1 in PROTOCOL_TABLES else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_CREATE_INDEX:
        return sqlite3.SQLITE_OK if arg2 in PROTOCOL_TABLES else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_CREATE_TRIGGER:
        return sqlite3.SQLITE_OK if arg1.startswith('restore_quarantine_') and arg2 in PROTOCOL_TABLES else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,
                  sqlite3.SQLITE_TRANSACTION,sqlite3.SQLITE_RECURSIVE): return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg2 is None: return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _durable(conn, path, plan, role, r3_id, envelope, control, stack, connections):
    sql_deadline = _deadline(conn)
    existing = read_preparation(conn, plan=plan, authority=role)
    if _rowids(conn) != envelope['rowids'][role]: _fail('rowids_changed')
    if existing is None:
        original = next(source for source in plan.input.sources if source.authority == role)
        if _hash(path) != original.file_sha256: _fail('input_changed')
        program = preparation_program(plan, role, prepared_by_R3_id=r3_id)
        conn.set_authorizer(_authorizer)
        try:
            for statement in program.statements:
                if time.monotonic() >= sql_deadline: _fail('sql_deadline')
                conn.execute(statement.sql, statement.params)
            if time.monotonic() >= sql_deadline: _fail('sql_deadline')
            existing = read_preparation(conn, plan=plan, authority=role)
            conn.commit()
        finally:
            conn.set_authorizer(None)
    if existing is None or existing.prepared_by_R3_id != r3_id: _fail('receipt_invalid')
    # Closing the owned writer lets SQLite perform its own WAL checkpoint and
    # cleanup. Never delete a journal or silently change journal_mode. If another
    # connection leaves a sidecar or retains write access, preparation holds.
    if conn.in_transaction: conn.rollback()
    conn.close()
    connections.pop(role)
    stack.enter_context(_pin_file(path,deny_write=True))
    _inactive_source(path)
    observed = stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)))
    connections[role] = observed
    _deadline(observed)
    actual = read_preparation(observed, plan=plan, authority=role)
    if actual != existing or _rowids(observed) != envelope['rowids'][role]: _fail('receipt_invalid')
    control.record_preparation(role, digest(asdict(actual)), _hash(path))


def prepare_restoration(backup_dir, restore_dir, *, maintenance_path, lifecycle_path,
                        reader=input, writer=print):
    """Interactive local operator action. It only seals deny dispositions."""
    try:
        return _prepare(backup_dir,restore_dir,maintenance_path=maintenance_path,
                        lifecycle_path=lifecycle_path,reader=reader,writer=writer)
    except (BackupError,RestoreQuarantineError,sqlite3.Error,OSError) as error:
        # Never include a SQLite statement, filesystem payload or raw OS error.
        _fail('validation_failed')


def _prepare(backup_dir, restore_dir, *, maintenance_path, lifecycle_path, reader, writer):
    root = _safe(PROJECT_ROOT)
    archive, target = _safe(backup_dir), _safe(restore_dir)
    if (archive == target or archive.is_relative_to(target) or target.is_relative_to(archive)
            or not archive.is_relative_to(root) or not target.is_relative_to(root)): _fail('scope_invalid')
    metadata = _read(target / 'restore.json', 4 * 1024 * 1024)
    if not isinstance(metadata,dict) or set(metadata.get('databases', {})) != set(SOURCE_ROLES): _fail('metadata_invalid')
    paths = {role: _safe(metadata['databases'][role],file=True) for role in SOURCE_ROLES}
    if len(set(paths.values())) != 4 or any(not path.is_relative_to(target) for path in paths.values()): _fail('scope_invalid')
    original = [_safe(maintenance_path,file=True), _safe(lifecycle_path,file=True)]
    if any(not path.is_relative_to(root) or path.is_relative_to(target) or path.is_relative_to(archive)
           for path in original): _fail('scope_invalid')
    operator = OperatorAuthority(root)
    restore_id = metadata.get('restore_id')
    from uuid import UUID
    if not isinstance(restore_id,str) or str(UUID(restore_id)) != restore_id: _fail('metadata_invalid')
    base = operator.anchor / 'restores'
    directory = base / restore_id
    if any(path.is_relative_to(operator.anchor) for path in (*paths.values(), archive, target)): _fail('scope_invalid')
    with ExitStack() as stack:
        stack.enter_context(protected_scope(target))
        stack.enter_context(protected_scope(operator.anchor))
        for path in (*paths.values(), *original, archive/'manifest.json',target/'restore.json'):
            stack.enter_context(_pin_file(path))
        for path in (base,directory):
            if not path.exists(): create_private_directory(path)
            stack.enter_context(protected_scope(path))
        manifest = _validate_archive(archive)
        runtime_args = dict(project_root=root,archive_manifest=manifest,
                            maintenance_path=original[0],lifecycle_path=original[1])
        evidence = verify_stopped_runtime(**runtime_args)
        retained_initial = _retained_files(archive,target,paths)
        # Retained inputs are not writable SQL authorities. Keep real OS
        # write/delete-denial leases AND their directory identities through the
        # final decision; neither an earlier hash nor final recheck closes this
        # last gap on its own. The handles carry no activation capability.
        retained_dirs = {archive,target/'assets'}
        for raw in retained_initial:
            path = Path(raw)
            retained_dirs.update(parent for parent in path.parents
                                 if parent.is_relative_to(archive) or parent.is_relative_to(target))
        for directory_path in sorted(retained_dirs):
            stack.enter_context(_pin_file(directory_path,directory=True))
        for raw in sorted(retained_initial):
            stack.enter_context(_pin_file(Path(raw),deny_write=True))
        if _retained_files(archive,target,paths) != retained_initial: _fail('input_changed')
        # Actual native OS write-denial lease also supports an inactive WAL
        # header; its immutable observer cannot create WAL/SHM or mutate data.
        _inactive_source(paths['vikunja'])
        stack.enter_context(_pin_file(paths['vikunja'],deny_write=True))
        scope = {'root':str(root),'root_id':_identity(root),'archive':str(archive),
                 'archive_id':_identity(archive),'target':str(target),'target_id':_identity(target),
                 'files':{role:[str(path),_identity(path)] for role,path in paths.items()},
                 'runtime': [str(path) for path in original]}
        inventory = directory/'inventory.json'
        control_path = directory/'control.sqlite3'
        if inventory.exists():
            envelope = _read(inventory)
            if not isinstance(envelope,dict) or set(envelope) != {'version','scope','plan','rowids','retained','runtime_evidence_sha256'}:
                _fail('inventory_invalid')
            if envelope['version'] != 1 or envelope['scope'] != scope: _fail('scope_changed')
            plan = _load_plan(envelope['plan'])
            if not control_path.exists():
                if any(_hash(paths[source.authority]) != source.file_sha256 for source in plan.input.sources): _fail('control_missing')
                preview = reconciliation_preview(archive,target)
                if collect_quarantine(archive,target,expected_preview_sha256=preview['preview_sha256']) != plan:
                    _fail('inventory_invalid')
        else:
            preview = reconciliation_preview(archive,target)
            plan = collect_quarantine(archive,target,expected_preview_sha256=preview['preview_sha256'])
            if _retained_files(archive,target,paths) != retained_initial: _fail('input_changed')
            envelope = None
        initial_rowids = envelope['rowids'] if envelope is not None else _initial_rowids(paths,plan)
        connections = {}
        for role in AUTHORITIES:
            path = paths[role]
            _inactive_source(path)
            conn = stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,
                                                              isolation_level=None,timeout=.2)))
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN EXCLUSIVE')
            stack.callback(lambda c=conn: c.rollback() if any(c is item for item in connections.values()) else None)
            _deadline(conn)
            connections[role] = conn
            # Revalidate the full business snapshot after acquiring ALL locks
            # below; source files alone cannot represent uncheckpointed WAL.
        for role in AUTHORITIES:
            _deadline(connections[role])
            receipt = read_preparation(connections[role],plan=plan,authority=role)
            source = next(item for item in plan.input.sources if item.authority == role)
            if receipt is None and _hash(paths[role]) != source.file_sha256: _fail('input_changed')
            if _rowids(connections[role]) != initial_rowids[role]: _fail('rowids_changed')
        if envelope is None:
            if _retained_files(archive,target,paths) != retained_initial: _fail('input_changed')
            envelope = {'version':1,'scope':scope,'plan':asdict(plan),
                        'rowids':initial_rowids,
                        'retained':retained_initial,
                        'runtime_evidence_sha256':evidence['evidence_sha256']}
            # Last immutable filename is published without overwrite. A failed
            # or partial write is preserved and rejected on the next invocation.
            _write_new(inventory,envelope)
        if plan.input.restore_id != restore_id or plan.input.manifest_sha256 != _hash(archive/'manifest.json'):
            _fail('input_changed')
        if plan.input.catalogue_sha256 != _catalogue_hash(JSON_RULES_VERSION): _fail('catalogue_changed')
        if (envelope['retained'] != _retained_files(archive,target,paths)
                or envelope['runtime_evidence_sha256'] != evidence['evidence_sha256']): _fail('input_changed')
        native = next(source for source in plan.input.sources if source.authority == 'vikunja')
        if _hash(paths['vikunja']) != native.file_sha256: _fail('input_changed')
        binding = {'protocol':1,'restore_id':restore_id,'plan_sha256':plan.plan_sha256,
                   'input_sha256':plan.input.input_sha256,'scope_sha256':digest(scope),
                   'evidence_sha256':digest(envelope),'source_paths_sha256':digest({r:str(p) for r,p in paths.items()}),
                   'operator_authority_sha256':operator.operator_digest}
        control = RestoreControl(control_path,binding)
        previous = control.snapshot()
        if previous['decision'] is not None:
            for role in AUTHORITIES:
                _deadline(connections[role])
                receipt = read_preparation(connections[role],plan=plan,authority=role)
                record = previous['preparations'].get(role)
                if (receipt is None or receipt.prepared_by_R3_id != control.r3_id or record is None
                        or record != {'receipt_sha256':digest(asdict(receipt)),'file_sha256':_hash(paths[role])}
                        or _rowids(connections[role]) != envelope['rowids'][role]): _fail('receipt_invalid')
            if verify_stopped_runtime(**runtime_args)['evidence_sha256'] != evidence['evidence_sha256']: _fail('runtime_changed')
            return previous['decision']
        approval = operator.issue(binding,reader=reader,writer=writer)
        verified = operator.authenticate(binding,approval)
        control.consume_approval(verified)
        for role in AUTHORITIES:
            _durable(connections[role],paths[role],plan,role,control.r3_id,envelope,control,stack,connections)
        final = control.snapshot()
        for role in AUTHORITIES:
            _deadline(connections[role])
            receipt = read_preparation(connections[role],plan=plan,authority=role)
            if (receipt is None or final['preparations'][role] !=
                    {'receipt_sha256':digest(asdict(receipt)),'file_sha256':_hash(paths[role])}
                    or _rowids(connections[role]) != envelope['rowids'][role]): _fail('receipt_invalid')
        if verify_stopped_runtime(**runtime_args)['evidence_sha256'] != evidence['evidence_sha256']: _fail('runtime_changed')
        if _retained_files(archive,target,paths) != envelope['retained'] or _hash(paths['vikunja']) != native.file_sha256:
            _fail('input_changed')
        operator.authenticate(binding,approval)  # Current principal/scope/MAC/expiry, not a cached DTO.
        return control.complete(approval_digest=verified.approval_digest,native_file_sha256=native.file_sha256)
