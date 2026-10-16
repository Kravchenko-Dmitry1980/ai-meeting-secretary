"""Controlled connection-only native preparation, always blocked.

The caller owns the real BEGIN EXCLUSIVE transaction, Windows file/directory
custody, stopped runtime verification, source identity and R3 byte provenance.
``in_transaction`` is a necessary condition, not proof of an exclusive lock.
This module never opens a path, commits, issues an epoch or grants activation.
The caller must roll back its transaction on any error, then commit/close,
reacquire deny-write custody and perform an independent durable readback.

History SQL remains the separately qualified precursor. The additional hold
composition requires its own native qualification. A proposed decision binding
is never the later global activation authority. There is no grant issuer here;
the empty future-grant table denies all DML at SQL level.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import sqlite3
import time
from uuid import UUID

from secretary.domain.restore_quarantine import encoded, typed
from . import restore_native_history as history

PROTOCOL_VERSION = 1
PREFIX = 'secretary_r4_native_'
HOLD_TABLE = PREFIX + 'prepared_hold'
GRANT_TABLE = PREFIX + 'future_grants'
MAX_BUSINESS_ROWS = 100000
MAX_BUSINESS_BYTES = 64 * 1024 * 1024
MAX_CELL_BYTES = 1024 * 1024
MAX_READ_SECONDS = 30
_SHA = re.compile('[0-9a-f]{64}\\Z')
_REASONS = frozenset(('native_preparation_invalid', 'native_preparation_binding_invalid',
    'native_preparation_connection_invalid', 'native_preparation_transaction_required',
    'native_preparation_schema_changed', 'native_preparation_partial',
    'native_preparation_hold_changed', 'native_preparation_grant_present',
    'native_preparation_history_nonzero', 'native_preparation_business_changed',
    'native_preparation_bounded'))


class NativePreparationError(ValueError):
    code = 'maintenance_restore_blocked'

    def __init__(self, reason='native_preparation_invalid'):
        self.reason = reason if type(reason) is str and reason in _REASONS else 'native_preparation_invalid'
        super().__init__(self.code)


def _fail(reason='native_preparation_invalid'):
    raise NativePreparationError(reason) from None


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('ascii')).hexdigest()


def _uuid(value):
    try:
        return type(value) is str and len(value) == 36 and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class NativePreparationBinding:
    native: history.NativeHistoryBinding
    restore_id: str
    r3_id: str
    generation_id: str
    ledger_id: str
    source_commitment_sha256: str
    r3_preparation_sha256: str
    baseline_file_sha256: str
    source_path_sha256: str
    file_id: tuple[int, int]
    baseline_rowids_sha256: str


@dataclass(frozen=True)
class NativeHoldPlan:
    binding_sha256: str
    native_binding_sha256: str
    initial_head: history.NativeHistoryHead
    statements: tuple[history.NativeHistoryStatement, ...]
    protocol_schema: tuple[tuple[str, str, str, str | None], ...]
    state: str = 'source_prepared_blocked'
    activation_supported: bool = False
    outbound_enabled: bool = False


@dataclass(frozen=True)
class NativeHoldReceipt:
    binding_sha256: str
    native_binding_sha256: str
    initial_head: history.NativeHistoryHead
    receipt_sha256: str
    metadata_schema: tuple[tuple[str, str, str, str | None], ...]
    metadata_tables: tuple[str, ...]
    state: str = 'source_prepared_blocked'
    activation_supported: bool = False
    outbound_enabled: bool = False


@dataclass(frozen=True)
class PreparedNativeSnapshot:
    native_snapshot: history.NativeHistorySnapshot
    hold_receipt: NativeHoldReceipt
    binding_sha256: str
    receipt_sha256: str
    metadata_schema: tuple[tuple[str, str, str, str | None], ...]
    metadata_tables: tuple[str, ...]
    business_tables: tuple[str, ...]
    business_rowids_sha256: str
    state: str = 'source_prepared_blocked'
    activation_supported: bool = False
    outbound_enabled: bool = False


def _binding(binding):
    if (type(binding) is not NativePreparationBinding or type(binding.native) is not history.NativeHistoryBinding
            or not all(_uuid(getattr(binding, key)) for key in ('restore_id', 'r3_id', 'generation_id', 'ledger_id'))
            or any(type(getattr(binding, key)) is not str or not _SHA.fullmatch(getattr(binding, key))
                for key in ('source_commitment_sha256', 'r3_preparation_sha256', 'baseline_file_sha256',
                    'source_path_sha256', 'baseline_rowids_sha256'))
            or type(binding.file_id) is not tuple or len(binding.file_id) != 2
            or any(type(value) is not int or not 0 <= value < 2 ** 128 for value in binding.file_id)):
        _fail('native_preparation_binding_invalid')
    try:
        native_sha = history.build_install_plan(binding.native).binding_sha256
    except history.NativeHistoryError:
        _fail('native_preparation_binding_invalid')
    raw = _canonical(asdict(binding))
    return raw, _digest({'protocol_version': PROTOCOL_VERSION, 'prepared_binding': asdict(binding)}), native_sha


def build_hold_plan(binding):
    """Fixed SQL only; no storage access, custody, final authority or activation."""
    raw, binding_sha, native_sha = _binding(binding)
    head = history.NativeHistoryHead(binding.native.epoch_id, 0, None, history._digest({'genesis': native_sha}))
    hold_sql = (f'CREATE TABLE "{HOLD_TABLE}" (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
        'protocol_version INTEGER NOT NULL CHECK(protocol_version=1), binding_json TEXT NOT NULL, '
        'binding_sha256 TEXT NOT NULL, native_binding_sha256 TEXT NOT NULL, '
        'initial_sequence INTEGER NOT NULL CHECK(initial_sequence=0), '
        'initial_nonce TEXT CHECK(initial_nonce IS NULL), initial_digest TEXT NOT NULL)')
    grant_sql = (f'CREATE TABLE "{GRANT_TABLE}" (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
        'grant_sha256 TEXT NOT NULL)')
    marker = (1, PROTOCOL_VERSION, raw, binding_sha, native_sha, 0, None, head.digest)
    statements = [history.NativeHistoryStatement(hold_sql), history.NativeHistoryStatement(grant_sql),
        history.NativeHistoryStatement(f'INSERT INTO "{HOLD_TABLE}" VALUES(?,?,?,?,?,?,?,?)', marker)]
    objects = [(HOLD_TABLE, 'table', HOLD_TABLE, hold_sql), (GRANT_TABLE, 'table', GRANT_TABLE, grant_sql)]

    def deny(name, table, operation, when=''):
        sql = (f'CREATE TRIGGER "{name}" BEFORE {operation} ON "{table}"'
            + (f' WHEN {when}' if when else '')
            + " BEGIN SELECT RAISE(ABORT,'native_source_prepared_blocked'); END")
        statements.append(history.NativeHistoryStatement(sql))
        objects.append((name, 'trigger', table, sql))

    deny(PREFIX + 'hold_insert', HOLD_TABLE, 'INSERT', f'EXISTS(SELECT 1 FROM "{HOLD_TABLE}")')
    for operation in ('UPDATE', 'DELETE'):
        deny(PREFIX + 'hold_' + operation.lower(), HOLD_TABLE, operation)
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        deny(PREFIX + 'grant_' + operation.lower(), GRANT_TABLE, operation)
    for table in history.PINNED_REGISTERED_TABLES:
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            deny(PREFIX + table + '_deny_' + operation.lower(), table, operation)
    return NativeHoldPlan(binding_sha, native_sha, head, tuple(statements), tuple(sorted(objects)))


def _connection(conn):
    if not isinstance(conn, sqlite3.Connection):
        _fail('native_preparation_connection_invalid')
    databases = conn.execute('PRAGMA database_list').fetchall()
    if {row[1] for row in databases} - {'main', 'temp'}:
        _fail('native_preparation_connection_invalid')
    # The fixed history SQL uses unqualified table references. TEMP aliases
    # must not hide main grants, markers, migrations or business snapshots.
    if conn.execute('SELECT 1 FROM temp.sqlite_master LIMIT 1').fetchone() is not None:
        _fail('native_preparation_connection_invalid')


@contextmanager
def _bounded(conn):
    _connection(conn)
    previous = conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(previous, MAX_CELL_BYTES))
        yield
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous)


def _read_hold(conn, binding):
    _connection(conn)
    plan = build_hold_plan(binding)
    schema = history._schema(conn)
    found = tuple(row for row in schema if row[0].startswith(PREFIX)
        or row[0].startswith('sqlite_autoindex_' + PREFIX))
    if found != plan.protocol_schema:
        _fail('native_preparation_schema_changed')
    raw, binding_sha, native_sha = _binding(binding)
    marker = tuple(tuple(row) for row in conn.execute(f'SELECT * FROM "{HOLD_TABLE}" LIMIT 2'))
    expected = (1, PROTOCOL_VERSION, raw, binding_sha, native_sha, 0, None, plan.initial_head.digest)
    if marker != (expected,):
        _fail('native_preparation_hold_changed')
    if conn.execute(f'SELECT singleton FROM "{GRANT_TABLE}" LIMIT 1').fetchone() is not None:
        _fail('native_preparation_grant_present')
    receipt_sha = _digest({'protocol_version': PROTOCOL_VERSION, 'hold': expected,
        'protocol_schema': plan.protocol_schema, 'state': 'source_prepared_blocked'})
    return NativeHoldReceipt(binding_sha, native_sha, plan.initial_head, receipt_sha,
        plan.protocol_schema, tuple(sorted((HOLD_TABLE, GRANT_TABLE))))


def read_hold(conn, *, binding):
    """Verify the actual complete hold; frozen output is diagnostic only."""
    try:
        with _bounded(conn):
            return _read_hold(conn, binding)
    except NativePreparationError:
        raise
    except (history.NativeHistoryError, sqlite3.Error, OSError, ValueError, TypeError,
            AttributeError, OverflowError, RecursionError):
        _fail()


def _business_rowids(conn):
    # Same typed, ordered rowid encoding as R3, but only the exact 42 native
    # tables. Metadata is never chosen by a caller-supplied exclusion list.
    value = hashlib.sha256()
    rows = size = 0
    deadline = time.monotonic() + MAX_READ_SECONDS
    for table in history.PINNED_REGISTERED_TABLES:
        value.update(encoded(table).encode())
        for row in conn.execute(f'SELECT _rowid_,* FROM "{table}" ORDER BY _rowid_'):
            raw = encoded(typed(tuple(row))).encode()
            rows += 1
            size += len(raw)
            if rows > MAX_BUSINESS_ROWS or size > MAX_BUSINESS_BYTES or time.monotonic() >= deadline:
                _fail('native_preparation_bounded')
            value.update(len(raw).to_bytes(8, 'big'))
            value.update(raw)
    return value.hexdigest()


def _reserved(schema):
    return any(row[0].startswith((PREFIX, history.PROTOCOL_PREFIX,
        'sqlite_autoindex_' + PREFIX, 'sqlite_autoindex_' + history.PROTOCOL_PREFIX)) for row in schema)


def native_business_rowids(conn, *, binding=None):
    """Initial 42-table baseline or a fully verified composed prepared readback."""
    if binding is not None:
        return read_prepared_native(conn, binding=binding).business_rowids_sha256
    try:
        with _bounded(conn):
            if _reserved(history._schema(conn)):
                _fail('native_preparation_partial')
            history.native_schema_descriptor(conn)
            return _business_rowids(conn)
    except NativePreparationError:
        raise
    except (history.NativeHistoryError, sqlite3.Error, OSError, ValueError, TypeError,
            AttributeError, OverflowError, RecursionError):
        _fail()


def read_prepared_native(conn, *, binding):
    """Return exact metadata projection only after full actual source validation."""
    try:
        with _bounded(conn):
            hold = _read_hold(conn, binding)
            native = history._read_prepared_connection(conn, prepared_binding=binding)
            if native.head != hold.initial_head or native.event_count != 0:
                _fail('native_preparation_history_nonzero')
            business_sha = _business_rowids(conn)
            if business_sha != binding.baseline_rowids_sha256:
                _fail('native_preparation_business_changed')
            metadata = tuple(sorted((*history.build_install_plan(binding.native).protocol_schema,
                *hold.metadata_schema)))
            tables = tuple(sorted((history.EPOCH_TABLE, history.HISTORY_TABLE, HOLD_TABLE, GRANT_TABLE)))
            receipt_sha = _digest({'protocol_version': PROTOCOL_VERSION, 'binding_sha256': hold.binding_sha256,
                'hold_receipt_sha256': hold.receipt_sha256, 'native_snapshot': asdict(native),
                'business_rowids_sha256': business_sha, 'metadata_schema': metadata,
                'state': 'source_prepared_blocked'})
            return PreparedNativeSnapshot(native, hold, hold.binding_sha256, receipt_sha,
                metadata, tables, history.PINNED_REGISTERED_TABLES, business_sha)
    except NativePreparationError:
        raise
    except (history.NativeHistoryError, sqlite3.Error, OSError, ValueError, TypeError,
            AttributeError, OverflowError, RecursionError):
        _fail()


def prepare_native_source_in_connection(conn, *, binding):
    """Install once in the root-owned transaction; exact repeat performs no DDL.

    Source identity, actual exclusive custody and durable readback are caller
    responsibilities. No in_transaction-only exclusive-lock proof is claimed.
    """
    try:
        with _bounded(conn):
            plan = build_hold_plan(binding)
            if not conn.in_transaction:
                _fail('native_preparation_transaction_required')
            schema = history._schema(conn)
            if _reserved(schema):
                # The closed reader rejects partial, foreign and changed history
                # or hold objects. Never repair an ambiguous installed protocol.
                return read_prepared_native(conn, binding=binding)
            actual = history.native_schema_descriptor(conn)
            if (actual.schema_sha256 != binding.native.expected_native_schema_sha256
                    or actual.migration_ids != binding.native.expected_migration_ids):
                _fail('native_preparation_schema_changed')
            if _business_rowids(conn) != binding.baseline_rowids_sha256:
                _fail('native_preparation_business_changed')
            for statement in (*history.build_install_plan(binding.native).statements, *plan.statements):
                conn.execute(statement.sql, statement.parameters)
            return read_prepared_native(conn, binding=binding)
    except NativePreparationError:
        raise
    except (history.NativeHistoryError, sqlite3.Error, OSError, ValueError, TypeError,
            AttributeError, OverflowError, RecursionError):
        _fail()
