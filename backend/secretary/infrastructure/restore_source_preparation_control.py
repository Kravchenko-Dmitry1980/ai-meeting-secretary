"""Independent blocked source-epoch proposals, never an activation grant.

The explicit private local store retains the original R3 context, one proposed
epoch, authenticated operator receipt commitments and four durable source
receipt commitments. Typed approval DTOs do not themselves authenticate anyone;
orchestration must freshly authenticate the current operator against this exact
proposal before each new append. Fresh business-owner/provider evidence and
durable source readbacks remain separate requirements. Imports open no stores.

Private Windows custody protects against other accounts, not malicious code
already running as the current user. Time/size limits are cooperative bounds,
not a real-time SLA. A read never creates, migrates, repairs or recovers a store.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID, uuid4
from urllib.parse import quote

from .restore_operator import _win32_path


_MAX_DATABASE_BYTES = 8 * 1024 * 1024
_MAX_VALUE_BYTES = 64 * 1024
_MAX_SQL_OBJECTS = 64
_MAX_APPROVALS = 64
_TIMEOUT_SECONDS = .2
_READ_SECONDS = 1.0
_ROLES = ('secretary', 'team', 'billing', 'vikunja')
_CAPABILITIES = frozenset(('fresh_work', 'fresh_auth', 'sql_write', 'recovery',
    'native_runtime', 'polza_dispatch', 'vikunja_dispatch', 'max_dispatch'))
_BINDING_KEYS = frozenset(('protocol', 'context', 'ledger_id', 'generation_id',
    'epoch_id', 'capabilities', 'runtime_deployment_id', 'native_baseline'))
_CONTEXT_KEYS = frozenset(('protocol', 'restore_id', 'r3_id', 'r3_decision_sha256',
    'r3_binding_sha256', 'manifest_sha256', 'metadata_sha256', 'inventory_sha256',
    'source_context_sha256', 'scope_sha256', 'runtime_evidence_sha256', 'sources'))
_SOURCE_KEYS = frozenset(('role', 'path', 'file_id', 'baseline_file_sha256',
    'preparation_sha256', 'source_commitment_sha256'))
_SHA = re.compile('[0-9a-f]{64}')
_CODES = frozenset('source_preparation_' + suffix for suffix in (
    'binding_invalid', 'path_invalid', 'store_invalid', 'schema_invalid',
    'missing', 'exists', 'busy', 'io_failed', 'approval_invalid', 'approval_expired',
    'approval_conflict', 'approval_limit', 'receipt_invalid', 'receipt_conflict',
    'approval_required'))


class SourcePreparationControlError(ValueError):
    """Stable code only: caller text, SQL, paths and evidence never escape."""
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES else 'source_preparation_store_invalid'
        super().__init__(self.code)


class _CustodyBodyError(Exception):
    def __init__(self, error): self.error = error


def _fail(code='source_preparation_store_invalid'):
    raise SourcePreparationControlError(code) from None


def _is_sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _is_uuid(value):
    try:
        return type(value) is str and len(value) == 36 and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError): return False


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_canonical(value).encode('ascii')).hexdigest()


def _local_path(value):
    if not isinstance(value, (str, Path)): _fail('source_preparation_path_invalid')
    raw = str(value)
    normalized = raw.replace('/', '\\')
    components = re.split(r'[\\/]', raw)
    if (not 0 < len(raw) <= 4096 or normalized.startswith('\\\\')
            or any(ord(c) < 32 for c in raw)
            or any(p in ('.', '..') or p.rstrip(' .') != p
                for p in components if p)
            or any(':' in p or any(c in '<>"|?*' for c in p) for p in components[1:])
            or any(re.fullmatch(r'CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]',
                p.split('.')[0].upper()) is not None for p in components[1:] if p)
            or not Path(raw).is_absolute() or not Path(raw).name):
        _fail('source_preparation_path_invalid')
    return Path(raw)


def _binding(value):
    try:
        if (type(value) is not dict or set(value) != _BINDING_KEYS
                or type(value['protocol']) is not int or value['protocol'] != 1
                or any(not _is_uuid(value[key]) for key in
                    ('ledger_id', 'generation_id', 'epoch_id', 'runtime_deployment_id'))):
            _fail('source_preparation_binding_invalid')
        context = value['context']
        if (type(context) is not dict or set(context) != _CONTEXT_KEYS
                or type(context['protocol']) is not int or context['protocol'] != 1
                or any(not _is_uuid(context[key]) for key in ('restore_id', 'r3_id'))
                or any(not _is_sha(context[key]) for key in
                    _CONTEXT_KEYS - {'protocol', 'restore_id', 'r3_id', 'sources'})):
            _fail('source_preparation_binding_invalid')
        sources, paths, ids = context['sources'], set(), set()
        if type(sources) not in (tuple, list) or len(sources) != 4:
            _fail('source_preparation_binding_invalid')
        normalized_sources = []
        for role, item in zip(_ROLES, sources):
            if (type(item) is not dict or set(item) != _SOURCE_KEYS or item['role'] != role
                    or type(item['path']) is not str
                    or any(not _is_sha(item[key]) for key in
                        ('baseline_file_sha256', 'preparation_sha256', 'source_commitment_sha256'))):
                _fail('source_preparation_binding_invalid')
            path = _local_path(item['path'])
            identity = item['file_id']
            if (type(identity) not in (tuple, list) or len(identity) != 2
                    or any(type(x) is not int or not 0 <= x < 2**128 for x in identity)
                    or identity[1] == 0 or str(path).casefold() in paths or tuple(identity) in ids):
                _fail('source_preparation_binding_invalid')
            paths.add(str(path).casefold()); ids.add(tuple(identity))
            normalized_sources.append({**item, 'file_id': list(identity)})
        caps = value['capabilities']
        if (type(caps) not in (tuple, list) or not 0 < len(caps) <= len(_CAPABILITIES)
                or any(type(x) is not str or x not in _CAPABILITIES for x in caps)
                or tuple(caps) != tuple(sorted(set(caps)))):
            _fail('source_preparation_binding_invalid')
        native = value['native_baseline']
        if (type(native) is not dict or set(native) != {'schema_sha256', 'migration_ids', 'rowids_sha256'}
                or not _is_sha(native['schema_sha256']) or not _is_sha(native['rowids_sha256'])):
            _fail('source_preparation_binding_invalid')
        migrations = native['migration_ids']
        if (type(migrations) not in (tuple, list) or not 0 < len(migrations) <= 1024
                or any(type(x) is not str or not 0 < len(x) <= 256
                    or any(ord(c) < 32 or ord(c) > 126 for c in x) for x in migrations)
                or tuple(migrations) != tuple(sorted(set(migrations)))):
            _fail('source_preparation_binding_invalid')
        result = {**value, 'context': {**context, 'sources': normalized_sources},
            'capabilities': list(caps), 'native_baseline': {**native, 'migration_ids': list(migrations)}}
        if len(_canonical(result)) > _MAX_VALUE_BYTES: _fail('source_preparation_binding_invalid')
        return json.loads(_canonical(result))
    except (SourcePreparationControlError, ValueError, TypeError, UnicodeError, RecursionError):
        _fail('source_preparation_binding_invalid')


def _head(value, *, role, epoch_id):
    if role != 'vikunja':
        if value is not None: _fail('source_preparation_receipt_invalid')
        return None
    if (type(value) is not dict or set(value) != {'epoch_id', 'sequence', 'nonce', 'digest'}
            or value['epoch_id'] != epoch_id or type(value['sequence']) is not int
            or value['sequence'] != 0 or value['nonce'] is not None or not _is_sha(value['digest'])):
        _fail('source_preparation_receipt_invalid')
    return dict(value)


def _sha_column(name):
    return (f"{name} TEXT NOT NULL CHECK(typeof({name})='text' AND length({name})=64 "
        f"AND {name} NOT GLOB '*[^0-9a-f]*')")


_TABLES = {
    'source_proposal': (
        'CREATE TABLE source_proposal(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'preparation_id TEXT NOT NULL UNIQUE CHECK(length(preparation_id)=36),'
        'binding TEXT NOT NULL CHECK(json_valid(binding) AND length(binding)<=65536),'
        + _sha_column('binding_sha256') + ')'),
    'source_operator_approval': (
        'CREATE TABLE source_operator_approval(seq INTEGER PRIMARY KEY '
        "CHECK(typeof(seq)='integer' AND seq BETWEEN 1 AND 64),"
        + _sha_column('operator_digest') + ',' + _sha_column('approval_digest') + ' UNIQUE,'
        + _sha_column('nonce_hash') + ' UNIQUE,'
        "expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at>0))"),
    'source_preparations': (
        'CREATE TABLE source_preparations(role TEXT PRIMARY KEY NOT NULL '
        "CHECK(role IN ('secretary','team','billing','vikunja')),"
        + _sha_column('receipt_sha256') + ',' + _sha_column('file_sha256') + ','
        'native_head TEXT CHECK(native_head IS NULL OR '
        '(json_valid(native_head) AND length(native_head)<=1024)))'),
}


def _trigger(name, table, event, condition=None):
    when = f' WHEN {condition}' if condition else ''
    return (f'CREATE TRIGGER {name} BEFORE {event} ON {table}{when} '
        "BEGIN SELECT RAISE(ABORT,'source_preparation_immutable'); END")


_TRIGGERS = {}
for _table in _TABLES:
    for _event in ('update', 'delete'):
        _name = f'{_table}_immutable_{_event}'
        _TRIGGERS[_name] = (_table, _trigger(_name, _table, _event.upper()))
_TRIGGERS['source_proposal_replace_guard'] = ('source_proposal', _trigger(
    'source_proposal_replace_guard', 'source_proposal', 'INSERT', 'EXISTS(SELECT 1 FROM source_proposal)'))
_TRIGGERS['source_approval_replace_guard'] = ('source_operator_approval', _trigger(
    'source_approval_replace_guard', 'source_operator_approval', 'INSERT',
    'EXISTS(SELECT 1 FROM source_operator_approval WHERE seq=NEW.seq OR '
    'approval_digest=NEW.approval_digest OR nonce_hash=NEW.nonce_hash) OR '
    'NEW.seq!=(SELECT count(*)+1 FROM source_operator_approval)'))
_TRIGGERS['source_preparations_replace_guard'] = ('source_preparations', _trigger(
    'source_preparations_replace_guard', 'source_preparations', 'INSERT',
    'EXISTS(SELECT 1 FROM source_preparations WHERE role=NEW.role) OR '
    'NOT EXISTS(SELECT 1 FROM source_operator_approval)'))
_DDL = (*_TABLES.values(), *(item[1] for item in _TRIGGERS.values()))
_EXPECTED_SCHEMA = {
    **{name: ('table', name, sql) for name, sql in _TABLES.items()},
    **{name: ('trigger', table, sql) for name, (table, sql) in _TRIGGERS.items()},
    'sqlite_autoindex_source_proposal_1': ('index', 'source_proposal', None),
    'sqlite_autoindex_source_operator_approval_1': ('index', 'source_operator_approval', None),
    'sqlite_autoindex_source_operator_approval_2': ('index', 'source_operator_approval', None),
    'sqlite_autoindex_source_preparations_1': ('index', 'source_preparations', None),
}


class SourcePreparationControl:
    """Exclusive proposal creator and existing-only reader/writer; always OFF."""
    @classmethod
    def create(cls, path: Path, binding: dict):
        if binding is None: _fail('source_preparation_binding_invalid')
        instance = cls.__new__(cls)
        instance._setup(path, binding)
        with instance._custody():
            fd = instance._open_descriptor(create=True)
            with os.fdopen(fd, 'wb') as stream:
                stream.flush(); os.fsync(stream.fileno())
                instance._identity = instance._file_identity(os.fstat(stream.fileno()))
                # Windows CRT descriptor permits SQLite reads/writes while
                # denying deletion until the initialization commit has closed.
                instance._initialize()
        instance._open()
        return instance

    @classmethod
    def open_existing(cls, path: Path, binding: dict | None = None):
        instance = cls.__new__(cls)
        instance._setup(path, binding)
        instance._open()
        return instance

    def _setup(self, path, binding):
        self._binding = None if binding is None else _binding(binding)
        self._identity = None
        self._preparation_id = None
        if not isinstance(path, Path): _fail('source_preparation_path_invalid')
        self._path = _local_path(path)
        # Public binding/path remain ordinary drive-local names. Only private
        # native I/O spelling receives the prefix after all input rejection.
        self._io_path = Path(_win32_path(self._path))

    def _open_descriptor(self, *, create=False):
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self._io_path), 0xC0000000 if create else 0x80000000,
            3, None, 1 if create else 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if create and error in (80, 183): _fail('source_preparation_exists')
            if not create and error in (2, 3): _fail('source_preparation_missing')
            _fail('source_preparation_io_failed')
        try:
            return msvcrt.open_osfhandle(handle,
                (os.O_RDWR if create else os.O_RDONLY) | os.O_BINARY)
        except (OSError, ValueError):
            kernel.CloseHandle(handle)
            _fail('source_preparation_io_failed')

    def _sqlite_uri(self, *, write):
        # SQLite ships a dedicated Windows long-path VFS. The ordinary public
        # file URI keeps identity/guard checks tied to the requested companion;
        # the VFS performs its own internal Win32 prefix conversion.
        uri = self._path.as_uri() + ('?mode=rw' if write else '?mode=ro')
        if len(str(self._path)) > 248:
            # Encoding backslashes keeps URI authority empty. The filename
            # decoded by SQLite remains the validated internal drive spelling.
            uri = ('file:' + quote(str(self._io_path), safe='')
                + ('?mode=rw' if write else '?mode=ro') + '&vfs=win32-longpath')
        return uri

    @contextmanager
    def _custody(self):
        from .restore_operator import OperatorError, protected_scope
        try:
            with protected_scope(self._path.parent):
                try: yield
                except SourcePreparationControlError as error: raise _CustodyBodyError(error) from None
        except _CustodyBodyError as error: raise error.error from None
        except OperatorError: _fail('source_preparation_path_invalid')
        except OSError: _fail('source_preparation_io_failed')

    @staticmethod
    def _file_identity(info): return info.st_dev, info.st_ino

    @staticmethod
    def _unsafe(info):
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

    def _check_file(self):
        try:
            info = self._io_path.lstat()
            identity = self._file_identity(info)
            if (not stat.S_ISREG(info.st_mode) or self._unsafe(info) or info.st_nlink != 1
                    or not 100 <= info.st_size <= _MAX_DATABASE_BYTES
                    or self._identity is not None and identity != self._identity): _fail()
            for suffix in ('-wal', '-shm', '-journal'):
                try: Path(str(self._io_path) + suffix).lstat()
                except FileNotFoundError: continue
                _fail()
            with os.fdopen(self._open_descriptor(), 'rb') as stream:
                if self._file_identity(os.fstat(stream.fileno())) != identity: _fail()
                header = stream.read(32)
            if header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01': _fail()
            self._identity = identity
        except SourcePreparationControlError: raise
        except FileNotFoundError: _fail('source_preparation_missing')
        except OSError: _fail('source_preparation_io_failed')

    @staticmethod
    def _configure(conn, *, read_only):
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, _MAX_VALUE_BYTES)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA recursive_triggers=ON')
        if read_only: conn.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + _READ_SECONDS
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

    @contextmanager
    def _connect(self, *, write):
        with self._custody():
            self._check_file()
            conn = None
            try:
                # Parent custody alone does not deny child substitution. Keep
                # the original no-delete child descriptor through SQL close.
                with os.fdopen(self._open_descriptor(), 'rb') as stream:
                    if self._file_identity(os.fstat(stream.fileno())) != self._identity: _fail()
                    self._check_file()
                    try:
                        conn = sqlite3.connect(self._sqlite_uri(write=write),
                            uri=True, timeout=_TIMEOUT_SECONDS, isolation_level=None)
                        self._configure(conn, read_only=not write)
                        if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',): _fail()
                        yield conn
                    finally:
                        if conn is not None:
                            if conn.in_transaction: conn.rollback()
                            conn.close()
                            conn = None
                    self._check_file()
            except SourcePreparationControlError: raise
            except sqlite3.Error as error:
                _fail('source_preparation_busy' if getattr(error, 'sqlite_errorcode', None)
                    in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else 'source_preparation_store_invalid')
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                _fail('source_preparation_io_failed')
            finally:
                if conn is not None:
                    if conn.in_transaction: conn.rollback()
                    conn.close()
            self._check_file()

    def _check_initial_file(self, *, locked=False):
        info = self._io_path.lstat()
        if (self._identity is None or self._file_identity(info) != self._identity
                or not stat.S_ISREG(info.st_mode) or self._unsafe(info)
                or info.st_nlink != 1 or info.st_size != 0): _fail()
        for suffix in (('-wal', '-shm') if locked else ('-wal', '-shm', '-journal')):
            try: Path(str(self._io_path) + suffix).lstat()
            except FileNotFoundError: continue
            _fail()

    def _initialize(self):
        conn = None
        try:
            self._check_initial_file()
            conn = sqlite3.connect(self._sqlite_uri(write=True), uri=True,
                timeout=_TIMEOUT_SECONDS, isolation_level=None)
            self._configure(conn, read_only=False)
            if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',): _fail()
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN EXCLUSIVE')
            self._check_initial_file(locked=True)
            if conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone() is not None: _fail()
            for sql in _DDL: conn.execute(sql)
            conn.execute('INSERT INTO source_proposal VALUES(1,?,?,?)',
                (str(uuid4()), _canonical(self._binding), _digest(self._binding)))
            conn.commit()
        except SourcePreparationControlError: raise
        except (sqlite3.Error, OSError, ValueError, TypeError): _fail('source_preparation_io_failed')
        finally:
            if conn is not None:
                if conn.in_transaction: conn.rollback()
                conn.close()

    def _validate_schema(self, conn):
        objects = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master '
            'ORDER BY name LIMIT ?', (_MAX_SQL_OBJECTS + 1,)).fetchall()
        if len(objects) > _MAX_SQL_OBJECTS: _fail('source_preparation_schema_invalid')
        found = {}
        for kind, name, table, sql in objects:
            if (type(name) is not str or name in found
                    or _EXPECTED_SCHEMA.get(name) != (kind, table, sql)):
                _fail('source_preparation_schema_invalid')
            found[name] = (kind, table, sql)
        if found != _EXPECTED_SCHEMA: _fail('source_preparation_schema_invalid')

    def _validate(self, conn):
        self._validate_schema(conn)
        rows = conn.execute('SELECT singleton,preparation_id,binding,binding_sha256 '
            'FROM source_proposal LIMIT 2').fetchall()
        if len(rows) != 1: _fail()
        singleton, preparation_id, raw_binding, binding_sha = rows[0]
        if (type(singleton) is not int or singleton != 1 or not _is_uuid(preparation_id)
                or self._preparation_id is not None and self._preparation_id != preparation_id
                or type(raw_binding) is not str or not 0 < len(raw_binding) <= _MAX_VALUE_BYTES): _fail()
        bound = _binding(json.loads(raw_binding))
        if (raw_binding != _canonical(bound) or binding_sha != _digest(bound)
                or self._binding is not None and self._binding != bound): _fail()
        approvals = []
        seen_nonces, seen_digests = set(), set()
        rows = conn.execute('SELECT seq,operator_digest,approval_digest,nonce_hash,expires_at '
            'FROM source_operator_approval ORDER BY seq LIMIT 65').fetchall()
        if len(rows) > _MAX_APPROVALS: _fail()
        for index, (seq, operator, receipt, nonce, expires) in enumerate(rows, 1):
            if (type(seq) is not int or seq != index
                    or not all(_is_sha(value) for value in (operator, receipt, nonce))
                    or nonce in seen_nonces or receipt in seen_digests
                    or approvals and operator != approvals[0]['operator_digest']
                    or type(expires) is not int or not 0 < expires < 2**63): _fail()
            seen_nonces.add(nonce); seen_digests.add(receipt)
            approvals.append(dict(operator_digest=operator, approval_digest=receipt,
                nonce_hash=nonce, expires_at=expires))
        preparations = {}
        rows = conn.execute('SELECT role,receipt_sha256,file_sha256,native_head '
            'FROM source_preparations ORDER BY role LIMIT 5').fetchall()
        if len(rows) > 4 or rows and not approvals: _fail()
        for role, receipt, file_sha, raw_head in rows:
            if (type(role) is not str or role not in _ROLES or role in preparations
                    or not _is_sha(receipt) or not _is_sha(file_sha)
                    or raw_head is not None and (type(raw_head) is not str or len(raw_head) > 1024)): _fail()
            head = _head(None if raw_head is None else json.loads(raw_head), role=role, epoch_id=bound['epoch_id'])
            if raw_head != (None if head is None else _canonical(head)): _fail()
            preparations[role] = dict(receipt_sha256=receipt, file_sha256=file_sha, native_head=head)
        if conn.execute('PRAGMA foreign_key_check').fetchmany(1): _fail()
        return dict(protocol=1, preparation_id=preparation_id, binding=bound,
            binding_sha256=binding_sha, approval=approvals[-1] if approvals else None,
            approvals=tuple(approvals), preparations=preparations,
            missing_source_roles=tuple(role for role in _ROLES if role not in preparations),
            state='all_prepared_blocked' if len(preparations) == 4 else 'partial_prepared_blocked',
            activation_supported=False, outbound_enabled=False)

    def _open(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            snapshot = self._validate(conn)
        self._binding = snapshot['binding']
        self._preparation_id = snapshot['preparation_id']

    @contextmanager
    def _transaction(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN'); self._validate(conn)
        with self._connect(write=True) as conn:
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN IMMEDIATE')
            snapshot = self._validate(conn)
            yield conn, snapshot
            self._validate(conn)
            conn.commit()

    def snapshot(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            return self._validate(conn)

    @staticmethod
    def _fresh(receipt):
        now = int(time.time())
        if not now < receipt['expires_at'] <= now + 300: _fail('source_preparation_approval_expired')

    def consume_approval(self, verified):
        from .restore_operator import VerifiedOperatorApproval
        if type(verified) is not VerifiedOperatorApproval: _fail('source_preparation_approval_invalid')
        if (not all(_is_sha(value) for value in (verified.operator_digest, verified.approval_digest, verified.nonce_hash))
                or type(verified.expires_at) is not int or not 0 < verified.expires_at < 2**63):
            _fail('source_preparation_approval_invalid')
        record = dict(operator_digest=verified.operator_digest, approval_digest=verified.approval_digest,
            nonce_hash=verified.nonce_hash, expires_at=verified.expires_at)
        with self._transaction() as (conn, snapshot):
            for previous in snapshot['approvals']:
                if previous['nonce_hash'] == record['nonce_hash'] or previous['approval_digest'] == record['approval_digest']:
                    if previous != record: _fail('source_preparation_approval_conflict')
                    return
            if snapshot['approval'] is not None and snapshot['approval']['operator_digest'] != record['operator_digest']:
                _fail('source_preparation_approval_invalid')
            self._fresh(record)
            if len(snapshot['approvals']) >= _MAX_APPROVALS: _fail('source_preparation_approval_limit')
            conn.execute('INSERT INTO source_operator_approval VALUES(?,?,?,?,?)',
                (len(snapshot['approvals']) + 1, record['operator_digest'], record['approval_digest'],
                record['nonce_hash'], record['expires_at']))

    def record_preparation(self, role, receipt_sha256, file_sha256, native_head=None):
        if (type(role) is not str or role not in _ROLES
                or not _is_sha(receipt_sha256) or not _is_sha(file_sha256)):
            _fail('source_preparation_receipt_invalid')
        head = _head(native_head, role=role, epoch_id=self._binding['epoch_id'])
        record = dict(receipt_sha256=receipt_sha256, file_sha256=file_sha256, native_head=head)
        with self._transaction() as (conn, snapshot):
            previous = snapshot['preparations'].get(role)
            if previous is not None:
                if previous != record: _fail('source_preparation_receipt_conflict')
                return
            if snapshot['approval'] is None: _fail('source_preparation_approval_required')
            self._fresh(snapshot['approval'])
            conn.execute('INSERT INTO source_preparations VALUES(?,?,?,?)',
                (role, receipt_sha256, file_sha256, None if head is None else _canonical(head)))
