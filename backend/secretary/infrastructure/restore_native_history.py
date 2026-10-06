"""Declarative native-history precursor; no writer, activation or owner permit.

The 42-table catalogue was observed in the exact pinned FREE v2.7.0 native dump.
The SQL plan is bound to a PREPARED proposal, never the final activation authority.
A future orchestrator must supply real custody/proofs and retain history elsewhere.
Path reads require an inactive source; connection reads use the caller's actual
transaction view. Neither reading nor constructing a DTO authorizes any work.
This detects missing protocol and rollback below independently observed history;
it does not detect all arbitrary writes by the same Windows user.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID

PROTOCOL_VERSION = 1
PROTOCOL_PREFIX = 'secretary_native_activation_v1_'
EPOCH_TABLE = PROTOCOL_PREFIX + 'epoch'
HISTORY_TABLE = PROTOCOL_PREFIX + 'history'
PINNED_NATIVE_VERSION = '2.7.0'
PINNED_NATIVE_BINARY_SHA256 = 'e485792c33f537124fb187658a84099a74ab0f1b948a44dee9049fede1ad66b6'
PINNED_UPSTREAM_COMMIT = 'a16be96aa454671fdf213b0fbe411dd38a098418'
PINNED_REGISTERED_TABLES = (
    'api_tokens', 'buckets', 'favorites', 'files', 'label_tasks', 'labels',
    'license_status', 'link_shares', 'migration', 'migration_status',
    'notifications', 'oauth_codes', 'project_ancestors', 'project_task_counters',
    'project_views', 'projects', 'reactions', 'saved_filters', 'sessions',
    'subscriptions', 'task_assignees', 'task_attachments', 'task_buckets',
    'task_comments', 'task_index_aliases', 'task_positions', 'task_relations',
    'task_reminders', 'task_unread_statuses', 'tasks', 'team_members',
    'team_projects', 'teams', 'time_entries', 'totp', 'unsplash_photos',
    'user_invite_link_teams', 'user_invite_links', 'user_tokens', 'users',
    'users_projects', 'webhooks',
)
MAX_HISTORY_EVENTS = 100000
MAX_SCHEMA_OBJECTS = 1024
MAX_CELL_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_READ_SECONDS = 10
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_MIGRATION = re.compile(r'(?:[0-9]{14}|SCHEMA_INIT)\Z')
_ERROR_REASONS = frozenset({
    'native_binding_invalid', 'native_schema_bounded', 'native_schema_invalid',
    'native_catalogue_changed', 'native_migration_set_changed',
    'native_connection_invalid', 'native_source_invalid',
    'native_retained_head_invalid', 'native_protocol_missing',
    'native_protocol_schema_changed', 'native_base_schema_changed',
    'native_epoch_marker_changed', 'native_history_bounded',
    'native_history_invalid', 'native_history_rollback',
    'native_source_journaled', 'native_source_path_invalid',
    'native_source_reparse', 'native_source_missing', 'native_source_bounded',
    'native_source_changed',
})


class NativeHistoryError(ValueError):
    def __init__(self, reason):
        self.code = 'maintenance_restore_blocked'
        self.reason = reason if type(reason) is str and reason in _ERROR_REASONS else 'native_source_invalid'
        super().__init__(self.code)


def _fail(reason):
    raise NativeHistoryError(reason)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


CATALOGUE_SHA256 = _digest({'native_version': PINNED_NATIVE_VERSION,
    'native_binary_sha256': PINNED_NATIVE_BINARY_SHA256, 'tables': PINNED_REGISTERED_TABLES})


def catalogue_metadata():
    """Provenance, not a claim that a caller's source or runtime was qualified."""
    return {'native_version': PINNED_NATIVE_VERSION,
        'native_binary_sha256': PINNED_NATIVE_BINARY_SHA256,
        'upstream_commit': PINNED_UPSTREAM_COMMIT,
        'source_catalogue': 'REGISTERED_TABLE_NAMES_SOURCE_VERIFIED',
        'native_catalogue': 'OBSERVED_EXACT_PINNED_NATIVE_DUMP',
        'catalogue_sha256': CATALOGUE_SHA256, 'table_count': len(PINNED_REGISTERED_TABLES),
        'history_runtime_qualification': 'SEPARATE_EVIDENCE_REQUIRED'}


@dataclass(frozen=True)
class NativeHistoryBinding:
    deployment_id: str
    epoch_id: str
    decision_id: str
    decision_sha256: str
    expected_native_schema_sha256: str
    expected_migration_ids: tuple[str, ...]


@dataclass(frozen=True)
class NativeSourceDescriptor:
    schema_sha256: str
    migration_ids: tuple[str, ...]
    registered_tables: tuple[str, ...]
    catalogue_sha256: str


@dataclass(frozen=True)
class NativeHistoryStatement:
    sql: str
    parameters: tuple = ()


@dataclass(frozen=True)
class NativeHistoryPlan:
    binding_sha256: str
    catalogue_sha256: str
    statements: tuple[NativeHistoryStatement, ...]
    # (name,type,tbl_name,sql); includes the exact generated unique autoindex.
    protocol_schema: tuple[tuple[str, str, str, str | None], ...]


@dataclass(frozen=True)
class NativeHistoryHead:
    epoch_id: str
    sequence: int
    nonce: str | None
    digest: str


@dataclass(frozen=True)
class NativeHistorySnapshot:
    head: NativeHistoryHead
    event_count: int
    binding_sha256: str
    catalogue_sha256: str
    native_schema_sha256: str
    qualification: str = 'DIAGNOSTIC_ONLY'


def _valid_uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _binding(binding):
    if (not isinstance(binding, NativeHistoryBinding)
            or any(not _valid_uuid(value) for value in (binding.deployment_id, binding.epoch_id, binding.decision_id))
            or any(type(value) is not str or not _SHA.fullmatch(value)
                   for value in (binding.decision_sha256, binding.expected_native_schema_sha256))
            or type(binding.expected_migration_ids) is not tuple
            or not 1 <= len(binding.expected_migration_ids) <= 512
            or any(type(value) is not str or not _MIGRATION.fullmatch(value) for value in binding.expected_migration_ids)
            or binding.expected_migration_ids != tuple(sorted(set(binding.expected_migration_ids)))
            or 'SCHEMA_INIT' not in binding.expected_migration_ids):
        _fail('native_binding_invalid')
    return _digest({'protocol_version': PROTOCOL_VERSION, 'catalogue_sha256': CATALOGUE_SHA256,
                    'prepared_proposal': asdict(binding)})


def _own_object_names():
    names = {EPOCH_TABLE, HISTORY_TABLE, f'sqlite_autoindex_{HISTORY_TABLE}_1'}
    for table, short in ((EPOCH_TABLE, 'epoch'), (HISTORY_TABLE, 'history')):
        for operation in ('insert', 'update', 'delete'):
            names.add(PROTOCOL_PREFIX + short + '_' + operation)
    for table in PINNED_REGISTERED_TABLES:
        for operation in ('insert', 'update', 'delete'):
            for kind in ('guard', 'history'):
                names.add(PROTOCOL_PREFIX + table + '_' + kind + '_' + operation)
    return frozenset(names)


def _schema(conn):
    rows = conn.execute('SELECT name,type,tbl_name,sql FROM sqlite_master ORDER BY name LIMIT ?',
                        (MAX_SCHEMA_OBJECTS + 1,)).fetchall()
    if len(rows) > MAX_SCHEMA_OBJECTS:
        _fail('native_schema_bounded')
    result = tuple(tuple(row) for row in rows)
    for row in result:
        if (any(type(value) is not str for value in row[:3])
                or (row[3] is not None and type(row[3]) is not str)
                or any(isinstance(value, str) and len(value.encode()) > MAX_CELL_BYTES for value in row)):
            _fail('native_schema_invalid')
    return result


def _descriptor(conn, protocol_schema=()):
    schema = _schema(conn)
    # Exclude only exact approved object records, including SQL. An object with
    # an own-looking name but a changed type/target/body stays in the fingerprint.
    approved = frozenset(protocol_schema)
    base = tuple(row for row in schema if row not in approved)
    tables = tuple(sorted(row[0] for row in base if row[1] == 'table' and row[0] != 'sqlite_sequence'))
    if tables != PINNED_REGISTERED_TABLES:
        _fail('native_catalogue_changed')
    migrations = tuple(row[0] for row in conn.execute('SELECT id FROM migration ORDER BY id LIMIT 513'))
    if (not 1 <= len(migrations) <= 512 or any(type(value) is not str or not _MIGRATION.fullmatch(value) for value in migrations)
            or migrations != tuple(sorted(set(migrations))) or 'SCHEMA_INIT' not in migrations):
        _fail('native_migration_set_changed')
    for table in PINNED_REGISTERED_TABLES:
        # A future version using WITHOUT ROWID must be separately qualified.
        conn.execute(f'SELECT _rowid_ FROM "{table}" LIMIT 0')
    return NativeSourceDescriptor(_digest(base), migrations, tables, CATALOGUE_SHA256)


def native_schema_descriptor(conn, *, binding=None):
    """Read the actual supplied source schema; descriptor is never a permit."""
    if not isinstance(conn, sqlite3.Connection):
        _fail('native_connection_invalid')
    try:
        protocol = build_install_plan(binding).protocol_schema if binding is not None else ()
        return _descriptor(conn, protocol)
    except NativeHistoryError:
        raise
    except (sqlite3.Error, ValueError, TypeError, AttributeError, OverflowError):
        _fail('native_source_invalid')


def build_install_plan(binding):
    """Return SQL only. No connection, schema writes, admission or activation."""
    binding_sha = _binding(binding)
    marker = (1, PROTOCOL_VERSION, binding.deployment_id, binding.epoch_id,
              binding.decision_id, binding.decision_sha256, binding_sha, CATALOGUE_SHA256)
    epoch_sql = (f'CREATE TABLE "{EPOCH_TABLE}" (id INTEGER PRIMARY KEY CHECK(id=1), '
        'protocol_version INTEGER NOT NULL CHECK(protocol_version=1), deployment_id TEXT NOT NULL, '
        'epoch_id TEXT NOT NULL, decision_id TEXT NOT NULL, decision_sha256 TEXT NOT NULL, '
        'binding_sha256 TEXT NOT NULL, catalogue_sha256 TEXT NOT NULL)')
    history_sql = (f'CREATE TABLE "{HISTORY_TABLE}" (seq INTEGER PRIMARY KEY CHECK(seq>0), '
        'epoch_id TEXT NOT NULL, nonce TEXT NOT NULL UNIQUE, previous_nonce TEXT, '
        'table_name TEXT NOT NULL, operation TEXT NOT NULL, native_rowid INTEGER NOT NULL)')
    statements = [NativeHistoryStatement(epoch_sql), NativeHistoryStatement(history_sql),
        NativeHistoryStatement(f'INSERT INTO "{EPOCH_TABLE}" VALUES(?,?,?,?,?,?,?,?)', marker)]
    objects = [(EPOCH_TABLE, 'table', EPOCH_TABLE, epoch_sql),
               (HISTORY_TABLE, 'table', HISTORY_TABLE, history_sql),
               (f'sqlite_autoindex_{HISTORY_TABLE}_1', 'index', HISTORY_TABLE, None)]
    expected_epoch = (f'(SELECT count(*) FROM "{EPOCH_TABLE}" WHERE id=1 '
        f"AND protocol_version=1 AND binding_sha256='{binding_sha}' AND epoch_id='{binding.epoch_id}')=1")

    def trigger(name, table, timing, operation, body, when=''):
        sql = f'CREATE TRIGGER "{name}" {timing} {operation} ON "{table}"' + (f' WHEN {when}' if when else '') + ' BEGIN ' + body + ' END'
        statements.append(NativeHistoryStatement(sql))
        objects.append((name, 'trigger', table, sql))

    for table, short in ((EPOCH_TABLE, 'epoch'), (HISTORY_TABLE, 'history')):
        for operation in ('UPDATE', 'DELETE'):
            trigger(PROTOCOL_PREFIX + short + '_' + operation.lower(), table, 'BEFORE', operation,
                    "SELECT RAISE(ABORT,'native_activation_history_immutable');")
    trigger(PROTOCOL_PREFIX + 'epoch_insert', EPOCH_TABLE, 'BEFORE', 'INSERT',
            "SELECT RAISE(ABORT,'native_activation_epoch_immutable');", f'EXISTS(SELECT 1 FROM "{EPOCH_TABLE}")')
    allowed_tables = ','.join("'" + value + "'" for value in PINNED_REGISTERED_TABLES)
    valid_history = (f"{expected_epoch} AND NEW.epoch_id='{binding.epoch_id}' "
        f'AND (NEW.seq=-1 OR NEW.seq=COALESCE((SELECT max(seq) FROM "{HISTORY_TABLE}"),0)+1) '
        f'AND NEW.previous_nonce IS (SELECT nonce FROM "{HISTORY_TABLE}" ORDER BY seq DESC LIMIT 1) '
        "AND length(NEW.nonce)=64 AND NEW.nonce NOT GLOB '*[^0-9a-f]*' "
        f'AND NOT EXISTS(SELECT 1 FROM "{HISTORY_TABLE}" WHERE nonce=NEW.nonce) '
        f"AND NEW.table_name IN ({allowed_tables}) AND NEW.operation IN ('INSERT','UPDATE','DELETE') "
        "AND typeof(NEW.native_rowid)='integer'")
    trigger(PROTOCOL_PREFIX + 'history_insert', HISTORY_TABLE, 'BEFORE', 'INSERT',
            "SELECT RAISE(ABORT,'native_activation_history_invalid');", 'NOT (' + valid_history + ')')
    for table in PINNED_REGISTERED_TABLES:
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            ref = 'OLD' if operation == 'DELETE' else 'NEW'
            trigger(PROTOCOL_PREFIX + table + '_guard_' + operation.lower(), table, 'BEFORE', operation,
                    "SELECT RAISE(ABORT,'native_activation_epoch_required');", 'NOT (' + expected_epoch + ')')
            trigger(PROTOCOL_PREFIX + table + '_history_' + operation.lower(), table, 'AFTER', operation,
                f'INSERT INTO "{HISTORY_TABLE}"(epoch_id,nonce,previous_nonce,table_name,operation,native_rowid) '
                f"SELECT '{binding.epoch_id}',lower(hex(randomblob(32))),"
                f'(SELECT nonce FROM "{HISTORY_TABLE}" ORDER BY seq DESC LIMIT 1),'
                f"'{table}','{operation}',{ref}._rowid_;")
    return NativeHistoryPlan(binding_sha, CATALOGUE_SHA256, tuple(statements), tuple(sorted(objects)))


def _head(binding, retained_head):
    if retained_head is None:
        return
    if (not isinstance(retained_head, NativeHistoryHead) or retained_head.epoch_id != binding.epoch_id
            or type(retained_head.sequence) is not int or not 0 <= retained_head.sequence <= MAX_HISTORY_EVENTS
            or type(retained_head.digest) is not str or not _SHA.fullmatch(retained_head.digest)
            or (retained_head.sequence == 0 and retained_head.nonce is not None)
            or (retained_head.sequence > 0 and (type(retained_head.nonce) is not str or not _SHA.fullmatch(retained_head.nonce)))):
        _fail('native_retained_head_invalid')


def _read(conn, binding, retained_head, *, prepared_binding=None):
    plan = build_install_plan(binding)
    _head(binding, retained_head)
    schema = _schema(conn)
    own = tuple(row for row in schema if row[0] in _own_object_names())
    if not own:
        _fail('native_protocol_missing')
    if own != plan.protocol_schema:
        _fail('native_protocol_schema_changed')
    extra = ()
    if prepared_binding is not None:
        # Closed preparation-only integration: validate ALL exact hold objects
        # and rows before excluding any of them. No generic exclusion input.
        from .restore_native_preparation import _read_hold
        if prepared_binding.native != binding:
            _fail('native_binding_invalid')
        extra = _read_hold(conn, prepared_binding).metadata_schema
    descriptor = _descriptor(conn, (*plan.protocol_schema, *extra))
    if descriptor.schema_sha256 != binding.expected_native_schema_sha256:
        _fail('native_base_schema_changed')
    if descriptor.migration_ids != binding.expected_migration_ids:
        _fail('native_migration_set_changed')
    marker = conn.execute(f'SELECT * FROM "{EPOCH_TABLE}" LIMIT 2').fetchall()
    expected = (1, PROTOCOL_VERSION, binding.deployment_id, binding.epoch_id,
                binding.decision_id, binding.decision_sha256, plan.binding_sha256, CATALOGUE_SHA256)
    if tuple(tuple(row) for row in marker) != (expected,):
        _fail('native_epoch_marker_changed')
    rows = conn.execute(f'SELECT seq,epoch_id,nonce,previous_nonce,table_name,operation,native_rowid '
                        f'FROM "{HISTORY_TABLE}" ORDER BY seq LIMIT ?', (MAX_HISTORY_EVENTS + 1,))
    digest = _digest({'genesis': plan.binding_sha256})
    sequence, nonce = 0, None
    matched = retained_head is None
    if retained_head is not None and retained_head.sequence == 0:
        matched = retained_head.nonce is None and retained_head.digest == digest
    started = time.monotonic()
    for row in rows:
        event = tuple(row)
        if sequence >= MAX_HISTORY_EVENTS or time.monotonic() - started > MAX_READ_SECONDS:
            _fail('native_history_bounded')
        if (len(event) != 7 or type(event[0]) is not int or event[0] != sequence + 1
                or event[1] != binding.epoch_id or type(event[2]) is not str or not _SHA.fullmatch(event[2])
                or event[3] != nonce or event[4] not in PINNED_REGISTERED_TABLES
                or event[5] not in ('INSERT', 'UPDATE', 'DELETE') or type(event[6]) is not int):
            _fail('native_history_invalid')
        sequence, nonce = event[0], event[2]
        digest = _digest({'previous_digest': digest, 'event': event})
        if retained_head is not None and retained_head.sequence == sequence:
            matched = retained_head.nonce == nonce and retained_head.digest == digest
    if retained_head is not None and not matched:
        _fail('native_history_rollback')
    return NativeHistorySnapshot(NativeHistoryHead(binding.epoch_id, sequence, nonce, digest),
        sequence, plan.binding_sha256, CATALOGUE_SHA256, descriptor.schema_sha256)


def read_connection(conn, *, binding, retained_head=None):
    """Diagnostic of the caller's actual SQL view, not custody or admission proof."""
    if not isinstance(conn, sqlite3.Connection):
        _fail('native_connection_invalid')
    previous_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(previous_limit, MAX_CELL_BYTES))
        return _read(conn, binding, retained_head)
    except NativeHistoryError:
        raise
    except (sqlite3.Error, ValueError, TypeError, AttributeError, OverflowError):
        _fail('native_source_invalid')
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)


def _read_prepared_connection(conn, *, prepared_binding):
    """Private composed reader; the closed hold receipt supplies its genesis."""
    from .restore_native_preparation import _read_hold
    hold = _read_hold(conn, prepared_binding)
    return _read(conn, prepared_binding.native, hold.initial_head,
        prepared_binding=prepared_binding)


def _inactive(path):
    if any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')):
        _fail('native_source_journaled')


def _path(path):
    if not isinstance(path, (str, os.PathLike)):
        _fail('native_source_path_invalid')
    raw = os.fspath(path)
    if (type(raw) is not str or not raw or len(raw) > 4096
            or raw.replace('\\', '/').startswith('//') or any(ord(char) < 32 for char in raw)):
        _fail('native_source_path_invalid')
    parts = re.split(r'[\\/]', raw)
    if (any(part in ('.', '..') or part.rstrip(' .') != part for part in parts if part)
            or any(':' in part for part in parts[1:])):
        _fail('native_source_path_invalid')
    path = Path(raw)
    if not path.is_absolute():
        _fail('native_source_path_invalid')
    if os.name == 'nt' and any(re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])',
            part.split('.')[0], re.IGNORECASE) for part in path.parts[1:]):
        _fail('native_source_path_invalid')
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail('native_source_reparse')
    if not stat.S_ISREG(info.st_mode):
        _fail('native_source_missing')
    if info.st_nlink != 1:
        _fail('native_source_path_invalid')
    if not 0 < info.st_size <= MAX_SOURCE_BYTES:
        _fail('native_source_bounded')
    return path


def _identity(path):
    stat = path.stat()
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, digest


def read_existing(path, *, binding, retained_head=None):
    """Read inactive existing file without schema writes or auxiliary sidecars.

    A live/journaled source is refused instead of opening it with immutable=1.
    Source rechecks are diagnostic; actual write/delete custody belongs to R4.
    """
    _binding(binding)
    _head(binding, retained_head)
    try:
        path = _path(path)
        _inactive(path)
        before = _identity(path)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=.2)) as conn:
            def readonly(action, arg1, arg2, database, trigger):
                return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                    sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE) else sqlite3.SQLITE_DENY
            conn.set_authorizer(readonly)
            result = read_connection(conn, binding=binding, retained_head=retained_head)
        _inactive(path)
        if _identity(path) != before:
            _fail('native_source_changed')
        return result
    except NativeHistoryError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError, OverflowError):
        _fail('native_source_invalid')
