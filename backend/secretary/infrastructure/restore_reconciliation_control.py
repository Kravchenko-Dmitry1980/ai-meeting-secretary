"""Independent R3 preparation ledger; this protocol never activates a restore.

The caller must authenticate an operator freshly with restore_operator before
passing its exact VerifiedOperatorApproval DTO. A DTO is a typed receipt, not
cryptographic proof. The caller also verifies the existing private parent
directory and the hashes of native files/receipts; this ledger opens none of
those stores, and never invokes normal admission or migration repositories.

SQLite lock waiting is bounded to 0.2 seconds; reads have explicit size/row
bounds and a cooperative progress deadline. These are not a real-time SLA.
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


MAX_APPROVALS = 1024
_SCHEMA_APPROVAL_LIMIT = 1024
_MAX_DATABASE_BYTES = 8 * 1024 * 1024
_MAX_SQL_OBJECTS = 64
_MAX_VALUE_BYTES = 16 * 1024
_TIMEOUT_SECONDS = 0.2
_READ_SECONDS = 1.0
_AUTHORITIES = ('secretary', 'team', 'billing')
_BINDING_KEYS = frozenset((
    'protocol', 'restore_id', 'plan_sha256', 'input_sha256', 'scope_sha256',
    'evidence_sha256', 'source_paths_sha256', 'operator_authority_sha256',
))
_SHA = re.compile('[0-9a-f]{64}')
_CODES = frozenset((
    'restore_control_binding_invalid', 'restore_control_path_invalid',
    'restore_control_store_invalid', 'restore_control_schema_invalid',
    'restore_control_busy', 'restore_control_io_failed',
    'restore_control_approval_invalid', 'restore_control_approval_expired',
    'restore_control_nonce_consumed', 'restore_control_approval_limit',
    'restore_control_preparation_invalid', 'restore_control_preparation_conflict',
    'restore_control_incomplete', 'restore_control_decision_conflict',
    'restore_control_finalized',
    'restore_control_readonly',
))


class ControlError(ValueError):
    """A stable code only; database paths, SQL, and raw evidence never escape."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else 'restore_control_store_invalid'
        super().__init__(self.code)


def _fail(code='restore_control_store_invalid'):
    raise ControlError(code) from None


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
            or not _is_uuid(value['restore_id'])
            or any(not _is_sha(value[key]) for key in _BINDING_KEYS - {'protocol', 'restore_id'})):
        _fail('restore_control_binding_invalid')
    return dict(value)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_canonical(value).encode('ascii')).hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result

    if type(raw) is not str or len(raw) > _MAX_VALUE_BYTES:
        _fail()
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: _fail())
        if type(value) is not dict or _canonical(value) != raw:
            _fail()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _sha_column(name):
    return (f'{name} TEXT NOT NULL CHECK(typeof({name})=\'text\' AND length({name})=64 '
            f'AND {name} NOT GLOB \'*[^0-9a-f]*\')')


_TABLES = {
    'r3_hold': (
        'CREATE TABLE r3_hold('
        'singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'r3_id TEXT NOT NULL UNIQUE CHECK(length(r3_id)=36),'
        'binding TEXT NOT NULL CHECK(json_valid(binding) AND length(binding)<=4096),'
        + _sha_column('binding_sha256') + ','
        "state TEXT NOT NULL CHECK(state='restore_hold'))"
    ),
    'r3_approvals': (
        'CREATE TABLE r3_approvals('
        'seq INTEGER PRIMARY KEY CHECK(seq BETWEEN 1 AND 1024),'
        + _sha_column('operator_digest') + ','
        + _sha_column('approval_digest') + ','
        + _sha_column('nonce_hash') + ' UNIQUE,'
        "expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at>0))"
    ),
    'r3_preparations': (
        "CREATE TABLE r3_preparations(authority TEXT PRIMARY KEY NOT NULL "
        "CHECK(authority IN ('secretary','team','billing')),"
        + _sha_column('receipt_sha256') + ',' + _sha_column('file_sha256') + ')'
    ),
    'r3_decision': (
        'CREATE TABLE r3_decision(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'approval_seq INTEGER NOT NULL REFERENCES r3_approvals(seq),'
        'payload TEXT NOT NULL CHECK(json_valid(payload) AND length(payload)<=8192),'
        + _sha_column('decision_sha256') + ')'
    ),
}


def _trigger(name, table, event, condition=None):
    when = f' WHEN {condition}' if condition else ''
    return (f'CREATE TRIGGER {name} BEFORE {event} ON {table}{when} '
            "BEGIN SELECT RAISE(ABORT,'restore_control_immutable'); END")


_TRIGGERS = {}
for _table in _TABLES:
    for _event in ('update', 'delete'):
        _name = f'{_table}_immutable_{_event}'
        _TRIGGERS[_name] = (_table, _trigger(_name, _table, _event.upper()))

for _table, _condition in (
    ('r3_hold', 'EXISTS(SELECT 1 FROM r3_hold)'),
    ('r3_approvals', 'EXISTS(SELECT 1 FROM r3_approvals WHERE seq=NEW.seq OR nonce_hash=NEW.nonce_hash)'),
    ('r3_preparations', 'EXISTS(SELECT 1 FROM r3_preparations WHERE authority=NEW.authority)'),
    ('r3_decision', 'EXISTS(SELECT 1 FROM r3_decision)'),
):
    _name = f'{_table}_replace_guard'
    _TRIGGERS[_name] = (_table, _trigger(_name, _table, 'INSERT', _condition))

for _table in ('r3_approvals', 'r3_preparations'):
    _name = f'{_table}_final_guard'
    _TRIGGERS[_name] = (_table, _trigger(_name, _table, 'INSERT', 'EXISTS(SELECT 1 FROM r3_decision)'))

_TRIGGERS['r3_approvals_limit_guard'] = ('r3_approvals', _trigger(
    'r3_approvals_limit_guard', 'r3_approvals', 'INSERT', '(SELECT count(*) FROM r3_approvals)>=1024'))
_TRIGGERS['r3_decision_prepared_guard'] = ('r3_decision', _trigger(
    'r3_decision_prepared_guard', 'r3_decision', 'INSERT',
    '(SELECT count(*) FROM r3_preparations)!=3 OR '
    'NOT EXISTS(SELECT 1 FROM r3_approvals WHERE seq=NEW.approval_seq) OR '
    'NEW.approval_seq!=(SELECT max(seq) FROM r3_approvals)'))

_DDL = (*_TABLES.values(), *(item[1] for item in _TRIGGERS.values()))
_EXPECTED_SCHEMA = {
    **{name: ('table', name, sql) for name, sql in _TABLES.items()},
    **{name: ('trigger', table, sql) for name, (table, sql) in _TRIGGERS.items()},
    **{f'sqlite_autoindex_{table}_1': ('index', table, None)
       for table in ('r3_hold', 'r3_approvals', 'r3_preparations')},
}


class RestoreControl:
    """Append-only control store that keeps outbound and activation blocked."""

    def __init__(self, path: Path, binding: dict, *, _existing_only: bool = False):
        self._read_only = _existing_only
        self._binding = _binding(binding)
        self._binding_json = _canonical(self._binding)
        self._binding_sha256 = _digest(self._binding)
        self._r3_id = None
        self._identity = None
        if not isinstance(path, Path):
            _fail('restore_control_path_invalid')
        try:
            self._path = path.absolute()
            parent = self._path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or self._unsafe(parent)
                    or not self._path.name or ':' in self._path.name):
                _fail('restore_control_path_invalid')
            try:
                self._path.lstat()
            except FileNotFoundError:
                if _existing_only:
                    _fail('restore_control_path_invalid')
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                try:
                    fd = os.open(self._path, flags, 0o600)
                except FileExistsError:
                    pass  # A competing creator owns initialization; never reopen it for writes.
                else:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.flush()
                        os.fsync(stream.fileno())
                        self._identity = self._file_identity(os.fstat(stream.fileno()))
                    self._initialize()
            with self._connect(write=False) as conn:
                conn.execute('BEGIN')
                snapshot = self._validate(conn)
            self._r3_id = snapshot['r3_id']
        except ControlError:
            raise
        except (OSError, ValueError, TypeError, sqlite3.Error):
            _fail('restore_control_io_failed')

    @classmethod
    def open_existing(cls, path: Path, binding: dict) -> RestoreControl:
        """Read an existing R3 store; never create a replacement after loss."""
        return cls(path, binding, _existing_only=True)

    @property
    def r3_id(self) -> str:
        return self._r3_id

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
                    or (self._identity is not None and identity != self._identity)):
                _fail()
            for suffix in ('-wal', '-shm', '-journal'):
                if Path(str(self._path) + suffix).exists():
                    _fail()
            with self._path.open('rb') as stream:
                header = stream.read(32)
            if header[:16] != b'SQLite format 3\0' or header[18:20] != b'\x01\x01':
                _fail()
            self._identity = identity
        except ControlError:
            raise
        except OSError:
            _fail('restore_control_io_failed')

    @contextmanager
    def _connect(self, *, write):
        self._check_file()
        conn = None
        try:
            conn = sqlite3.connect(self._path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                   uri=True, timeout=_TIMEOUT_SECONDS, isolation_level=None)
            self._configure(conn, read_only=not write)
            if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',):
                _fail()
            yield conn
        except ControlError:
            raise
        except sqlite3.Error as exc:
            code = getattr(exc, 'sqlite_errorcode', None)
            _fail('restore_control_busy' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                  else 'restore_control_store_invalid')
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            _fail('restore_control_io_failed')
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

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
            conn.execute('INSERT INTO r3_hold VALUES(1,?,?,?,?)',
                         (str(uuid4()), self._binding_json, self._binding_sha256, 'restore_hold'))
            conn.commit()
        except ControlError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError):
            _fail('restore_control_io_failed')
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def _check_initial_file(self, *, locked=False):
        """Only the exclusively created empty inode can receive initial DDL."""
        info = self._path.lstat()
        if (self._identity is None or self._file_identity(info) != self._identity
                or not stat.S_ISREG(info.st_mode) or self._unsafe(info)
                or info.st_nlink != 1 or info.st_size != 0):
            _fail()
        # BEGIN EXCLUSIVE creates its own rollback journal even for an empty DB.
        suffixes = ('-wal', '-shm') if locked else ('-wal', '-shm', '-journal')
        if any(Path(str(self._path) + suffix).exists() for suffix in suffixes):
            _fail()

    def _validate_schema(self, conn):
        objects = conn.execute(
            'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name LIMIT ?',
            (_MAX_SQL_OBJECTS + 1,)).fetchall()
        if len(objects) > _MAX_SQL_OBJECTS:
            _fail('restore_control_schema_invalid')
        found = {}
        for kind, name, table, sql in objects:
            if name == 'sqlite_sequence':
                if (kind, table, sql) != ('table', 'sqlite_sequence', 'CREATE TABLE sqlite_sequence(name,seq)'):
                    _fail('restore_control_schema_invalid')
                continue
            if (type(name) is not str or name in found
                    or _EXPECTED_SCHEMA.get(name) != (kind, table, sql)):
                _fail('restore_control_schema_invalid')
            found[name] = (kind, table, sql)
        if found != _EXPECTED_SCHEMA:
            _fail('restore_control_schema_invalid')

    def _decision(self, r3_id, preparations, approval_digest, native_file_sha256):
        result = {
            'protocol': 1, 'state': 'complete_prepared_blocked', 'control_state': 'restore_hold',
            'r3_id': r3_id, 'binding': dict(self._binding), 'binding_sha256': self._binding_sha256,
            'operator_digest': self._binding['operator_authority_sha256'],
            'approval_digest': approval_digest, 'preparations': preparations,
            'native_file_sha256': native_file_sha256, 'activation_supported': False,
            'outbound': False, 'outbound_enabled': False,
        }
        result['decision_sha256'] = _digest(result)
        return result

    def _validate(self, conn):
        self._validate_schema(conn)
        holds = conn.execute('SELECT singleton,r3_id,binding,binding_sha256,state FROM r3_hold LIMIT 2').fetchall()
        if len(holds) != 1:
            _fail()
        singleton, r3_id, raw_binding, binding_sha256, state = holds[0]
        if (type(singleton) is not int or singleton != 1 or not _is_uuid(r3_id)
                or (self._r3_id is not None and self._r3_id != r3_id)
                or raw_binding != self._binding_json or binding_sha256 != self._binding_sha256
                or state != 'restore_hold'):
            _fail()
        approvals = conn.execute(
            'SELECT seq,operator_digest,approval_digest,nonce_hash,expires_at '
            'FROM r3_approvals ORDER BY seq LIMIT ?', (_SCHEMA_APPROVAL_LIMIT + 1,)).fetchall()
        if len(approvals) > _SCHEMA_APPROVAL_LIMIT:
            _fail()
        nonces = set()
        for expected_seq, (seq, operator, digest, nonce, expires) in enumerate(approvals, 1):
            if (type(seq) is not int or seq != expected_seq
                    or not all(_is_sha(value) for value in (operator, digest, nonce))
                    or operator != self._binding['operator_authority_sha256'] or nonce in nonces
                    or type(expires) is not int or not 0 < expires < 2 ** 63):
                _fail()
            nonces.add(nonce)
        latest = approvals[-1] if approvals else None
        prep_rows = conn.execute(
            'SELECT authority,receipt_sha256,file_sha256 FROM r3_preparations ORDER BY authority LIMIT 4').fetchall()
        if len(prep_rows) > 3:
            _fail()
        preparations = {}
        for authority, receipt, file_hash in prep_rows:
            if (type(authority) is not str or authority not in _AUTHORITIES or authority in preparations
                    or not _is_sha(receipt) or not _is_sha(file_hash)):
                _fail()
            preparations[authority] = {'receipt_sha256': receipt, 'file_sha256': file_hash}
        decisions = conn.execute(
            'SELECT singleton,approval_seq,payload,decision_sha256 FROM r3_decision LIMIT 2').fetchall()
        if len(decisions) > 1:
            _fail()
        decision = None
        if decisions:
            singleton, approval_seq, raw, decision_hash = decisions[0]
            decision = _json(raw)
            native_hash = decision.get('native_file_sha256')
            if (type(singleton) is not int or singleton != 1 or latest is None
                    or type(approval_seq) is not int or approval_seq != latest[0]
                    or len(preparations) != 3 or not _is_sha(native_hash)
                    or not _is_sha(decision_hash)):
                _fail()
            expected = self._decision(r3_id, preparations, latest[2], native_hash)
            if raw != _canonical(expected) or decision_hash != expected['decision_sha256']:
                _fail()
        foreign = conn.execute('PRAGMA foreign_key_check').fetchmany(1)
        if foreign:
            _fail()
        snapshot = {
            'protocol': 1, 'r3_id': r3_id, 'state': 'restore_hold',
            'binding': dict(self._binding), 'binding_sha256': self._binding_sha256,
            'preparations': preparations, 'approval_digest': latest[2] if latest else None,
            'decision': decision, 'activation_supported': False,
            'outbound': False, 'outbound_enabled': False,
        }
        return snapshot

    @contextmanager
    def _transaction(self):
        if self._read_only:
            _fail('restore_control_readonly')
        # A foreign or altered file fails through a read-only connection first.
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            self._validate(conn)
        with self._connect(write=True) as conn:
            conn.execute('BEGIN IMMEDIATE')
            snapshot = self._validate(conn)
            yield conn, snapshot
            conn.commit()

    def snapshot(self) -> dict:
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            return self._validate(conn)

    def consume_approval(self, verified) -> None:
        """Consume a nonce, after the caller's fresh cryptographic authentication."""
        from secretary.infrastructure.restore_operator import VerifiedOperatorApproval
        if type(verified) is not VerifiedOperatorApproval:
            _fail('restore_control_approval_invalid')
        try:
            valid = (all(_is_sha(value) for value in (
                verified.operator_digest, verified.approval_digest, verified.nonce_hash))
                and verified.operator_digest == self._binding['operator_authority_sha256']
                and type(verified.expires_at) is int)
        except AttributeError:
            valid = False
        if not valid:
            _fail('restore_control_approval_invalid')
        now = int(time.time())
        if not now < verified.expires_at <= now + 300:
            _fail('restore_control_approval_expired')
        with self._transaction() as (conn, snapshot):
            if snapshot['decision'] is not None:
                _fail('restore_control_finalized')
            if conn.execute('SELECT 1 FROM r3_approvals WHERE nonce_hash=? LIMIT 1',
                            (verified.nonce_hash,)).fetchone():
                _fail('restore_control_nonce_consumed')
            count = conn.execute('SELECT count(*) FROM r3_approvals').fetchone()[0]
            if count >= min(MAX_APPROVALS, _SCHEMA_APPROVAL_LIMIT):
                _fail('restore_control_approval_limit')
            # Recheck time after obtaining the write lock, not only before waiting.
            now = int(time.time())
            if not now < verified.expires_at <= now + 300:
                _fail('restore_control_approval_expired')
            conn.execute('INSERT INTO r3_approvals VALUES(?,?,?,?,?)', (
                count + 1, verified.operator_digest, verified.approval_digest,
                verified.nonce_hash, verified.expires_at))

    def record_preparation(self, authority: str, receipt_sha256: str, file_sha256: str) -> None:
        if (type(authority) is not str or authority not in _AUTHORITIES
                or not _is_sha(receipt_sha256) or not _is_sha(file_sha256)):
            _fail('restore_control_preparation_invalid')
        with self._transaction() as (conn, snapshot):
            previous = snapshot['preparations'].get(authority)
            record = {'receipt_sha256': receipt_sha256, 'file_sha256': file_sha256}
            if previous is not None:
                if previous != record:
                    _fail('restore_control_preparation_conflict')
                return
            if snapshot['decision'] is not None:
                _fail('restore_control_finalized')
            conn.execute('INSERT INTO r3_preparations VALUES(?,?,?)',
                         (authority, receipt_sha256, file_sha256))

    def complete(self, *, approval_digest: str, native_file_sha256: str) -> dict:
        if not _is_sha(approval_digest) or not _is_sha(native_file_sha256):
            _fail('restore_control_decision_conflict')
        with self._transaction() as (conn, snapshot):
            previous = snapshot['decision']
            if previous is not None:
                if (previous['approval_digest'] != approval_digest
                        or previous['native_file_sha256'] != native_file_sha256):
                    _fail('restore_control_decision_conflict')
                return previous
            if (len(snapshot['preparations']) != 3
                    or snapshot['approval_digest'] != approval_digest):
                _fail('restore_control_incomplete')
            accepted = conn.execute('SELECT seq,expires_at FROM r3_approvals ORDER BY seq DESC LIMIT 1').fetchone()
            if accepted is None or accepted[1] <= int(time.time()):
                _fail('restore_control_approval_expired')
            decision = self._decision(self._r3_id, snapshot['preparations'], approval_digest, native_file_sha256)
            conn.execute('INSERT INTO r3_decision VALUES(1,?,?,?)',
                         (accepted[0], _canonical(decision), decision['decision_sha256']))
            return decision
