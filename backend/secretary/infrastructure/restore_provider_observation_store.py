"""Internal append-only diagnostic GET observations, never an activation grant.

Only the trusted issuer may supply payloads after actual purpose authentication
and retained source/config custody. A VerifiedOperatorApproval or a valid payload
is not itself proof of authentication, a network request, or provider ownership.
Synthetic evidence remains explicitly marked; a future activation verifier must
reject it. Reading history neither refreshes it nor authorizes any operation.

Private Windows custody protects against other accounts, not malicious code
already running as the current user. This module never opens owner stores,
loads settings/secrets, calls providers, starts a runtime, or changes sources.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import quote
from uuid import UUID, uuid4

from .restore_operator import _win32_path

_MAX_DATABASE_BYTES = 8 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_OBSERVATION_BYTES = 1024 * 1024
_MAX_OBSERVATIONS = 512
_MAX_APPROVALS = 64
_MAX_SQL_OBJECTS = 64
_READ_SECONDS = 1.0
_TIMEOUT_SECONDS = .2
_PURPOSE = 'restore_provider_observation'
_BINDING_KEYS = frozenset(('protocol', 'purpose', 'restore_id', 'epoch_id',
    'preparation_id', 'source_snapshot_sha256', 'scope_sha256', 'binding_sha256'))
_PAYLOAD_KEYS = _BINDING_KEYS | frozenset(('evidence_kind', 'operator_approval_sha256',
    'provider', 'operation', 'credential_sha256', 'resource_sha256', 'request_sha256',
    'method', 'started_at', 'finished_at', 'expires_at', 'state', 'facts', 'error_code'))
_OPERATIONS = {'polza_key_usage': 'polza', 'polza_generation_history': 'polza',
    'max_bot_identity': 'max', 'max_subscriptions': 'max'}
_ERRORS = frozenset(('monthly_budget_configuration', 'monthly_budget_account_unavailable',
    'monthly_budget_receipt_unavailable', 'max_configuration_invalid', 'max_transport_unsafe',
    'max_response_invalid', 'max_http_rejected', 'max_transport_uncertain',
    'max_subscription_uncertain', 'provider_configuration_required',
    'provider_configuration_changed', 'provider_observation_unavailable'))
_SHA = re.compile('[0-9a-f]{64}')
_UTC = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z')
_CODES = frozenset('provider_observation_' + suffix for suffix in (
    'binding_invalid', 'path_invalid', 'store_invalid', 'schema_invalid', 'missing',
    'exists', 'busy', 'io_failed', 'approval_invalid', 'approval_expired',
    'approval_conflict', 'approval_limit', 'approval_required', 'payload_invalid',
    'payload_expired', 'observation_limit', 'byte_limit'))


class ObservationStoreError(ValueError):
    def __init__(self, code='provider_observation_store_invalid'):
        self.code = code if type(code) is str and code in _CODES else 'provider_observation_store_invalid'
        super().__init__(self.code)


class _CustodyBodyError(Exception):
    def __init__(self, error): self.error = error


def _fail(code='provider_observation_store_invalid'): raise ObservationStoreError(code) from None


def _is_sha(value): return type(value) is str and _SHA.fullmatch(value) is not None


def _is_uuid(value):
    try: return type(value) is str and len(value) == 36 and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError): return False


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value): return sha256(_canonical(value).encode('ascii')).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: _fail()
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _binding(value):
    try:
        if (type(value) is not dict or set(value) != _BINDING_KEYS
                or type(value['protocol']) is not int or value['protocol'] != 1
                or value['purpose'] != _PURPOSE
                or any(not _is_uuid(value[key]) for key in ('restore_id', 'epoch_id', 'preparation_id'))
                or any(not _is_sha(value[key]) for key in
                    ('source_snapshot_sha256', 'scope_sha256', 'binding_sha256'))):
            _fail('provider_observation_binding_invalid')
        return _json(_canonical(value))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail('provider_observation_binding_invalid')


def _utc(value):
    if type(value) is not str or _UTC.fullmatch(value) is None: _fail('provider_observation_payload_invalid')
    try:
        parsed = datetime.fromisoformat(value[:-1] + '+00:00')
        if parsed.isoformat(timespec='microseconds').replace('+00:00', 'Z') != value:
            _fail('provider_observation_payload_invalid')
        return parsed.timestamp()
    except (ValueError, OverflowError): _fail('provider_observation_payload_invalid')


def _integer(value, *, nullable=False):
    return (nullable and value is None) or (type(value) is int and 0 <= value < 2**63)


def _provider_id(value):
    return (type(value) is str and re.fullmatch('[1-9][0-9]{0,18}', value) is not None
        and int(value) < 2**63)


def _facts(value, operation, credential):
    if type(value) is not dict: _fail('provider_observation_payload_invalid')
    if operation == 'polza_key_usage':
        if (set(value) != {'key_tag', 'limit_micro', 'remaining_micro', 'usage_micro', 'reset'}
                or value['key_tag'] != credential or not _is_sha(value['key_tag'])
                or not _integer(value['limit_micro'], nullable=True)
                or not _integer(value['remaining_micro'], nullable=True)
                or not _integer(value['usage_micro'])
                or value['reset'] not in (None, 'daily', 'weekly', 'monthly', 'never')):
            _fail('provider_observation_payload_invalid')
    elif operation == 'polza_generation_history':
        if set(value) != {'provider_request_id', 'status', 'confirmed_cost_micro', 'provider_period'}:
            _fail('provider_observation_payload_invalid')
        identifier, status, cost = value['provider_request_id'], value['status'], value['confirmed_cost_micro']
        if (type(identifier) is not str or not 1 <= len(identifier) <= 256
                or identifier in ('.', '..') or identifier.strip() != identifier or not identifier.isprintable()
                or type(status) is not str or status not in ('completed', 'failed', 'pending')
                or value['provider_period'] is not None
                or (cost is not None if status == 'pending' else not _integer(cost))):
            _fail('provider_observation_payload_invalid')
    elif operation == 'max_bot_identity':
        if set(value) != {'bot_id'} or not _provider_id(value['bot_id']):
            _fail('provider_observation_payload_invalid')
    else:
        if (set(value) != {'bot_id', 'subscriptions'} or not _provider_id(value['bot_id'])
                or type(value['subscriptions']) is not list or len(value['subscriptions']) > 1000):
            _fail('provider_observation_payload_invalid')
        seen = set()
        for item in value['subscriptions']:
            if (type(item) is not dict or set(item) != {'url_sha256', 'time', 'update_types'}
                    or not _is_sha(item['url_sha256']) or item['url_sha256'] in seen
                    or not _integer(item['time'], nullable=True)
                    or type(item['update_types']) is not list or len(item['update_types']) > 100
                    or any(type(kind) is not str or re.fullmatch('[a-z_]{1,128}', kind) is None
                        for kind in item['update_types'])
                    or item['update_types'] != sorted(set(item['update_types']))):
                _fail('provider_observation_payload_invalid')
            seen.add(item['url_sha256'])


def _now():
    value = time.time()
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 2**63: _fail()
    return value


def _payload(value, binding, approval, *, fresh):
    try:
        if (type(value) is not dict or set(value) != _PAYLOAD_KEYS
                or _binding({key: value[key] for key in _BINDING_KEYS}) != binding
                or value['evidence_kind'] not in ('production_get', 'synthetic_get')
                or value['operator_approval_sha256'] != approval['approval_digest']
                or type(value['operation']) is not str or value['operation'] not in _OPERATIONS
                or value['provider'] != _OPERATIONS[value['operation']] or value['method'] != 'GET'
                or any(not _is_sha(value[key]) for key in
                    ('credential_sha256', 'resource_sha256', 'request_sha256', 'operator_approval_sha256'))):
            _fail('provider_observation_payload_invalid')
        started, finished, expires = (_utc(value[key]) for key in ('started_at', 'finished_at', 'expires_at'))
        if (not 0 <= finished - started <= 30 or not finished < expires <= finished + 300
                or expires > approval['expires_at'] or started < approval['expires_at'] - 300):
            _fail('provider_observation_payload_invalid')
        if fresh:
            now = _now()
            if finished > now or not now < expires: _fail('provider_observation_payload_expired')
        if value['state'] == 'observed':
            if value['error_code'] is not None: _fail('provider_observation_payload_invalid')
            _facts(value['facts'], value['operation'], value['credential_sha256'])
        elif value['state'] == 'unavailable':
            if (value['facts'] is not None or type(value['error_code']) is not str
                    or value['error_code'] not in _ERRORS): _fail('provider_observation_payload_invalid')
        else: _fail('provider_observation_payload_invalid')
        raw = _canonical(value)
        if len(raw) > _MAX_PAYLOAD_BYTES: _fail('provider_observation_byte_limit')
        return _json(raw), raw
    except ObservationStoreError: raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        _fail('provider_observation_payload_invalid')


def _local_path(value):
    if not isinstance(value, Path): _fail('provider_observation_path_invalid')
    raw = str(value)
    components = re.split(r'[\\/]', raw)
    if (not 0 < len(raw) <= 4096 or raw.replace('/', '\\').startswith('\\\\')
            or any(ord(c) < 32 for c in raw)
            or any(p in ('.', '..') or p.rstrip(' .') != p for p in components if p)
            or any(':' in p or any(c in '<>"|?*' for c in p) for p in components[1:])
            or any(re.fullmatch(r'CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]',
                p.split('.')[0].upper()) is not None for p in components[1:] if p)
            or not value.is_absolute() or not value.name): _fail('provider_observation_path_invalid')
    return value


def _sha_column(name):
    return (f"{name} TEXT NOT NULL CHECK(typeof({name})='text' AND length({name})=64 "
        f"AND {name} NOT GLOB '*[^0-9a-f]*')")


_TABLES = {
    'observation_binding': ('CREATE TABLE observation_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'store_id TEXT NOT NULL UNIQUE CHECK(length(store_id)=36),'
        'binding TEXT NOT NULL CHECK(json_valid(binding) AND length(binding)<=8192),'
        + _sha_column('binding_digest_sha256') + ')'),
    'observation_operator_approval': ('CREATE TABLE observation_operator_approval(seq INTEGER PRIMARY KEY '
        "CHECK(typeof(seq)='integer' AND seq BETWEEN 1 AND 64),"
        + _sha_column('operator_digest') + ',' + _sha_column('approval_digest') + ' UNIQUE,'
        + _sha_column('nonce_hash') + ' UNIQUE,'
        "expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at>0))"),
    'provider_observations': ('CREATE TABLE provider_observations(seq INTEGER PRIMARY KEY '
        "CHECK(typeof(seq)='integer' AND seq BETWEEN 1 AND 512),"
        + _sha_column('approval_digest') + ',payload TEXT NOT NULL '
        'CHECK(json_valid(payload) AND length(payload)<=65536),'
        + _sha_column('payload_sha256') + ' UNIQUE,'
        "previous_record_sha256 TEXT CHECK(previous_record_sha256 IS NULL OR "
        "(typeof(previous_record_sha256)='text' AND length(previous_record_sha256)=64 "
        "AND previous_record_sha256 NOT GLOB '*[^0-9a-f]*'))," + _sha_column('record_sha256') + ' UNIQUE,'
        'FOREIGN KEY(approval_digest) REFERENCES observation_operator_approval(approval_digest))'),
}


def _trigger(name, table, event, condition=None):
    when = f' WHEN {condition}' if condition else ''
    return (f'CREATE TRIGGER {name} BEFORE {event} ON {table}{when} '
        "BEGIN SELECT RAISE(ABORT,'provider_observation_immutable'); END")


_TRIGGERS = {}
for _table in _TABLES:
    for _event in ('update', 'delete'):
        _name = f'{_table}_immutable_{_event}'
        _TRIGGERS[_name] = (_table, _trigger(_name, _table, _event.upper()))
_TRIGGERS['observation_binding_insert_guard'] = ('observation_binding', _trigger(
    'observation_binding_insert_guard', 'observation_binding', 'INSERT',
    'EXISTS(SELECT 1 FROM observation_binding)'))
_TRIGGERS['observation_approval_insert_guard'] = ('observation_operator_approval', _trigger(
    'observation_approval_insert_guard', 'observation_operator_approval', 'INSERT',
    'EXISTS(SELECT 1 FROM observation_operator_approval WHERE seq=NEW.seq OR '
    'approval_digest=NEW.approval_digest OR nonce_hash=NEW.nonce_hash OR operator_digest!=NEW.operator_digest) OR '
    'NEW.seq!=(SELECT count(*)+1 FROM observation_operator_approval)'))
_TRIGGERS['provider_observation_insert_guard'] = ('provider_observations', _trigger(
    'provider_observation_insert_guard', 'provider_observations', 'INSERT',
    'EXISTS(SELECT 1 FROM provider_observations WHERE seq=NEW.seq OR '
    'payload_sha256=NEW.payload_sha256 OR record_sha256=NEW.record_sha256) OR '
    'NEW.seq!=(SELECT count(*)+1 FROM provider_observations) OR '
    'NEW.approval_digest IS NOT (SELECT approval_digest FROM observation_operator_approval ORDER BY seq DESC LIMIT 1) OR '
    'NEW.previous_record_sha256 IS NOT (SELECT record_sha256 FROM provider_observations ORDER BY seq DESC LIMIT 1) OR '
    'length(NEW.payload)+(SELECT coalesce(sum(length(payload)),0) FROM provider_observations)>1048576'))
_DDL = (*_TABLES.values(), *(item[1] for item in _TRIGGERS.values()))
_EXPECTED_SCHEMA = {
    **{name: ('table', name, sql) for name, sql in _TABLES.items()},
    **{name: ('trigger', table, sql) for name, (table, sql) in _TRIGGERS.items()},
    'sqlite_autoindex_observation_binding_1': ('index', 'observation_binding', None),
    'sqlite_autoindex_observation_operator_approval_1': ('index', 'observation_operator_approval', None),
    'sqlite_autoindex_observation_operator_approval_2': ('index', 'observation_operator_approval', None),
    'sqlite_autoindex_provider_observations_1': ('index', 'provider_observations', None),
    'sqlite_autoindex_provider_observations_2': ('index', 'provider_observations', None),
}


def _namespace(conn):
    if type(conn) is not sqlite3.Connection: _fail()
    databases = conn.execute('PRAGMA database_list').fetchall()
    if (not any(row[0] == 0 and row[1] == 'main' for row in databases)
            or any(not (row[0] == 0 and row[1] == 'main' or row[0] == 1 and row[1] == 'temp')
                for row in databases)
            or conn.execute('SELECT 1 FROM temp.sqlite_master LIMIT 1').fetchone() is not None): _fail()


class ObservationStore:
    """Internal pipeline journal; create is exclusive, every read existing-only."""
    @classmethod
    def create(cls, path: Path, binding: dict):
        instance = cls.__new__(cls)
        instance._setup(path, binding)
        with instance._custody():
            with os.fdopen(instance._open_descriptor(create=True), 'wb') as stream:
                stream.flush(); os.fsync(stream.fileno())
                instance._identity = instance._file_identity(os.fstat(stream.fileno()))
                instance._initialize()
        instance._open()
        return instance

    @classmethod
    def open_existing(cls, path: Path, binding: dict):
        instance = cls.__new__(cls)
        instance._setup(path, binding)
        instance._open()
        return instance

    def _setup(self, path, binding):
        self._binding = _binding(binding)
        self._path = _local_path(path)
        self._io_path = Path(_win32_path(self._path))
        self._identity = self._store_id = None

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
            if create and error in (80, 183): _fail('provider_observation_exists')
            if not create and error in (2, 3): _fail('provider_observation_missing')
            _fail('provider_observation_io_failed')
        try:
            return msvcrt.open_osfhandle(handle, (os.O_RDWR if create else os.O_RDONLY) | os.O_BINARY)
        except (OSError, ValueError):
            kernel.CloseHandle(handle)
            _fail('provider_observation_io_failed')

    def _sqlite_uri(self, *, write):
        suffix = '?mode=rw' if write else '?mode=ro'
        if len(str(self._path)) > 248:
            return 'file:' + quote(str(self._io_path), safe='') + suffix + '&vfs=win32-longpath'
        return self._path.as_uri() + suffix

    @contextmanager
    def _custody(self):
        from .restore_operator import OperatorError, protected_scope
        try:
            with protected_scope(self._path.parent):
                try: yield
                except ObservationStoreError as error: raise _CustodyBodyError(error) from None
        except _CustodyBodyError as error: raise error.error from None
        except OperatorError: _fail('provider_observation_path_invalid')
        except OSError: _fail('provider_observation_io_failed')

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
        except ObservationStoreError: raise
        except FileNotFoundError: _fail('provider_observation_missing')
        except OSError: _fail('provider_observation_io_failed')

    @staticmethod
    def _configure(conn, *, read_only):
        _namespace(conn)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_PAYLOAD_BYTES + 8192)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, _MAX_PAYLOAD_BYTES)
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
                with os.fdopen(self._open_descriptor(), 'rb') as stream:
                    if self._file_identity(os.fstat(stream.fileno())) != self._identity: _fail()
                    self._check_file()
                    try:
                        conn = sqlite3.connect(self._sqlite_uri(write=write), uri=True,
                            timeout=_TIMEOUT_SECONDS, isolation_level=None)
                        self._configure(conn, read_only=not write)
                        if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',): _fail()
                        yield conn
                    finally:
                        if conn is not None:
                            if conn.in_transaction: conn.rollback()
                            conn.close()
                            conn = None
                    self._check_file()
            except ObservationStoreError: raise
            except sqlite3.Error as error:
                _fail('provider_observation_busy' if getattr(error, 'sqlite_errorcode', None)
                    in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else 'provider_observation_store_invalid')
            except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
                _fail('provider_observation_io_failed')
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
            conn.execute('INSERT INTO observation_binding VALUES(1,?,?,?)',
                (str(uuid4()), _canonical(self._binding), _digest(self._binding)))
            self._validate(conn)
            conn.commit()
        except ObservationStoreError: raise
        except (sqlite3.Error, OSError, ValueError, TypeError): _fail('provider_observation_io_failed')
        finally:
            if conn is not None:
                if conn.in_transaction: conn.rollback()
                conn.close()

    @staticmethod
    def _validate_schema(conn):
        _namespace(conn)
        rows = conn.execute('SELECT type,name,tbl_name,sql FROM main.sqlite_master '
            'ORDER BY name LIMIT ?', (_MAX_SQL_OBJECTS + 1,)).fetchall()
        found = {name: (kind, table, sql) for kind, name, table, sql in rows}
        if len(rows) != len(found) or found != _EXPECTED_SCHEMA: _fail('provider_observation_schema_invalid')

    @staticmethod
    def _record(store_id, binding_digest, seq, payload, previous):
        payload_hash = _digest(payload)
        record_hash = _digest(dict(protocol=1, store_id=store_id, binding_digest_sha256=binding_digest,
            seq=seq, operator_approval_sha256=payload['operator_approval_sha256'],
            payload_sha256=payload_hash, previous_record_sha256=previous))
        return dict(seq=seq, payload=payload, payload_sha256=payload_hash,
            previous_record_sha256=previous, record_sha256=record_hash)

    def _validate(self, conn):
        self._validate_schema(conn)
        rows = conn.execute('SELECT singleton,store_id,binding,binding_digest_sha256 '
            'FROM main.observation_binding LIMIT 2').fetchall()
        if len(rows) != 1: _fail()
        singleton, store_id, raw, binding_digest = rows[0]
        if (type(singleton) is not int or singleton != 1 or not _is_uuid(store_id)
                or self._store_id is not None and self._store_id != store_id
                or type(raw) is not str or not 0 < len(raw) <= 8192): _fail()
        bound = _binding(_json(raw))
        if bound != self._binding or raw != _canonical(bound) or binding_digest != _digest(bound): _fail()
        approvals, by_digest, nonces = [], {}, set()
        rows = conn.execute('SELECT seq,operator_digest,approval_digest,nonce_hash,expires_at '
            'FROM main.observation_operator_approval ORDER BY seq LIMIT 65').fetchall()
        if len(rows) > _MAX_APPROVALS: _fail()
        for index, (seq, operator, digest, nonce, expires) in enumerate(rows, 1):
            if (type(seq) is not int or seq != index
                    or not all(_is_sha(item) for item in (operator, digest, nonce))
                    or nonce in nonces or digest in by_digest
                    or approvals and operator != approvals[0]['operator_digest']
                    or type(expires) is not int or not 0 < expires < 2**63): _fail()
            record = dict(operator_digest=operator, approval_digest=digest, nonce_hash=nonce, expires_at=expires)
            approvals.append(record); by_digest[digest] = record; nonces.add(nonce)
        observations, seen, total_bytes, previous = [], set(), 0, None
        rows = conn.execute('SELECT seq,approval_digest,payload,payload_sha256,previous_record_sha256,record_sha256 '
            'FROM main.provider_observations ORDER BY seq LIMIT 513').fetchall()
        if len(rows) > _MAX_OBSERVATIONS: _fail()
        for index, (seq, approval_digest, raw, payload_hash, predecessor, record_hash) in enumerate(rows, 1):
            if (type(seq) is not int or seq != index or approval_digest not in by_digest
                    or type(raw) is not str or not 0 < len(raw) <= _MAX_PAYLOAD_BYTES
                    or not _is_sha(payload_hash) or payload_hash in seen
                    or predecessor != previous or not _is_sha(record_hash)): _fail()
            value, canonical = _payload(_json(raw), bound, by_digest[approval_digest], fresh=False)
            record = self._record(store_id, binding_digest, seq, value, previous)
            if raw != canonical or payload_hash != record['payload_sha256'] or record_hash != record['record_sha256']: _fail()
            total_bytes += len(raw)
            if total_bytes > _MAX_OBSERVATION_BYTES: _fail()
            observations.append(record); seen.add(payload_hash); previous = record_hash
        if conn.execute('PRAGMA foreign_key_check').fetchmany(1): _fail()
        return dict(protocol=1, store_id=store_id, binding=bound, binding_digest_sha256=binding_digest,
            approval=approvals[-1] if approvals else None, approvals=tuple(approvals),
            observations=tuple(observations), observation_bytes=total_bytes,
            state='provider_observations_blocked', activation_supported=False, outbound_enabled=False)

    def _open(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            snapshot = self._validate(conn)
        self._store_id = snapshot['store_id']

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
        now = _now()
        if not now < receipt['expires_at'] <= now + 300: _fail('provider_observation_approval_expired')

    def consume_approval(self, verified):
        """Root must authenticate this exact purpose/scope first, never just construct the DTO."""
        from .restore_operator import VerifiedOperatorApproval
        if (type(verified) is not VerifiedOperatorApproval
                or not all(_is_sha(item) for item in
                    (verified.operator_digest, verified.approval_digest, verified.nonce_hash))
                or type(verified.expires_at) is not int or not 0 < verified.expires_at < 2**63):
            _fail('provider_observation_approval_invalid')
        record = dict(operator_digest=verified.operator_digest, approval_digest=verified.approval_digest,
            nonce_hash=verified.nonce_hash, expires_at=verified.expires_at)
        with self._transaction() as (conn, snapshot):
            for old in snapshot['approvals']:
                if old['nonce_hash'] == record['nonce_hash'] or old['approval_digest'] == record['approval_digest']:
                    if old != record: _fail('provider_observation_approval_conflict')
                    self._fresh(record)
                    return
            if snapshot['approval'] is not None and snapshot['approval']['operator_digest'] != record['operator_digest']:
                _fail('provider_observation_approval_invalid')
            self._fresh(record)
            if len(snapshot['approvals']) >= _MAX_APPROVALS: _fail('provider_observation_approval_limit')
            conn.execute('INSERT INTO observation_operator_approval VALUES(?,?,?,?,?)',
                (len(snapshot['approvals']) + 1, record['operator_digest'], record['approval_digest'],
                    record['nonce_hash'], record['expires_at']))
            self._fresh(record)

    def append_observation(self, payload):
        """Append closed diagnostic facts from the internal issuer; never verifies a GET by itself."""
        with self._transaction() as (conn, snapshot):
            approval = snapshot['approval']
            if approval is None: _fail('provider_observation_approval_required')
            self._fresh(approval)
            value, raw = _payload(payload, snapshot['binding'], approval, fresh=True)
            payload_hash = _digest(value)
            for old in snapshot['observations']:
                if old['payload_sha256'] == payload_hash: return old
            if len(snapshot['observations']) >= _MAX_OBSERVATIONS: _fail('provider_observation_observation_limit')
            if snapshot['observation_bytes'] + len(raw) > _MAX_OBSERVATION_BYTES: _fail('provider_observation_byte_limit')
            previous = snapshot['observations'][-1]['record_sha256'] if snapshot['observations'] else None
            record = self._record(snapshot['store_id'], snapshot['binding_digest_sha256'],
                len(snapshot['observations']) + 1, value, previous)
            conn.execute('INSERT INTO provider_observations VALUES(?,?,?,?,?,?)',
                (record['seq'], approval['approval_digest'], raw, record['payload_sha256'],
                    previous, record['record_sha256']))
            self._fresh(approval)
            _payload(value, snapshot['binding'], approval, fresh=True)
        return record
