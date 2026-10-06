"""Durable R4 owner decision journal; every v1 decision remains blocked.

This writer binds a current owner receipt to a fresh read of the four restored
sources, current config and the existing provider-observation journal. It does
not call providers, mutate restored sources, enable runtime work, or issue an
activation grant. The owner nonce and blocked decision are one SQLite commit.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID, uuid4

from secretary.domain.restore_quarantine import digest
from .restore_business_owner import (
    BusinessOwnerAuthority, BusinessOwnerError, VerifiedBusinessOwnerConsent,
)
from .restore_fresh_evidence import FreshEvidenceError, prepared_restore_evidence
from .restore_operator import (
    OperatorError, _local, _win32_path, create_private_directory, protected_scope,
)
from .restore_provider_observation_store import ObservationStore, ObservationStoreError
from .restore_provider_observations import (
    ProviderObservationError, _binding as _provider_binding, _pinned_config, _requests,
)

_MAX_DATABASE_BYTES = 8 * 1024 * 1024
_MAX_RECORDS = 256
_MAX_SQL_OBJECTS = 48
_MAX_VALUE_BYTES = 32 * 1024
_TIMEOUT_SECONDS = .2
_READ_SECONDS = 1.0
_SHA = re.compile('[0-9a-f]{64}')
_ROLES = ('secretary', 'team', 'billing', 'vikunja')
_FIXED_BLOCKERS = frozenset((
    'shared_permission_predicate_missing',
    'native_authenticated_get_missing',
    'fresh_execution_lineage_missing',
))
_BLOCKERS = _FIXED_BLOCKERS | frozenset((
    'provider_observations_missing', 'provider_observation_synthetic',
    'provider_observation_incomplete', 'provider_observation_stale',
    'provider_history_incomplete', 'billing_liabilities_unresolved',
))
_CODES = frozenset('activation_decision_' + suffix for suffix in (
    'invalid', 'path_invalid', 'store_invalid', 'schema_invalid', 'missing',
    'exists', 'busy', 'io_failed', 'source_invalid', 'source_changed',
    'provider_invalid', 'consent_invalid', 'consent_replay', 'consent_conflict',
    'directory_invalid', 'decision_limit',
))


class ActivationDecisionError(ValueError):
    """Stable error codes only; no paths, SQL, consent, IDs or provider data."""

    def __init__(self, code='activation_decision_invalid'):
        self.code = code if type(code) is str and code in _CODES else 'activation_decision_invalid'
        super().__init__(self.code)


class _DecisionBodyError(Exception):
    def __init__(self, error):
        self.error = error


class _CurrentBodyError(Exception):
    def __init__(self, error):
        self.error = error


def _fail(code='activation_decision_invalid'):
    raise ActivationDecisionError(code) from None


def _is_sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _is_uuid(value):
    try:
        return type(value) is str and len(value) == 36 and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('ascii')).hexdigest()


def _commitment(value):
    try:
        from .restore_business_owner import _commitment as check_owner_commitment
        if type(value) is not dict:
            _fail()
        return check_owner_commitment(value, value.get('owner_policy_sha256'))
    except ActivationDecisionError:
        raise
    except (BusinessOwnerError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        _fail()


def _path(path):
    if not isinstance(path, Path):
        _fail('activation_decision_path_invalid')
    raw = str(path)
    parts = re.split(r'[\\/]', raw)
    if (not path.is_absolute() or not 0 < len(raw) <= 4096 or raw.startswith(('\\\\', '//'))
            or any(ord(char) < 32 for char in raw)
            or any(part in ('.', '..') or part.rstrip(' .') != part for part in parts if part)
            or any(':' in part for part in parts[1:]) or not path.name):
        _fail('activation_decision_path_invalid')
    return path


_LEDGER_BINDING_FIELDS = frozenset((
    'protocol', 'purpose', 'restore_id', 'epoch_id', 'preparation_id',
    'binding_sha256', 'scope_sha256', 'capabilities',
))


def _binding(value):
    if (type(value) is not dict or set(value) != _LEDGER_BINDING_FIELDS
            or type(value['protocol']) is not int or value['protocol'] != 1
            or value['purpose'] != 'restore_activation_decision_ledger'
            or any(not _is_uuid(value[key]) for key in (
                'restore_id', 'epoch_id', 'preparation_id'))
            or any(not _is_sha(value[key]) for key in ('binding_sha256', 'scope_sha256'))
            or type(value['capabilities']) is not list or not 1 <= len(value['capabilities']) <= 8
            or any(type(item) is not str or not item or len(item) > 64 for item in value['capabilities'])
            or sorted(set(value['capabilities'])) != value['capabilities']):
        _fail('activation_decision_store_invalid')
    return {**value, 'capabilities': list(value['capabilities'])}


def _ledger_binding(commitment):
    value = _commitment(commitment)
    return _binding(dict(protocol=1, purpose='restore_activation_decision_ledger',
        restore_id=value['restore_id'], epoch_id=value['epoch_id'],
        preparation_id=value['preparation_id'], binding_sha256=value['binding_sha256'],
        scope_sha256=value['scope_sha256'], capabilities=list(value['capabilities'])))


def _sha_column(name):
    return (f"{name} TEXT NOT NULL CHECK(typeof({name})='text' AND length({name})=64 "
        f"AND {name} NOT GLOB '*[^0-9a-f]*')")


_TABLES = {
    'decision_binding': ('CREATE TABLE decision_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'store_id TEXT NOT NULL CHECK(length(store_id)=36),'
        'binding TEXT NOT NULL CHECK(json_valid(binding) AND length(binding)<=8192),'
        + _sha_column('binding_sha256') + ')'),
    'activation_decisions': ('CREATE TABLE activation_decisions('
        'decision_id TEXT PRIMARY KEY CHECK(length(decision_id)=36),'
        "seq INTEGER NOT NULL UNIQUE CHECK(typeof(seq)='integer' AND seq BETWEEN 1 AND 256),"
        'payload TEXT NOT NULL CHECK(json_valid(payload) AND length(payload)<=32768),'
        + _sha_column('payload_sha256') + ',previous_record_sha256 TEXT CHECK('
        "previous_record_sha256 IS NULL OR (typeof(previous_record_sha256)='text' "
        "AND length(previous_record_sha256)=64 AND previous_record_sha256 NOT GLOB '*[^0-9a-f]*')),"
        + _sha_column('record_sha256') + ')'),
    'owner_consent_uses': ('CREATE TABLE owner_consent_uses('
        + _sha_column('nonce_hash') + ' PRIMARY KEY,'
        'decision_id TEXT NOT NULL UNIQUE REFERENCES activation_decisions(decision_id),'
        + _sha_column('consent_digest') + ',' + _sha_column('decision_key_sha256') + ')'),
}


def _trigger(name, table, event, condition=None):
    when = f' WHEN {condition}' if condition else ''
    return (f'CREATE TRIGGER {name} BEFORE {event} ON {table}{when} '
        "BEGIN SELECT RAISE(ABORT,'activation_decision_immutable'); END")


_TRIGGERS = {}
for _table in _TABLES:
    for _event in ('UPDATE', 'DELETE'):
        _name = f'{_table}_immutable_{_event.lower()}'
        _TRIGGERS[_name] = (_table, _trigger(_name, _table, _event))
_TRIGGERS['decision_binding_insert_guard'] = ('decision_binding', _trigger(
    'decision_binding_insert_guard', 'decision_binding', 'INSERT',
    'EXISTS(SELECT 1 FROM decision_binding)'))
_TRIGGERS['activation_decisions_append_guard'] = ('activation_decisions', _trigger(
    'activation_decisions_append_guard', 'activation_decisions', 'INSERT',
    'NEW.seq != (SELECT count(*)+1 FROM activation_decisions) OR '
    'NEW.previous_record_sha256 IS NOT (SELECT record_sha256 FROM activation_decisions ORDER BY seq DESC LIMIT 1) OR '
    f'(SELECT count(*) FROM activation_decisions)>={_MAX_RECORDS}'))
_TRIGGERS['owner_consent_uses_insert_guard'] = ('owner_consent_uses', _trigger(
    'owner_consent_uses_insert_guard', 'owner_consent_uses', 'INSERT',
    'EXISTS(SELECT 1 FROM owner_consent_uses WHERE nonce_hash=NEW.nonce_hash) OR '
    'NOT EXISTS(SELECT 1 FROM activation_decisions WHERE decision_id=NEW.decision_id)'))

_DDL = (*_TABLES.values(), *(value[1] for value in _TRIGGERS.values()))
_EXPECTED_SCHEMA = {
    **{name: ('table', name, sql) for name, sql in _TABLES.items()},
    **{name: ('trigger', table, sql) for name, (table, sql) in _TRIGGERS.items()},
    'sqlite_autoindex_activation_decisions_1': ('index', 'activation_decisions', None),
    'sqlite_autoindex_activation_decisions_2': ('index', 'activation_decisions', None),
    'sqlite_autoindex_owner_consent_uses_1': ('index', 'owner_consent_uses', None),
    'sqlite_autoindex_owner_consent_uses_2': ('index', 'owner_consent_uses', None),
}


def _decision_record(store_id, binding_sha256, seq, payload, previous):
    payload_hash = _digest(payload)
    record_hash = _digest(dict(protocol=1, store_id=store_id,
        binding_sha256=binding_sha256, seq=seq, payload_sha256=payload_hash,
        previous_record_sha256=previous))
    return dict(decision_id=payload['decision_id'], seq=seq, payload=payload,
        payload_sha256=payload_hash, previous_record_sha256=previous,
        record_sha256=record_hash)


def _decision_key(commitment, *, consent_digest, nonce_hash, owner_digest, owner_policy_sha256):
    return _digest(dict(commitment_sha256=_digest(_commitment(commitment)), consent_digest=consent_digest,
        nonce_hash=nonce_hash, owner_digest=owner_digest,
        owner_policy_sha256=owner_policy_sha256))


def _payload(value, binding, *, previous):
    keys = frozenset(('protocol', 'purpose', 'decision_id', 'seq', 'state',
        'restore_id', 'epoch_id', 'preparation_id', 'source_snapshot_sha256',
        'binding_sha256', 'scope_sha256', 'provider_observations_sha256',
        'resource_scope_sha256', 'capabilities', 'owner_digest', 'owner_policy_sha256',
        'consent_digest', 'nonce_hash', 'decision_key_sha256', 'created_at',
        'expires_at', 'blockers', 'previous_record_sha256'))
    if type(value) is not dict or set(value) != keys:
        _fail('activation_decision_store_invalid')
    if (type(value['protocol']) is not int or value['protocol'] != 1
            or value['purpose'] != 'restore_activation_decision'
            or not _is_uuid(value['decision_id'])
            or type(value['seq']) is not int or not 1 <= value['seq'] <= _MAX_RECORDS
            or value['state'] != 'activation_blocked'
            or any(value[key] != binding[key] for key in (
                'restore_id', 'epoch_id', 'preparation_id', 'binding_sha256',
                'scope_sha256', 'capabilities'))
            or any(not _is_sha(value[key]) for key in (
                'source_snapshot_sha256', 'provider_observations_sha256',
                'resource_scope_sha256', 'owner_digest', 'owner_policy_sha256',
                'consent_digest', 'nonce_hash', 'decision_key_sha256'))
            or type(value['created_at']) is not int or not 0 < value['created_at'] < 2**63
            or type(value['expires_at']) is not int or value['expires_at'] != value['created_at'] + 300
            or type(value['blockers']) is not list
            or any(type(item) is not str or item not in _BLOCKERS for item in value['blockers'])
            or value['blockers'] != sorted(set(value['blockers']))
            or not _FIXED_BLOCKERS.issubset(value['blockers'])
            or value['previous_record_sha256'] != previous):
        _fail('activation_decision_store_invalid')
    commitment = _commitment(dict(protocol=1, purpose='restore_activation_consent',
        restore_id=value['restore_id'], epoch_id=value['epoch_id'],
        preparation_id=value['preparation_id'],
        source_snapshot_sha256=value['source_snapshot_sha256'],
        binding_sha256=value['binding_sha256'], scope_sha256=value['scope_sha256'],
        provider_observations_sha256=value['provider_observations_sha256'],
        resource_scope_sha256=value['resource_scope_sha256'],
        capabilities=list(value['capabilities']), owner_policy_sha256=value['owner_policy_sha256']))
    expected_key = _decision_key(commitment, consent_digest=value['consent_digest'],
        nonce_hash=value['nonce_hash'], owner_digest=value['owner_digest'],
        owner_policy_sha256=value['owner_policy_sha256'])
    if _ledger_binding(commitment) != binding or value['decision_key_sha256'] != expected_key:
        _fail('activation_decision_store_invalid')
    return dict(value)


class ActivationDecisionLedger:
    """Independent append-only store. It cannot represent an allowing state."""

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
                _fail('activation_decision_exists')
            except OSError:
                _fail('activation_decision_io_failed')
            with os.fdopen(fd, 'wb') as stream:
                stream.flush()
                os.fsync(stream.fileno())
                instance._identity = instance._file_identity(os.fstat(stream.fileno()))
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
        self._store_id = None
        if not isinstance(path, Path):
            _fail('activation_decision_path_invalid')
        self._path = _path(path)

    @contextmanager
    def _custody(self):
        try:
            with protected_scope(self._path.parent):
                try:
                    yield
                except ActivationDecisionError as error:
                    raise _DecisionBodyError(error) from None
        except _DecisionBodyError as error:
            raise error.error from None
        except OperatorError:
            _fail('activation_decision_path_invalid')
        except OSError:
            _fail('activation_decision_io_failed')

    @staticmethod
    def _file_identity(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _unsafe(info):
        return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, 'st_file_attributes', 0) & 0x400))

    def _check_file(self):
        try:
            info = self._path.lstat()
            identity = self._file_identity(info)
            if (not stat.S_ISREG(info.st_mode) or self._unsafe(info) or info.st_nlink != 1
                    or not 100 <= info.st_size <= _MAX_DATABASE_BYTES
                    or self._identity is not None and identity != self._identity):
                _fail()
            for suffix in ('-wal', '-shm', '-journal'):
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
        except ActivationDecisionError:
            raise
        except FileNotFoundError:
            _fail('activation_decision_missing')
        except OSError:
            _fail('activation_decision_io_failed')

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
                with self._path.open('rb') as stream:
                    if self._file_identity(os.fstat(stream.fileno())) != self._identity:
                        _fail()
                    self._check_file()
                    uri = self._path.as_uri() + ('?mode=rw' if write else '?mode=ro')
                    conn = sqlite3.connect(uri, uri=True, timeout=_TIMEOUT_SECONDS, isolation_level=None)
                    self._configure(conn, read_only=not write)
                    if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',):
                        _fail()
                    yield conn
            except ActivationDecisionError:
                raise
            except sqlite3.Error as exc:
                code = getattr(exc, 'sqlite_errorcode', None)
                _fail('activation_decision_busy' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    else 'activation_decision_store_invalid')
            except (OSError, ValueError, TypeError, UnicodeError):
                _fail('activation_decision_io_failed')
            finally:
                if conn is not None:
                    if conn.in_transaction:
                        conn.rollback()
                    conn.close()
                self._check_file()

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
            conn.execute('INSERT INTO decision_binding VALUES(1,?,?,?)',
                (str(uuid4()), self._binding_json, self._binding_sha256))
            conn.commit()
        except ActivationDecisionError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError):
            _fail('activation_decision_io_failed')
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()

    def _validate_schema(self, conn):
        rows = conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master '
            'ORDER BY name LIMIT ?', (_MAX_SQL_OBJECTS + 1,)).fetchall()
        if len(rows) > _MAX_SQL_OBJECTS:
            _fail('activation_decision_schema_invalid')
        found = {}
        for kind, name, table, sql in rows:
            if (type(name) is not str or name in found
                    or _EXPECTED_SCHEMA.get(name) != (kind, table, sql)):
                _fail('activation_decision_schema_invalid')
            found[name] = (kind, table, sql)
        if found != _EXPECTED_SCHEMA:
            _fail('activation_decision_schema_invalid')

    def _validate(self, conn):
        self._validate_schema(conn)
        rows = conn.execute('SELECT singleton,store_id,binding,binding_sha256 '
            'FROM decision_binding LIMIT 2').fetchall()
        if len(rows) != 1:
            _fail('activation_decision_store_invalid')
        singleton, store_id, raw_binding, binding_hash = rows[0]
        if (type(singleton) is not int or singleton != 1 or not _is_uuid(store_id)
                or self._store_id is not None and self._store_id != store_id
                or raw_binding != self._binding_json or binding_hash != self._binding_sha256):
            _fail('activation_decision_store_invalid')
        rows = conn.execute('SELECT decision_id,seq,payload,payload_sha256,'
            'previous_record_sha256,record_sha256 FROM activation_decisions ORDER BY seq LIMIT ?',
            (_MAX_RECORDS + 1,)).fetchall()
        if len(rows) > _MAX_RECORDS:
            _fail('activation_decision_schema_invalid')
        decisions, seen_ids, previous = [], set(), None
        for expected_seq, row in enumerate(rows, 1):
            decision_id, seq, raw, payload_hash, predecessor, record_hash = row
            if (type(seq) is not int or seq != expected_seq or type(raw) is not str
                    or not 0 < len(raw) <= _MAX_VALUE_BYTES or not _is_sha(payload_hash)
                    or not _is_sha(record_hash) or predecessor != previous):
                _fail('activation_decision_store_invalid')
            try:
                value = json.loads(raw, object_pairs_hook=_unique_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, TypeError, UnicodeError, RecursionError):
                _fail('activation_decision_store_invalid')
            value = _payload(value, self._binding, previous=previous)
            if value['seq'] != seq or value['decision_id'] != decision_id or decision_id in seen_ids:
                _fail('activation_decision_store_invalid')
            if raw != _canonical(value) or payload_hash != _digest(value):
                _fail('activation_decision_store_invalid')
            record = _decision_record(store_id, self._binding_sha256, seq, value, previous)
            if record['record_sha256'] != record_hash:
                _fail('activation_decision_store_invalid')
            decisions.append(record)
            seen_ids.add(decision_id)
            previous = record_hash
        uses = conn.execute('SELECT nonce_hash,decision_id,consent_digest,decision_key_sha256 '
            'FROM owner_consent_uses ORDER BY decision_id LIMIT ?', (_MAX_RECORDS + 1,)).fetchall()
        if len(uses) != len(decisions):
            _fail('activation_decision_store_invalid')
        by_id = {row['decision_id']: row for row in (record['payload'] for record in decisions)}
        seen_nonces = set()
        for nonce_hash, decision_id, consent_digest, decision_key in uses:
            record = by_id.get(decision_id)
            if (record is None or nonce_hash in seen_nonces or nonce_hash != record['nonce_hash']
                    or consent_digest != record['consent_digest']
                    or decision_key != record['decision_key_sha256']):
                _fail('activation_decision_store_invalid')
            seen_nonces.add(nonce_hash)
        if conn.execute('PRAGMA foreign_key_check').fetchmany(1):
            _fail('activation_decision_store_invalid')
        return dict(protocol=1, store_id=store_id, binding=dict(self._binding),
            binding_sha256=self._binding_sha256, decisions=tuple(decisions),
            used_owner_nonce_hashes=tuple(sorted(seen_nonces)),
            current=decisions[-1]['payload'] if decisions else None,
            state='activation_blocked' if decisions else 'decision_not_recorded',
            activation_supported=False, outbound=False, outbound_enabled=False)

    def _open(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            snapshot = self._validate(conn)
        self._store_id = snapshot['store_id']

    @contextmanager
    def _transaction(self):
        with self._connect(write=False) as conn:
            conn.execute('BEGIN')
            self._validate(conn)
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

    def _append_blocked(self, consent, commitment, blockers, *, precommit):
        try:
            commitment = _commitment(commitment)
        except ActivationDecisionError:
            _fail('activation_decision_consent_invalid')
        if (type(consent) is not VerifiedBusinessOwnerConsent
                or any(not _is_sha(getattr(consent, name)) for name in (
                    'owner_digest', 'owner_policy_sha256', 'consent_digest', 'nonce_hash'))
                or type(consent.expires_at) is not int or not 0 < consent.expires_at < 2**63
                or type(blockers) is not tuple or blockers != tuple(sorted(set(blockers)))
                or not _FIXED_BLOCKERS.issubset(blockers)
                or any(type(code) is not str or code not in _BLOCKERS for code in blockers)
                or _ledger_binding(commitment) != self._binding
                or consent.owner_policy_sha256 != commitment['owner_policy_sha256']
                or not callable(precommit)):
            _fail('activation_decision_consent_invalid')
        key = _decision_key(commitment, consent_digest=consent.consent_digest,
            nonce_hash=consent.nonce_hash, owner_digest=consent.owner_digest,
            owner_policy_sha256=consent.owner_policy_sha256)
        with self._transaction() as (conn, snapshot):
            existing = next((record for record in snapshot['decisions']
                if record['payload']['nonce_hash'] == consent.nonce_hash), None)
            if existing is not None:
                payload = existing['payload']
                if payload['consent_digest'] != consent.consent_digest or payload['decision_key_sha256'] != key:
                    _fail('activation_decision_consent_conflict')
                precommit(True)
                return payload
            if len(snapshot['decisions']) >= _MAX_RECORDS:
                _fail('activation_decision_decision_limit')
            seq = len(snapshot['decisions']) + 1
            previous = snapshot['decisions'][-1]['record_sha256'] if snapshot['decisions'] else None
            created_at = consent.expires_at - 300
            payload = dict(protocol=1, purpose='restore_activation_decision',
                decision_id=str(uuid4()), seq=seq, state='activation_blocked',
                restore_id=commitment['restore_id'], epoch_id=commitment['epoch_id'],
                preparation_id=commitment['preparation_id'],
                source_snapshot_sha256=commitment['source_snapshot_sha256'],
                binding_sha256=commitment['binding_sha256'],
                scope_sha256=commitment['scope_sha256'],
                provider_observations_sha256=commitment['provider_observations_sha256'],
                resource_scope_sha256=commitment['resource_scope_sha256'],
                capabilities=list(commitment['capabilities']),
                owner_digest=consent.owner_digest, owner_policy_sha256=consent.owner_policy_sha256,
                consent_digest=consent.consent_digest, nonce_hash=consent.nonce_hash,
                decision_key_sha256=key, created_at=created_at, expires_at=consent.expires_at,
                blockers=list(blockers), previous_record_sha256=previous)
            payload = _payload(payload, self._binding, previous=previous)
            record = _decision_record(snapshot['store_id'], self._binding_sha256, seq, payload, previous)
            conn.execute('INSERT INTO activation_decisions VALUES(?,?,?,?,?,?)',
                (record['decision_id'], seq, _canonical(payload), record['payload_sha256'],
                    previous, record['record_sha256']))
            conn.execute('INSERT INTO owner_consent_uses VALUES(?,?,?,?)',
                (consent.nonce_hash, record['decision_id'], consent.consent_digest, key))
            precommit(False)
            return payload


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _provider_snapshot(project_root, evidence):
    from .restore_operator import _win32_path
    directory = (_local(project_root) / '.runtime/team-operator/activations' /
        evidence.restore_id / 'source-epochs' / evidence.epoch_id / 'provider-observations')
    path = directory / 'observations.sqlite3'
    binding = _provider_binding(evidence)
    if not os.path.lexists(_win32_path(path)):
        for suffix in ('-wal', '-shm', '-journal'):
            if os.path.lexists(_win32_path(Path(str(path) + suffix))):
                _fail('activation_decision_provider_invalid')
        return dict(protocol=1, purpose='restore_provider_observation',
            binding=binding, state='missing', observations=()), None
    try:
        store = ObservationStore.open_existing(path, binding)
        return store.snapshot(), store
    except ObservationStoreError:
        _fail('activation_decision_provider_invalid')


def _fresh_provider_blockers(snapshot, resources, liabilities):
    blockers = set(_FIXED_BLOCKERS)
    if liabilities:
        blockers.add('billing_liabilities_unresolved')
    expected, held = _requests(resources, liabilities)
    if held:
        blockers.add('provider_history_incomplete')
    if snapshot is None or snapshot.get('state') == 'missing':
        blockers.add('provider_observations_missing')
        return tuple(sorted(blockers))
    rows = [item['payload'] for item in snapshot['observations']]
    if any(item.get('evidence_kind') != 'production_get' for item in rows):
        blockers.add('provider_observation_synthetic')
    now = datetime.now(timezone.utc)
    seen = set()
    for provider, operation, credential, request_id, liability_sha in expected:
        request_sha = digest([provider, operation, request_id])
        resource_sha = digest([resources, request_id, liability_sha])
        key = (provider, operation, credential, request_sha, resource_sha)
        matching = [item for item in rows if (item.get('provider'), item.get('operation'),
            item.get('credential_sha256'), item.get('request_sha256'), item.get('resource_sha256')) == key]
        if len(matching) != 1:
            blockers.add('provider_observation_incomplete')
            continue
        item = matching[0]
        try:
            expires = datetime.strptime(item['expires_at'], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            blockers.add('provider_observation_incomplete')
            continue
        if item.get('state') != 'observed' or item.get('error_code') is not None:
            blockers.add('provider_observation_incomplete')
        if expires <= now:
            blockers.add('provider_observation_stale')
        seen.add(key)
    if len(seen) != len(expected):
        blockers.add('provider_observation_incomplete')
    for item in rows:
        if item.get('state') != 'observed' or item.get('error_code') is not None:
            blockers.add('provider_observation_incomplete')
        try:
            expires = datetime.strptime(item['expires_at'], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            blockers.add('provider_observation_incomplete')
        else:
            if expires <= now:
                blockers.add('provider_observation_stale')
    return tuple(sorted(blockers))


@contextmanager
def _current_context(project_root, backup_dir, restore_dir, *, maintenance_path,
        lifecycle_path, config_path):
    args = dict(project_root=project_root, backup_dir=backup_dir, restore_dir=restore_dir,
        maintenance_path=maintenance_path, lifecycle_path=lifecycle_path)
    try:
        with prepared_restore_evidence(**args) as evidence:
            with _pinned_config(project_root, config_path, evidence) as (config, resources, verify_config):
                liabilities = evidence.read_liabilities()
                provider_snapshot, _ = _provider_snapshot(project_root, evidence)
                authority = BusinessOwnerAuthority(project_root)
                commitment = dict(protocol=1, purpose='restore_activation_consent',
                    restore_id=evidence.restore_id, epoch_id=evidence.epoch_id,
                    preparation_id=evidence.preparation_id,
                    source_snapshot_sha256=evidence.snapshot_sha256,
                    binding_sha256=evidence.binding_sha256, scope_sha256=evidence.scope_sha256,
                    provider_observations_sha256=digest(provider_snapshot),
                    resource_scope_sha256=digest(resources),
                    capabilities=sorted(evidence.capabilities),
                    owner_policy_sha256=authority.owner_policy_sha256)
                commitment = _commitment(commitment)
                blockers = _fresh_provider_blockers(provider_snapshot, resources, liabilities)
                liability_sha = digest([asdict(row) for row in liabilities])
                config_sha = resources['configuration_sha256']
                current = dict(root=_local(project_root), evidence=evidence, authority=authority,
                    commitment=commitment, blockers=blockers, provider_snapshot=provider_snapshot,
                    verify_config=verify_config, resources=resources,
                    liability_sha256=liability_sha, configuration_sha256=config_sha,
                    arguments=args, config_path=config_path)
                body_error = None
                try:
                    yield current
                except BaseException as error:
                    body_error = error
                finally:
                    verify_config()
                    evidence.verify_current()
                if body_error is not None:
                    raise _CurrentBodyError(body_error) from None
    except _CurrentBodyError as error:
        raise error.error from None
    except ActivationDecisionError:
        raise
    except (FreshEvidenceError, ProviderObservationError, ObservationStoreError,
            BusinessOwnerError, OperatorError):
        _fail('activation_decision_source_invalid')
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            sqlite3.Error, UnicodeError, OverflowError, RecursionError):
        _fail('activation_decision_source_invalid')


def _fixed_store(root, evidence, binding):
    main_dir = root / '.runtime/team-operator/activations' / evidence.restore_id
    if not main_dir.is_dir():
        _fail('activation_decision_directory_invalid')
    directory = main_dir / 'decisions-v1'
    created = False
    if not os.path.lexists(_win32_path(directory)):
        try:
            create_private_directory(directory)
            created = True
        except (OperatorError, OSError, ValueError):
            _fail('activation_decision_directory_invalid')
    if not directory.is_dir():
        _fail('activation_decision_directory_invalid')
    path = directory / 'decision.sqlite3'
    if os.path.lexists(_win32_path(path)):
        return ActivationDecisionLedger.open_existing(path, binding)
    if not created:
        _fail('activation_decision_missing')
    return ActivationDecisionLedger.create(path, binding)


def current_activation_consent_commitment(project_root, backup_dir, restore_dir, *,
        maintenance_path, lifecycle_path, config_path):
    """Read-only current commitment for the separate business-owner consent ceremony."""
    try:
        with _current_context(project_root, backup_dir, restore_dir,
                maintenance_path=maintenance_path, lifecycle_path=lifecycle_path,
                config_path=config_path) as current:
            current['verify_config']()
            current['evidence'].verify_current()
            return dict(current['commitment'])
    except ActivationDecisionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error,
            UnicodeError, OverflowError, RecursionError):
        _fail('activation_decision_source_invalid')


def record_blocked_activation_decision(project_root, backup_dir, restore_dir, *,
        maintenance_path, lifecycle_path, config_path, owner_receipt):
    """Re-authenticate exact current owner consent and append only a blocked decision."""
    if type(owner_receipt) is not dict:
        _fail('activation_decision_consent_invalid')
    try:
        with _current_context(project_root, backup_dir, restore_dir,
                maintenance_path=maintenance_path, lifecycle_path=lifecycle_path,
                config_path=config_path) as current:
            authority = current['authority']
            commitment = current['commitment']
            verified = authority.authenticate(commitment, owner_receipt)
            if type(verified) is not VerifiedBusinessOwnerConsent:
                _fail('activation_decision_consent_invalid')
            evidence = current['evidence']

            def precommit(replay):
                evidence.verify_current()
                current['verify_config']()
                if current['resources']['configuration_sha256'] != current['configuration_sha256']:
                    _fail('activation_decision_source_changed')
                if digest([asdict(row) for row in evidence.read_liabilities()]) != current['liability_sha256']:
                    _fail('activation_decision_source_changed')
                provider_snapshot, _ = _provider_snapshot(project_root, evidence)
                if digest(provider_snapshot) != commitment['provider_observations_sha256']:
                    _fail('activation_decision_source_changed')
                if not replay:
                    current_liabilities = evidence.read_liabilities()
                    if _fresh_provider_blockers(provider_snapshot, current['resources'],
                            current_liabilities) != current['blockers']:
                        _fail('activation_decision_source_changed')
                rechecked = authority.authenticate(commitment, owner_receipt)
                if rechecked != verified:
                    _fail('activation_decision_consent_invalid')

            store = _fixed_store(current['root'], evidence, _ledger_binding(commitment))
            payload = store._append_blocked(verified, commitment, current['blockers'], precommit=precommit)
            snapshot = store.snapshot()
            if (snapshot['current'] != payload or snapshot['activation_supported'] is not False
                    or snapshot['outbound_enabled'] is not False):
                _fail('activation_decision_store_invalid')
            return dict(protocol=1, decision=payload, decision_id=payload['decision_id'],
                blockers=payload['blockers'], state='activation_blocked',
                activation_supported=False, outbound=False, outbound_enabled=False)
    except ActivationDecisionError:
        raise
    except (FreshEvidenceError, ProviderObservationError, ObservationStoreError,
            BusinessOwnerError, OperatorError):
        _fail('activation_decision_consent_invalid')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error,
            UnicodeError, OverflowError, RecursionError):
        _fail('activation_decision_invalid')
