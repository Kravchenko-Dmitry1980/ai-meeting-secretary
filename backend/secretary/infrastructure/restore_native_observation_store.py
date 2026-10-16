"""Durable managed-native diagnostic attempts; receipts are never authority.

The root authenticates the exact commitment with OperatorAuthority before passing
its DTO. This journal cannot verify authentication or actual process/GET custody.
Caller-supplied evidence kinds remain diagnostic, including synthetic records.
Windows IO custody is reused without changing the provider journal's purpose.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import re
import time
from uuid import uuid4

from . import restore_provider_observation_store as io
from .restore_operator import VerifiedOperatorApproval, _win32_path

_PURPOSE = 'restore_managed_native_observation'
_KINDS = ('managed_native_diagnostic', 'synthetic_managed_native')
_RESOURCES = frozenset(('configuration_sha256', 'binary_sha256', 'native_config_sha256',
    'launch_sha256', 'credential_sha256', 'native_binding_sha256', 'principal_projection_sha256'))
_CODES = frozenset(('managed_native_observed', 'managed_native_invalid',
    'managed_native_configuration', 'managed_native_source_changed', 'managed_native_approval',
    'managed_native_process_failed', 'managed_native_get_failed', 'managed_native_timeout',
    'managed_native_cleanup_failed', 'managed_native_readback_failed', 'managed_native_custody_failed',
    'managed_native_interrupted', 'native_readonly_get_unreconciled'))
_HASHES = ('get_sha256', 'process_sha256', 'final_snapshot_sha256', 'custody_sha256')
_TERMINAL = frozenset(('state', 'evidence_kind', 'code', 'started_at', 'finished_at',
    'diagnostic_only', 'activation_supported', 'outbound_enabled', *_HASHES))
_ERRORS = frozenset('native_observation_store_' + suffix for suffix in
    ('invalid', 'binding', 'path', 'schema', 'approval', 'expired', 'replay',
     'unfinished', 'limit', 'commitment', 'terminal', 'busy', 'io'))
_MAX_RECORDS = 128


class NativeObservationStoreError(io.ObservationStoreError):
    def __init__(self, code='native_observation_store_invalid'):
        self.code = code if type(code) is str and code in _ERRORS else 'native_observation_store_invalid'
        ValueError.__init__(self, self.code)


def _fail(suffix='invalid'):
    raise NativeObservationStoreError('native_observation_store_' + suffix) from None


def _binding(value):
    if (type(value) is not dict or set(value) != io._BINDING_KEYS
            or type(value['protocol']) is not int or value['protocol'] != 1
            or value['purpose'] != _PURPOSE
            or any(not io._is_uuid(value[k]) for k in ('restore_id', 'epoch_id', 'preparation_id'))
            or any(not io._is_sha(value[k]) for k in
                ('source_snapshot_sha256', 'scope_sha256', 'binding_sha256'))): _fail('binding')
    return io._json(io._canonical(value))


def _commitment(value, binding):
    if (type(value) is not dict or set(value) !=
            {'binding', 'evidence_kind', 'resources', 'attempt_context_sha256'}
            or _binding(value['binding']) != binding or value['evidence_kind'] not in _KINDS
            or type(value['resources']) is not dict or set(value['resources']) != _RESOURCES
            or not all(io._is_sha(v) for v in value['resources'].values())
            or not io._is_sha(value['attempt_context_sha256'])): _fail('commitment')
    return io._json(io._canonical(value))


def _utc(value):
    try:
        if (type(value) is not str or re.fullmatch(
                r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', value) is None): _fail('terminal')
        parsed = datetime.fromisoformat(value[:-1] + '+00:00')
        if parsed.tzinfo != timezone.utc or parsed.year < 1970: _fail('terminal')
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError): _fail('terminal')


def _now_utc():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _terminal(value, started):
    if (type(value) is not dict or set(value) != _TERMINAL
            or value['state'] not in ('observed', 'failed', 'unreconciled')
            or value['evidence_kind'] != started['commitment']['evidence_kind']
            or type(value['code']) is not str or value['code'] not in _CODES
            or value['diagnostic_only'] is not True or value['activation_supported'] is not False
            or value['outbound_enabled'] is not False
            or any(v is not None and not io._is_sha(v) for v in (value[k] for k in _HASHES))
            or _utc(value['started_at']) != _utc(started['started_at'])
            or _utc(value['finished_at']) < _utc(value['started_at'])): _fail('terminal')
    if value['state'] == 'observed':
        if value['code'] != 'managed_native_observed' or any(value[k] is None for k in _HASHES): _fail('terminal')
    elif value['code'] == 'managed_native_observed': _fail('terminal')
    return io._json(io._canonical(value))


_TABLES = {
    'native_binding': 'CREATE TABLE native_binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
        'store_id TEXT NOT NULL UNIQUE,binding TEXT NOT NULL,binding_sha256 TEXT NOT NULL)',
    'native_events': 'CREATE TABLE native_events(seq INTEGER PRIMARY KEY CHECK(seq BETWEEN 1 AND 128),'
        "kind TEXT NOT NULL CHECK(kind IN ('started','terminal')),attempt_id TEXT NOT NULL,"
        'payload TEXT NOT NULL CHECK(json_valid(payload) AND length(payload)<=65536),'
        'payload_sha256 TEXT NOT NULL,previous_record_sha256 TEXT,record_sha256 TEXT NOT NULL UNIQUE,'
        'approval_digest TEXT UNIQUE,nonce_hash TEXT UNIQUE,UNIQUE(attempt_id,kind))',
}
_TRIGGERS = {}
for _table in _TABLES:
    for _event in ('UPDATE', 'DELETE'):
        _name = _table + '_immutable_' + _event.lower()
        _TRIGGERS[_name] = (_table, f'CREATE TRIGGER {_name} BEFORE {_event} ON {_table} '
            "BEGIN SELECT RAISE(ABORT,'native_immutable'); END")
_TRIGGERS['native_binding_insert_guard'] = ('native_binding',
    'CREATE TRIGGER native_binding_insert_guard BEFORE INSERT ON native_binding '
    "WHEN EXISTS(SELECT 1 FROM native_binding) BEGIN SELECT RAISE(ABORT,'native_immutable'); END")
_TRIGGERS['native_events_insert_guard'] = ('native_events',
    'CREATE TRIGGER native_events_insert_guard BEFORE INSERT ON native_events '
    'WHEN EXISTS(SELECT 1 FROM native_events WHERE record_sha256=NEW.record_sha256 OR '
    '(NEW.approval_digest IS NOT NULL AND approval_digest=NEW.approval_digest) OR '
    '(NEW.nonce_hash IS NOT NULL AND nonce_hash=NEW.nonce_hash) OR '
    '(attempt_id=NEW.attempt_id AND kind=NEW.kind)) OR '
    'NEW.seq!=(SELECT count(*)+1 FROM native_events) OR '
    'NEW.previous_record_sha256 IS NOT (SELECT record_sha256 FROM native_events ORDER BY seq DESC LIMIT 1) OR '
    "(NEW.kind='started' AND (NEW.approval_digest IS NULL OR NEW.nonce_hash IS NULL OR "
    "EXISTS(SELECT 1 FROM native_events s WHERE s.kind='started' AND NOT EXISTS"
    "(SELECT 1 FROM native_events t WHERE t.kind='terminal' AND t.attempt_id=s.attempt_id)))) OR "
    "(NEW.kind='terminal' AND (NEW.approval_digest IS NOT NULL OR NEW.nonce_hash IS NOT NULL OR "
    "NOT EXISTS(SELECT 1 FROM native_events WHERE kind='started' AND attempt_id=NEW.attempt_id))) "
    "BEGIN SELECT RAISE(ABORT,'native_sequence'); END")
_SCHEMA = {**{k: ('table', k, v) for k, v in _TABLES.items()},
    **{k: ('trigger', t, v) for k, (t, v) in _TRIGGERS.items()},
    'sqlite_autoindex_native_binding_1': ('index', 'native_binding', None),
    **{f'sqlite_autoindex_native_events_{i}': ('index', 'native_events', None) for i in range(1, 5)}}


class NativeObservationStore(io.ObservationStore):
    """Separate purpose/schema, sharing only stopped SQLite/Windows IO custody."""
    @classmethod
    def create(cls, path: Path, binding: dict, *, private_folder: Path):
        instance = cls.__new__(cls)
        instance._setup_native(path, binding, private_folder)
        try:
            with instance._custody():
                with io.os.fdopen(instance._open_descriptor(create=True), 'wb') as stream:
                    stream.flush(); io.os.fsync(stream.fileno())
                    instance._identity = instance._file_identity(io.os.fstat(stream.fileno()))
                    instance._initialize()
            instance._open()
        except NativeObservationStoreError: raise
        except io.ObservationStoreError: _fail('io')
        return instance

    @classmethod
    def open_existing(cls, path: Path, binding: dict, *, private_folder: Path):
        instance = cls.__new__(cls)
        instance._setup_native(path, binding, private_folder)
        instance._open()
        return instance

    def _setup_native(self, path, binding, private_folder):
        try:
            self._path = io._local_path(path)
            folder = io._local_path(private_folder)
            if self._path.parent != folder: _fail('path')
            self._binding = _binding(binding)
            self._io_path = Path(_win32_path(self._path))
            self._identity = self._store_id = None
        except NativeObservationStoreError: raise
        except (io.ObservationStoreError, TypeError, ValueError): _fail('path')

    @contextmanager
    def _connect(self, *, write):
        try:
            with super()._connect(write=write) as conn: yield conn
        except NativeObservationStoreError: raise
        except io.ObservationStoreError as error:
            _fail('busy' if error.code == 'provider_observation_busy' else 'io')

    def _initialize(self):
        conn = None
        try:
            self._check_initial_file()
            conn = sqlite3.connect(self._sqlite_uri(write=True), uri=True, timeout=.2, isolation_level=None)
            self._configure(conn, read_only=False)
            if conn.execute('PRAGMA journal_mode').fetchone() != ('delete',): _fail()
            conn.execute('PRAGMA synchronous=FULL'); conn.execute('BEGIN EXCLUSIVE')
            self._check_initial_file(locked=True)
            if conn.execute('SELECT name FROM main.sqlite_master LIMIT 1').fetchone(): _fail()
            for sql in (*_TABLES.values(), *(v for _, v in _TRIGGERS.values())): conn.execute(sql)
            conn.execute('INSERT INTO native_binding VALUES(1,?,?,?)',
                (str(uuid4()), io._canonical(self._binding), io._digest(self._binding)))
            self._validate(conn); conn.commit()
        except NativeObservationStoreError: raise
        except (io.ObservationStoreError, sqlite3.Error, OSError, ValueError): _fail('io')
        finally:
            if conn is not None:
                if conn.in_transaction: conn.rollback()
                conn.close()

    @staticmethod
    def _record(store_id, binding_hash, seq, kind, attempt_id, payload, previous):
        digest = io._digest(payload)
        record = dict(seq=seq, kind=kind, attempt_id=attempt_id, payload=payload,
            payload_sha256=digest, previous_record_sha256=previous)
        record['record_sha256'] = io._digest(dict(protocol=1, purpose=_PURPOSE, store_id=store_id,
            binding_sha256=binding_hash, seq=seq, kind=kind, attempt_id=attempt_id,
            payload_sha256=digest, previous_record_sha256=previous))
        return record

    def _validate(self, conn):
        try:
            io._namespace(conn)
            rows = conn.execute('SELECT type,name,tbl_name,sql FROM main.sqlite_master LIMIT 65').fetchall()
            if {n: (k, t, s) for k, n, t, s in rows} != _SCHEMA: _fail('schema')
            bindings = conn.execute('SELECT * FROM main.native_binding LIMIT 2').fetchall()
            if len(bindings) != 1: _fail()
            singleton, store_id, raw, binding_hash = bindings[0]
            if (singleton != 1 or not io._is_uuid(store_id) or self._store_id not in (None, store_id)
                    or _binding(io._json(raw)) != self._binding or raw != io._canonical(self._binding)
                    or binding_hash != io._digest(self._binding)): _fail('binding')
            rows = conn.execute('SELECT * FROM main.native_events ORDER BY seq LIMIT 129').fetchall()
            if len(rows) > _MAX_RECORDS: _fail('limit')
            events, attempts, approvals, nonces, previous = [], {}, set(), set(), None
            for index, (seq, kind, attempt_id, raw, digest, predecessor, record_hash, approval, nonce) in enumerate(rows, 1):
                if seq != index or not io._is_uuid(attempt_id) or predecessor != previous: _fail()
                payload = io._json(raw)
                if kind == 'started':
                    if (set(payload) != {'attempt_id', 'commitment', 'commitment_sha256', 'operator_digest',
                            'operator_approval_sha256', 'nonce_hash', 'expires_at', 'started_at'}
                            or attempt_id in attempts or any(a['terminal'] is None for a in attempts.values())
                            or payload['attempt_id'] != attempt_id or approval in approvals or nonce in nonces
                            or payload['operator_approval_sha256'] != approval or payload['nonce_hash'] != nonce
                            or not all(io._is_sha(v) for v in (approval, nonce, payload['operator_digest']))
                            or type(payload['expires_at']) is not int
                            or not _utc(payload['started_at']) < payload['expires_at'] <= _utc(payload['started_at']) + 300): _fail()
                    commitment = _commitment(payload['commitment'], self._binding)
                    if io._digest(commitment) != payload['commitment_sha256']: _fail('commitment')
                    attempts[attempt_id] = {**payload, 'terminal': None}
                    approvals.add(approval); nonces.add(nonce)
                elif kind == 'terminal':
                    if (set(payload) != {'commitment_sha256', 'terminal'} or attempt_id not in attempts
                            or attempts[attempt_id]['terminal'] is not None or approval is not None or nonce is not None
                            or payload['commitment_sha256'] != attempts[attempt_id]['commitment_sha256']): _fail('terminal')
                    terminal = _terminal(payload['terminal'], attempts[attempt_id])
                    if terminal['state'] == 'observed' and _utc(terminal['finished_at']) >= attempts[attempt_id]['expires_at']: _fail('expired')
                    attempts[attempt_id]['terminal'] = terminal
                else: _fail()
                record = self._record(store_id, binding_hash, seq, kind, attempt_id, payload, previous)
                if raw != io._canonical(payload) or digest != record['payload_sha256'] or record_hash != record['record_sha256']: _fail()
                events.append(record); previous = record_hash
            return dict(protocol=1, store_id=store_id, binding=self._binding,
                binding_digest_sha256=binding_hash, events=tuple(events), attempts=tuple(attempts.values()),
                diagnostic_only=True, activation_supported=False, outbound_enabled=False)
        except NativeObservationStoreError: raise
        except (io.ObservationStoreError, TypeError, ValueError, KeyError, UnicodeError, RecursionError): _fail()

    def _append(self, conn, snapshot, kind, attempt_id, payload, approval=None, nonce=None):
        if len(snapshot['events']) >= _MAX_RECORDS: _fail('limit')
        previous = snapshot['events'][-1]['record_sha256'] if snapshot['events'] else None
        record = self._record(snapshot['store_id'], snapshot['binding_digest_sha256'],
            len(snapshot['events']) + 1, kind, attempt_id, payload, previous)
        conn.execute('INSERT INTO native_events VALUES(?,?,?,?,?,?,?,?,?)',
            (record['seq'], kind, attempt_id, io._canonical(payload), record['payload_sha256'],
                previous, record['record_sha256'], approval, nonce))
        return {**payload, 'record_sha256': record['record_sha256']}

    def start_attempt(self, verified, commitment):
        """Atomically consume a root-authenticated DTO and start before any launch."""
        if type(verified) is not VerifiedOperatorApproval: _fail('approval')
        if (not all(io._is_sha(v) for v in (verified.operator_digest, verified.approval_digest, verified.nonce_hash))
                or type(verified.expires_at) is not int): _fail('approval')
        value = _commitment(commitment, self._binding)
        with self._transaction() as (conn, snapshot):
            if any(a['terminal'] is None for a in snapshot['attempts']): _fail('unfinished')
            if any(a['operator_approval_sha256'] == verified.approval_digest or a['nonce_hash'] == verified.nonce_hash
                    for a in snapshot['attempts']): _fail('replay')
            if not time.time() < verified.expires_at <= time.time() + 300: _fail('expired')
            # Reserve room for a terminal: even the capacity boundary can fail closed.
            if len(snapshot['events']) > _MAX_RECORDS - 2: _fail('limit')
            payload = dict(attempt_id=str(uuid4()), commitment=value, commitment_sha256=io._digest(value),
                operator_digest=verified.operator_digest, operator_approval_sha256=verified.approval_digest,
                nonce_hash=verified.nonce_hash, expires_at=verified.expires_at, started_at=_now_utc())
            record = self._append(conn, snapshot, 'started', payload['attempt_id'], payload,
                verified.approval_digest, verified.nonce_hash)
            if not time.time() < verified.expires_at: _fail('expired')
        return record

    def finish_attempt(self, attempt_id, commitment_sha256, terminal):
        with self._transaction() as (conn, snapshot):
            started = next((a for a in snapshot['attempts'] if a['attempt_id'] == attempt_id), None)
            if started is None or started['terminal'] is not None: _fail('terminal')
            if commitment_sha256 != started['commitment_sha256']: _fail('commitment')
            value = _terminal(terminal, started)
            if _utc(value['finished_at']) > time.time(): _fail('terminal')
            if value['state'] == 'observed' and not time.time() < started['expires_at']: _fail('expired')
            record = self._append(conn, snapshot, 'terminal', attempt_id,
                dict(commitment_sha256=commitment_sha256, terminal=value))
            if value['state'] == 'observed' and not time.time() < started['expires_at']: _fail('expired')
        return record

    def consume_approval(self, verified): _fail('approval')
    def append_observation(self, payload): _fail('terminal')
