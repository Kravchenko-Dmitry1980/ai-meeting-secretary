"""Independent, append-only R4 preparation ledger; never an activation grant.

Bindings and source hashes are commitments, not owner or provider evidence. This
module opens only its explicitly supplied local ledger. Fresh owner/provider
verification, source-native write preparation, runtime gating and legacy lineage
exclusion belong to the later full R4 orchestration. Four receipts remain blocked.

The Windows parent must already have current-user private custody. SQLite locks
wait at most 0.2 seconds and reads have cooperative bounds, not a real-time SLA.
Custody does not sandbox malicious code already running as the same user.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID, uuid4


_MAX_DATABASE_BYTES = 8 * 1024 * 1024
_MAX_SQL_OBJECTS = 64
_MAX_VALUE_BYTES = 16 * 1024
_TIMEOUT_SECONDS = 0.2
_READ_SECONDS = 1.0
_ROLES = ('secretary', 'team', 'billing', 'vikunja')
_CAPABILITIES = frozenset(('fresh_work', 'fresh_auth', 'sql_write', 'recovery',
    'native_runtime', 'polza_dispatch', 'vikunja_dispatch', 'max_dispatch'))
_BINDING_KEYS = frozenset(('protocol', 'restore_id', 'r3_id', 'r3_decision_sha256',
    'r3_binding_sha256', 'manifest_sha256', 'metadata_sha256', 'inventory_sha256',
    'source_context_sha256'))
_SHA = re.compile('[0-9a-f]{64}')
_CODES = frozenset(('activation_ledger_binding_invalid', 'activation_ledger_path_invalid',
    'activation_ledger_store_invalid', 'activation_ledger_schema_invalid',
    'activation_ledger_missing', 'activation_ledger_exists', 'activation_ledger_busy',
    'activation_ledger_io_failed', 'activation_ledger_epoch_invalid',
    'activation_ledger_epoch_conflict', 'activation_ledger_source_invalid',
    'activation_ledger_source_conflict'))


class ActivationLedgerError(ValueError):
    """Stable code only; SQL, paths and supplied evidence never escape."""

    def __init__(self, code):
        self.code = code if code in _CODES else 'activation_ledger_store_invalid'
        super().__init__(self.code)


class _CustodyBodyError(Exception):
    """Keep ledger errors from being mistaken for custody-helper ValueErrors."""

    def __init__(self, error):
        self.error = error


def _fail(code='activation_ledger_store_invalid'):
    raise ActivationLedgerError(code) from None


def _is_sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _is_uuid(value):
    try:
        return type(value) is str and len(value) == 36 and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _binding(value):
    if (type(value) is not dict or set(value) != _BINDING_KEYS
            or type(value['protocol']) is not int or value['protocol'] != 1
            or not _is_uuid(value['restore_id']) or not _is_uuid(value['r3_id'])
            or any(not _is_sha(value[key]) for key in
                   _BINDING_KEYS - {'protocol', 'restore_id', 'r3_id'})):
        _fail('activation_ledger_binding_invalid')
    return dict(value)


def _capabilities(value):
    if (type(value) is not tuple or not 0 < len(value) <= len(_CAPABILITIES)
            or any(type(item) is not str or item not in _CAPABILITIES for item in value)
            or value != tuple(sorted(set(value)))):
        _fail('activation_ledger_epoch_invalid')
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_canonical(value).encode('ascii')).hexdigest()


def _sha_column(name):
    return (f'{name} TEXT NOT NULL CHECK(typeof({name})=\'text\' AND length({name})=64 '
            f'AND {name} NOT GLOB \'*[^0-9a-f]*\')')


_TABLES = {
    'ledger_binding': (
        'CREATE TABLE ledger_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'ledger_id TEXT NOT NULL UNIQUE CHECK(length(ledger_id)=36),'
        'binding TEXT NOT NULL CHECK(json_valid(binding) AND length(binding)<=4096),'
        + _sha_column('binding_sha256') + ')'
    ),
    'generation_hold': (
        'CREATE TABLE generation_hold(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'generation_id TEXT NOT NULL UNIQUE CHECK(length(generation_id)=36),'
        "ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal=1),"
        "state TEXT NOT NULL CHECK(state='generation_hold'))"
    ),
    'activation_epoch': (
        'CREATE TABLE activation_epoch(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'epoch_id TEXT NOT NULL UNIQUE CHECK(length(epoch_id)=36),'
        'generation_id TEXT NOT NULL REFERENCES generation_hold(generation_id),'
        + _sha_column('scope_sha256') + ','
        'capabilities TEXT NOT NULL CHECK(json_valid(capabilities) AND length(capabilities)<=512),'
        "state TEXT NOT NULL CHECK(state='epoch_prepared_blocked'))"
    ),
    'activation_sources': (
        'CREATE TABLE activation_sources(role TEXT PRIMARY KEY NOT NULL '
        "CHECK(role IN ('secretary','team','billing','vikunja')),"
        'epoch_id TEXT NOT NULL REFERENCES activation_epoch(epoch_id),'
        + _sha_column('source_commitment_sha256') + ','
        + _sha_column('baseline_file_sha256') + ','
        + _sha_column('preparation_sha256') + ')'
    ),
}


def _trigger(name, table, event, condition=None):
    when = f' WHEN {condition}' if condition else ''
    return (f'CREATE TRIGGER {name} BEFORE {event} ON {table}{when} '
            "BEGIN SELECT RAISE(ABORT,'activation_ledger_immutable'); END")


_TRIGGERS = {}
for _table in _TABLES:
    for _event in ('update', 'delete'):
        _name = f'{_table}_immutable_{_event}'
        _TRIGGERS[_name] = (_table, _trigger(_name, _table, _event.upper()))
for _table in ('ledger_binding', 'generation_hold', 'activation_epoch'):
    _name = f'{_table}_replace_guard'
    _TRIGGERS[_name] = (_table, _trigger(_name, _table, 'INSERT',
                                       f'EXISTS(SELECT 1 FROM {_table})'))
_TRIGGERS['activation_sources_replace_guard'] = ('activation_sources', _trigger(
    'activation_sources_replace_guard', 'activation_sources', 'INSERT',
    'EXISTS(SELECT 1 FROM activation_sources WHERE role=NEW.role)'))
_TRIGGERS['activation_sources_prepared_guard'] = ('activation_sources', _trigger(
    'activation_sources_prepared_guard', 'activation_sources', 'INSERT',
    'NOT EXISTS(SELECT 1 FROM activation_epoch WHERE epoch_id=NEW.epoch_id) OR '
    '(SELECT count(*) FROM activation_sources)>=4'))

_DDL = (*_TABLES.values(), *(item[1] for item in _TRIGGERS.values()))
_EXPECTED_SCHEMA = {
    **{name: ('table', name, sql) for name, sql in _TABLES.items()},
    **{name: ('trigger', table, sql) for name, (table, sql) in _TRIGGERS.items()},
    **{f'sqlite_autoindex_{table}_1': ('index', table, None) for table in _TABLES},
}


class RestoreActivationLedger:
    """Durable blocked preparation only; opening a missing reader never creates."""

    def __init__(self, path: Path, binding: dict):
        self._setup(path, binding)
        self._open()

    @classmethod
    def create(cls, path: Path, binding: dict):
        instance = cls.__new__(cls)
        instance._setup(path, binding)
        with instance._custody():
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(instance._path, flags, 0o600)
            except FileExistsError:
                _fail('activation_ledger_exists')
            except OSError:
                _fail('activation_ledger_io_failed')
            with os.fdopen(fd, 'wb') as stream:
                stream.flush()
                os.fsync(stream.fileno())
                instance._identity = instance._file_identity(os.fstat(stream.fileno()))
                # Keep the original Windows descriptor through SQLite commit:
                # it permits SQLite I/O but denies delete/rename substitution.
                instance._initialize()
        instance._open()
        return instance

    @classmethod
    def open_existing(cls, path: Path, binding: dict):
        return cls(path, binding)

    def _setup(self, path, binding):
        self._binding = _binding(binding)
        self._binding_json = _canonical(self._binding)
        self._binding_sha256 = _digest(self._binding)
        self._identity = None
        self._ledger_id = None
        self._generation_id = None
        if not isinstance(path, Path):
            _fail('activation_ledger_path_invalid')
        raw = str(path)
        if (not path.is_absolute() or len(raw) > 4096 or raw.startswith(('\\\\', '//'))
                or any(ord(c) < 32 for c in raw) or not path.name
                or any(item in ('.', '..') or item.rstrip(' .') != item
                       for item in re.split(r'[\\/]', raw) if item)
                or any(':' in item for item in re.split(r'[\\/]', raw)[1:])):
            _fail('activation_ledger_path_invalid')
        self._path = path

    @contextmanager
    def _custody(self):
        from secretary.infrastructure.restore_operator import OperatorError, protected_scope
        try:
            with protected_scope(self._path.parent):
                try:
                    yield
                except ActivationLedgerError as error:
                    raise _CustodyBodyError(error) from None
        except _CustodyBodyError as error:
            raise error.error from None
        except OperatorError:
            _fail('activation_ledger_path_invalid')
        except OSError:
            _fail('activation_ledger_io_failed')

    def _open(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            snapshot = self._validate(conn)
        self._ledger_id = snapshot['ledger_id']
        self._generation_id = snapshot['generation_hold']['generation_id']

    @staticmethod
    def _unsafe(info):
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

    @staticmethod
    def _file_identity(info):
        return info.st_dev, info.st_ino

    def _check_file(self):
        try:
            info = self._path.lstat()
            identity = self._file_identity(info)
            if (not stat.S_ISREG(info.st_mode) or self._unsafe(info) or info.st_nlink != 1
                    or not 100 <= info.st_size <= _MAX_DATABASE_BYTES
                    or self._identity is not None and identity != self._identity):
                _fail()
            for suffix in ('-wal', '-shm', '-journal'):
                # lstat catches dangling links as well as ordinary sidecars.
                try:
                    Path(str(self._path) + suffix).lstat()
                except FileNotFoundError:
                    continue
                _fail()
            with self._path.open('rb') as stream:
                if self._file_identity(os.fstat(stream.fileno())) != identity:
                    _fail()
                header = stream.read(32)
            if header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01':
                _fail()
            self._identity = identity
        except ActivationLedgerError:
            raise
        except FileNotFoundError:
            _fail('activation_ledger_missing')
        except OSError:
            _fail('activation_ledger_io_failed')

    @staticmethod
    def _configure(conn, *, read_only):
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, _MAX_VALUE_BYTES)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA recursive_triggers=ON')
        if read_only:
            conn.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + _READ_SECONDS
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

    @contextmanager
    def _connect(self, *, write):
        with self._custody():
            self._check_file()
            conn = None
            try:
                # Windows CRT read descriptors share ordinary reads/writes but
                # deny deletion. Pin the child, not merely its parent directory,
                # until SQLite rollback/commit/close has finished.
                with self._path.open('rb') as stream:
                    if self._file_identity(os.fstat(stream.fileno())) != self._identity:
                        _fail()
                    self._check_file()
                    try:
                        conn = sqlite3.connect(self._path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                               uri=True, timeout=_TIMEOUT_SECONDS, isolation_level=None)
                        self._configure(conn, read_only=not write)
                        if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',):
                            _fail()
                        yield conn
                    finally:
                        if conn is not None:
                            if conn.in_transaction:
                                conn.rollback()
                            conn.close()
                            conn = None
                    self._check_file()
            except ActivationLedgerError:
                raise
            except sqlite3.Error as exc:
                code = getattr(exc, 'sqlite_errorcode', None)
                _fail('activation_ledger_busy' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                      else 'activation_ledger_store_invalid')
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                _fail('activation_ledger_io_failed')

    def _check_initial_file(self, *, locked=False):
        info = self._path.lstat()
        if (self._identity is None or self._file_identity(info) != self._identity
                or not stat.S_ISREG(info.st_mode) or self._unsafe(info)
                or info.st_nlink != 1 or info.st_size != 0):
            _fail()
        suffixes = ('-wal', '-shm') if locked else ('-wal', '-shm', '-journal')
        for suffix in suffixes:
            try:
                Path(str(self._path) + suffix).lstat()
            except FileNotFoundError:
                continue
            _fail()

    def _initialize(self):
        conn = None
        try:
            self._check_initial_file()
            conn = sqlite3.connect(self._path.as_uri() + '?mode=rw', uri=True,
                                   timeout=_TIMEOUT_SECONDS, isolation_level=None)
            self._configure(conn, read_only=False)
            if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',):
                _fail()
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN EXCLUSIVE')
            self._check_initial_file(locked=True)
            if conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone() is not None:
                _fail()
            for sql in _DDL:
                conn.execute(sql)
            conn.execute('INSERT INTO ledger_binding VALUES(1,?,?,?)',
                         (str(uuid4()), self._binding_json, self._binding_sha256))
            conn.execute('INSERT INTO generation_hold VALUES(1,?,1,?)',
                         (str(uuid4()), 'generation_hold'))
            conn.commit()
        except ActivationLedgerError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError):
            _fail('activation_ledger_io_failed')
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def _validate_schema(self, conn):
        objects = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master '
                               'ORDER BY name LIMIT ?', (_MAX_SQL_OBJECTS + 1,)).fetchall()
        if len(objects) > _MAX_SQL_OBJECTS:
            _fail('activation_ledger_schema_invalid')
        found = {}
        for kind, name, table, sql in objects:
            if (type(name) is not str or name in found
                    or _EXPECTED_SCHEMA.get(name) != (kind, table, sql)):
                _fail('activation_ledger_schema_invalid')
            found[name] = (kind, table, sql)
        if found != _EXPECTED_SCHEMA:
            _fail('activation_ledger_schema_invalid')

    def _validate(self, conn):
        self._validate_schema(conn)
        bindings = conn.execute('SELECT singleton,ledger_id,binding,binding_sha256 '
                                'FROM ledger_binding LIMIT 2').fetchall()
        if len(bindings) != 1:
            _fail()
        singleton, ledger_id, raw_binding, binding_hash = bindings[0]
        if (type(singleton) is not int or singleton != 1 or not _is_uuid(ledger_id)
                or self._ledger_id is not None and self._ledger_id != ledger_id
                or raw_binding != self._binding_json or binding_hash != self._binding_sha256):
            _fail()
        holds = conn.execute('SELECT singleton,generation_id,ordinal,state FROM generation_hold LIMIT 2').fetchall()
        if len(holds) != 1:
            _fail()
        singleton, generation_id, ordinal, state = holds[0]
        if (type(singleton) is not int or singleton != 1 or not _is_uuid(generation_id)
                or self._generation_id is not None and generation_id != self._generation_id
                or type(ordinal) is not int or ordinal != 1 or state != 'generation_hold'):
            _fail()
        epoch_rows = conn.execute('SELECT singleton,epoch_id,generation_id,scope_sha256,'
                                  'capabilities,state FROM activation_epoch LIMIT 2').fetchall()
        if len(epoch_rows) > 1:
            _fail()
        epoch = None
        if epoch_rows:
            singleton, epoch_id, parent_id, scope, raw_capabilities, state = epoch_rows[0]
            if (type(singleton) is not int or singleton != 1 or not _is_uuid(epoch_id)
                    or parent_id != generation_id or not _is_sha(scope)
                    or type(raw_capabilities) is not str or len(raw_capabilities) > 512
                    or state != 'epoch_prepared_blocked'):
                _fail()
            try:
                values = json.loads(raw_capabilities)
                if type(values) is not list:
                    _fail()
                capabilities = _capabilities(tuple(values))
            except (ValueError, TypeError, UnicodeError, RecursionError):
                _fail()
            if raw_capabilities != _canonical(capabilities):
                _fail()
            epoch = {'epoch_id': epoch_id, 'generation_id': generation_id,
                     'scope_sha256': scope, 'capabilities': capabilities, 'state': state}
        source_rows = conn.execute('SELECT role,epoch_id,source_commitment_sha256,'
                                   'baseline_file_sha256,preparation_sha256 '
                                   'FROM activation_sources ORDER BY role LIMIT 5').fetchall()
        if len(source_rows) > 4:
            _fail()
        sources = {}
        for role, parent_id, commitment, baseline, preparation in source_rows:
            if (type(role) is not str or role not in _ROLES or role in sources or epoch is None
                    or parent_id != epoch['epoch_id']
                    or not all(_is_sha(value) for value in (commitment, baseline, preparation))):
                _fail()
            sources[role] = {'source_commitment_sha256': commitment,
                             'baseline_file_sha256': baseline, 'preparation_sha256': preparation}
        if conn.execute('PRAGMA foreign_key_check').fetchmany(1):
            _fail()
        return {'protocol': 1, 'state': 'epoch_prepared_blocked', 'ledger_id': ledger_id,
                'binding': dict(self._binding), 'binding_sha256': self._binding_sha256,
                'generation_hold': {'generation_id': generation_id, 'ordinal': 1,
                                    'state': 'generation_hold'},
                'epoch': epoch, 'sources': sources,
                'missing_source_roles': tuple(role for role in _ROLES if role not in sources),
                'preparation_complete': len(sources) == 4,
                'activation_supported': False, 'outbound': False, 'outbound_enabled': False}

    @contextmanager
    def _transaction(self):
        # Validate foreign/altered data read-only before opening any writer.
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            self._validate(conn)
        with self._connect(write=True) as conn:
            conn.execute('BEGIN IMMEDIATE')
            snapshot = self._validate(conn)
            yield conn, snapshot
            self._validate(conn)
            conn.commit()

    def snapshot(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            return self._validate(conn)

    def prepare_epoch(self, scope_sha256, capabilities):
        if not _is_sha(scope_sha256):
            _fail('activation_ledger_epoch_invalid')
        capabilities = _capabilities(capabilities)
        with self._transaction() as (conn, snapshot):
            previous = snapshot['epoch']
            if previous is not None:
                if (previous['scope_sha256'] != scope_sha256
                        or previous['capabilities'] != capabilities):
                    _fail('activation_ledger_epoch_conflict')
                return previous
            epoch = {'epoch_id': str(uuid4()),
                     'generation_id': snapshot['generation_hold']['generation_id'],
                     'scope_sha256': scope_sha256, 'capabilities': capabilities,
                     'state': 'epoch_prepared_blocked'}
            conn.execute('INSERT INTO activation_epoch VALUES(1,?,?,?,?,?)',
                         (epoch['epoch_id'], epoch['generation_id'], scope_sha256,
                          _canonical(capabilities), epoch['state']))
        return epoch

    def record_source(self, epoch, role, source_commitment_sha256,
                      baseline_file_sha256, preparation_sha256):
        if (not _is_uuid(epoch) or type(role) is not str or role not in _ROLES
                or not all(_is_sha(value) for value in (source_commitment_sha256,
                            baseline_file_sha256, preparation_sha256))):
            _fail('activation_ledger_source_invalid')
        with self._transaction() as (conn, snapshot):
            if snapshot['epoch'] is None or snapshot['epoch']['epoch_id'] != epoch:
                _fail('activation_ledger_source_invalid')
            record = {'source_commitment_sha256': source_commitment_sha256,
                      'baseline_file_sha256': baseline_file_sha256,
                      'preparation_sha256': preparation_sha256}
            previous = snapshot['sources'].get(role)
            if previous is not None:
                if previous != record:
                    _fail('activation_ledger_source_conflict')
                return
            conn.execute('INSERT INTO activation_sources VALUES(?,?,?,?,?)',
                         (role, epoch, source_commitment_sha256, baseline_file_sha256,
                          preparation_sha256))
