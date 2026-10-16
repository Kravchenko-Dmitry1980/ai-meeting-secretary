"""Declarative, append-only R2 evidence; this module cannot apply its SQL.

Only a future R3 controlled local transaction may execute the returned batch.
The reader accepts an already supplied connection and never opens a database,
commits, changes its authorizer, clears the restore guard or grants activation.
Its deadline checks are cooperative; it preserves the caller's existing SQLite
progress handler. R3 must supply an interruptible controlled connection when
an in-flight SQL deadline is required. No whole-operation SLA is claimed here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
import time
from uuid import UUID

from secretary.domain.restore_quarantine import (
    AUTHORITIES, PROTOCOL_VERSION, QuarantinePlan, RestoreQuarantineError, digest,
)

PROTOCOL_TABLES = ('restore_quarantine_protocol', 'restore_quarantine_sets',
                   'restore_quarantine_entries', 'restore_quarantine_links')
MAX_ROWS = 100000
MAX_CONTENT_BYTES = 64 * 1024 * 1024
MAX_CELL_BYTES = 1024 * 1024
MAX_SECONDS = 30
MAX_PROTOCOL_RECORDS = 300000
_INVALID = 'restore_quarantine_preparation_invalid'


@dataclass(frozen=True)
class SQLStatement:
    sql: str
    params: tuple = ()


@dataclass(frozen=True)
class PreparationProgram:
    statements: tuple[SQLStatement, ...]
    state: str = 'quarantine_prepared_only'
    activation_supported: bool = False


@dataclass(frozen=True)
class PreparedReceipt:
    authority: str
    restore_id: str
    input_sha256: str
    plan_sha256: str
    catalogue_sha256: str
    source_schema_sha256: str
    source_logical_sha256: str
    source_original_file_sha256: str
    entries_count: int
    entries_sha256: str
    links_count: int
    links_sha256: str
    prepared_by_R3_id: str
    activation_supported: bool = False


_TABLE_DDL = {
    PROTOCOL_TABLES[0]: '''CREATE TABLE restore_quarantine_protocol(
        version INTEGER PRIMARY KEY CHECK(version=1),
        catalogue_sha256 TEXT NOT NULL CHECK(length(catalogue_sha256)=64))''',
    PROTOCOL_TABLES[1]: '''CREATE TABLE restore_quarantine_sets(
        restore_id TEXT NOT NULL,authority TEXT NOT NULL,
        protocol_version INTEGER NOT NULL CHECK(protocol_version=1),
        catalogue_sha256 TEXT NOT NULL,input_sha256 TEXT NOT NULL,
        plan_sha256 TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,
        metadata_sha256 TEXT NOT NULL,bindings_sha256 TEXT NOT NULL,
        preview_sha256 TEXT NOT NULL,source_schema_sha256 TEXT NOT NULL,
        source_logical_sha256 TEXT NOT NULL,source_original_file_sha256 TEXT NOT NULL,
        source_tables INTEGER NOT NULL,source_rows INTEGER NOT NULL,
        entries_count INTEGER NOT NULL,entries_sha256 TEXT NOT NULL,
        links_count INTEGER NOT NULL,links_sha256 TEXT NOT NULL,
        prepared_by_R3_id TEXT NOT NULL,
        PRIMARY KEY(restore_id,authority),
        FOREIGN KEY(protocol_version) REFERENCES restore_quarantine_protocol(version)
            DEFERRABLE INITIALLY DEFERRED)''',
    PROTOCOL_TABLES[2]: '''CREATE TABLE restore_quarantine_entries(
        restore_id TEXT NOT NULL,authority TEXT NOT NULL,family TEXT NOT NULL,
        key_kind TEXT NOT NULL,key_hash TEXT NOT NULL,row_sha256 TEXT NOT NULL,
        state_sha256 TEXT NOT NULL,disposition TEXT NOT NULL
            CHECK(disposition IN ('no_replay','authority_invalid','evidence_only')),
        PRIMARY KEY(restore_id,authority,family,key_kind,key_hash),
        FOREIGN KEY(restore_id,authority) REFERENCES restore_quarantine_sets(restore_id,authority)
            DEFERRABLE INITIALLY DEFERRED)''',
    PROTOCOL_TABLES[3]: '''CREATE TABLE restore_quarantine_links(
        restore_id TEXT NOT NULL,child_authority TEXT NOT NULL,
        child_family TEXT NOT NULL,child_key_kind TEXT NOT NULL,child_key_hash TEXT NOT NULL,
        parent_authority TEXT NOT NULL,parent_family TEXT NOT NULL,
        parent_key_kind TEXT NOT NULL,parent_key_hash TEXT NOT NULL,relation TEXT NOT NULL,
        PRIMARY KEY(restore_id,child_authority,child_family,child_key_kind,child_key_hash,
                    parent_authority,parent_family,parent_key_kind,parent_key_hash,relation),
        FOREIGN KEY(restore_id,child_authority,child_family,child_key_kind,child_key_hash)
            REFERENCES restore_quarantine_entries(restore_id,authority,family,key_kind,key_hash)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(restore_id,child_authority) REFERENCES restore_quarantine_sets(restore_id,authority)
            DEFERRABLE INITIALLY DEFERRED)''',
}
_PRIMARY_COLUMNS = {
    PROTOCOL_TABLES[0]: ('version',),
    PROTOCOL_TABLES[1]: ('restore_id', 'authority'),
    PROTOCOL_TABLES[2]: ('restore_id', 'authority', 'family', 'key_kind', 'key_hash'),
    PROTOCOL_TABLES[3]: ('restore_id', 'child_authority', 'child_family', 'child_key_kind', 'child_key_hash',
                         'parent_authority', 'parent_family', 'parent_key_kind', 'parent_key_hash', 'relation'),
}


def _trigger_definitions():
    definitions = {}
    for table in PROTOCOL_TABLES:
        for verb in ('update', 'delete'):
            name = table + '_no_' + verb
            definitions[name] = (table, f'''CREATE TRIGGER {name} BEFORE {verb.upper()} ON {table}
                BEGIN SELECT RAISE(ABORT,'restore_quarantine_immutable'); END''')
        name = table + '_no_replace'
        same_key = ' AND '.join(f'{column}=NEW.{column}' for column in _PRIMARY_COLUMNS[table])
        condition = f'EXISTS(SELECT 1 FROM {table} WHERE {same_key})'
        if table == PROTOCOL_TABLES[0]:
            condition = f'EXISTS(SELECT 1 FROM {table})'
        if table in (PROTOCOL_TABLES[2], PROTOCOL_TABLES[3]):
            role = 'authority' if table == PROTOCOL_TABLES[2] else 'child_authority'
            condition += (' OR EXISTS(SELECT 1 FROM restore_quarantine_sets '
                          f'WHERE restore_id=NEW.restore_id AND authority=NEW.{role})')
        definitions[name] = (table, f'''CREATE TRIGGER {name} BEFORE INSERT ON {table}
            WHEN {condition} BEGIN SELECT RAISE(ABORT,'restore_quarantine_immutable'); END''')
    return definitions


_TRIGGER_DDL = _trigger_definitions()
_KNOWN_OBJECTS = frozenset((*PROTOCOL_TABLES, *_TRIGGER_DDL))


def _canonical(value):
    # This encoding deliberately matches the original R1 inventory watermark.
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _quoted(value):
    return '"' + value.replace('"', '""') + '"'


def _identity(plan, authority, identifier=None):
    if type(plan) is not QuarantinePlan or authority not in AUTHORITIES:
        raise RestoreQuarantineError(_INVALID)
    if identifier is not None:
        try:
            if type(identifier) is not str or str(UUID(identifier)) != identifier:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise RestoreQuarantineError(_INVALID) from None
    return next(source for source in plan.input.sources if source.authority == authority)


def _records(plan, authority):
    restore_id = plan.input.restore_id
    entries = tuple((restore_id, authority, item.origin.family, item.origin.key_kind,
                     item.origin.key_hash, item.row_sha256, item.state_sha256, item.disposition)
                    for item in plan.entries if item.origin.authority == authority)
    links = tuple((restore_id, authority, item.child.family, item.child.key_kind, item.child.key_hash,
                   item.parent.authority, item.parent.family, item.parent.key_kind,
                   item.parent.key_hash, item.relation)
                  for item in plan.links if item.child.authority == authority)
    if len(entries) + len(links) > MAX_PROTOCOL_RECORDS:
        raise RestoreQuarantineError(_INVALID)
    for stream in (entries, links):
        size = 0
        for row in stream:
            size += len(_canonical(row))
            if size > MAX_CONTENT_BYTES:
                raise RestoreQuarantineError(_INVALID)
    return tuple(sorted(entries)), tuple(sorted(links))


def validate_preparation_bounds(plan):
    """Pure size validation for the collector; this acquires no permission."""
    for authority in AUTHORITIES:
        _identity(plan, authority)
        _records(plan, authority)


def _receipt(plan, authority, identifier):
    source = _identity(plan, authority, identifier)
    entries, links = _records(plan, authority)
    return PreparedReceipt(authority, plan.input.restore_id, plan.input.input_sha256,
                           plan.plan_sha256, plan.input.catalogue_sha256, source.schema_sha256,
                           source.logical_sha256, source.file_sha256,
                           len(entries), digest(entries), len(links), digest(links), identifier)


def _seal(plan, authority, receipt):
    source = _identity(plan, authority)
    return (plan.input.restore_id, authority, PROTOCOL_VERSION, plan.input.catalogue_sha256,
            plan.input.input_sha256, plan.plan_sha256, plan.input.manifest_sha256,
            plan.input.metadata_sha256, plan.input.bindings_sha256, plan.input.preview_sha256,
            source.schema_sha256, source.logical_sha256, source.file_sha256,
            source.tables, source.rows, receipt.entries_count, receipt.entries_sha256,
            receipt.links_count, receipt.links_sha256, receipt.prepared_by_R3_id)


def preparation_program(plan, authority, *, prepared_by_R3_id, existing=None):
    """Produce inert SQL. Caller identity does not acquire an execution capability."""
    if prepared_by_R3_id is None:
        raise RestoreQuarantineError(_INVALID)
    expected = _receipt(plan, authority, prepared_by_R3_id)
    if existing is not None:
        if type(existing) is not PreparedReceipt or existing != expected:
            raise RestoreQuarantineError(_INVALID)
        return PreparationProgram(())
    entries, links = _records(plan, authority)
    statements = [SQLStatement(sql) for sql in _TABLE_DDL.values()]
    statements.extend(SQLStatement(sql) for _, sql in _TRIGGER_DDL.values())
    statements.append(SQLStatement('INSERT INTO restore_quarantine_protocol VALUES(?,?)',
                                   (PROTOCOL_VERSION, plan.input.catalogue_sha256)))
    statements.extend(SQLStatement('INSERT INTO restore_quarantine_entries VALUES(?,?,?,?,?,?,?,?)', row)
                      for row in entries)
    statements.extend(SQLStatement('INSERT INTO restore_quarantine_links VALUES(?,?,?,?,?,?,?,?,?,?)', row)
                      for row in links)
    # The immutable seal is last. Before it, deferred FKs cannot commit normally.
    seal = _seal(plan, authority, expected)
    statements.append(SQLStatement('INSERT INTO restore_quarantine_sets VALUES(' + ','.join('?' for _ in seal) + ')', seal))
    return PreparationProgram(tuple(statements))


def _within_deadline(deadline):
    if time.monotonic() >= deadline:
        raise RestoreQuarantineError(_INVALID)


def _execute(conn, sql, deadline):
    _within_deadline(deadline)
    cursor = conn.execute(sql)
    _within_deadline(deadline)
    return cursor


def _schema_objects(conn, deadline):
    objects = []
    size = 0
    for row in _execute(conn, 'SELECT type,name,tbl_name,sql FROM sqlite_master '
                        'WHERE sql IS NOT NULL ORDER BY type,name', deadline):
        _within_deadline(deadline)
        row = tuple(row)
        size += len(_canonical(row))
        if len(objects) >= 4096 or size > MAX_CONTENT_BYTES:
            raise RestoreQuarantineError(_INVALID)
        objects.append(row)
    return objects


def _protocol_schema_valid(objects):
    expected = {name: ('table', name, sql.strip()) for name, sql in _TABLE_DDL.items()}
    expected.update({name: ('trigger', table, sql.strip()) for name, (table, sql) in _TRIGGER_DDL.items()})
    actual = {name: (kind, table, sql.strip()) for kind, name, table, sql in objects
              if name in _KNOWN_OBJECTS or table in PROTOCOL_TABLES or name.startswith('restore_quarantine_')}
    return actual == expected


def _business_snapshot(conn, objects, deadline, *, excluded_tables=()):
    # Internal callers may project separately validated protocol metadata. The
    # public R2/R3 reader always supplies the empty default and stays strict.
    if (type(excluded_tables) is not tuple or any(type(name) is not str for name in excluded_tables)
            or len(set(excluded_tables)) != len(excluded_tables)):
        raise RestoreQuarantineError(_INVALID)
    ddl = [(kind, name, sql) for kind, name, _, sql in objects if name not in _KNOWN_OBJECTS]
    tables = sorted(row[0] for row in _execute(conn, "SELECT name FROM sqlite_master WHERE type='table'", deadline)
                    if row[0] not in (*PROTOCOL_TABLES, *excluded_tables))
    if len(tables) > 512:
        raise RestoreQuarantineError(_INVALID)
    schema_hash = hashlib.sha256(_canonical(ddl)).hexdigest()
    counts = {}
    for name in tables:
        if not name.startswith('sqlite_'):
            counts[name] = _execute(conn, 'SELECT count(*) FROM ' + _quoted(name), deadline).fetchone()[0]
            _within_deadline(deadline)
    if sum(counts.values()) > MAX_ROWS:
        raise RestoreQuarantineError(_INVALID)
    schema = {'user_version': _execute(conn, 'PRAGMA user_version', deadline).fetchone()[0],
              'schema_sha256': schema_hash, 'row_counts': counts}
    logical = hashlib.sha256(_canonical(schema))
    rows = size = 0
    for table in tables:
        hashes = []
        for row in _execute(conn, 'SELECT * FROM ' + _quoted(table), deadline):
            rows += 1
            if rows > MAX_ROWS or time.monotonic() > deadline:
                raise RestoreQuarantineError(_INVALID)
            values = [('blob', value.hex()) if isinstance(value, bytes) else (type(value).__name__, value)
                      for value in row]
            raw = _canonical(values)
            size += len(raw)
            if size > MAX_CONTENT_BYTES:
                raise RestoreQuarantineError(_INVALID)
            hashes.append(hashlib.sha256(raw).hexdigest())
        logical.update(_canonical([table, sorted(hashes)]))
    return schema_hash, logical.hexdigest(), len(tables), sum(counts.values())


def _read_rows(conn, table, maximum, deadline):
    rows = []
    size = 0
    for row in _execute(conn, 'SELECT * FROM ' + table, deadline):
        _within_deadline(deadline)
        if len(rows) >= maximum:
            raise RestoreQuarantineError(_INVALID)
        size += len(_canonical(tuple(row)))
        if size > MAX_CONTENT_BYTES:
            raise RestoreQuarantineError(_INVALID)
        rows.append(tuple(row))
    return tuple(sorted(rows))


def read_preparation(conn, *, plan, authority):
    """Validate supplied evidence; even a complete result keeps activation OFF."""
    return _read_preparation_projected(conn, plan=plan, authority=authority)


def _read_preparation_projected(conn, *, plan, authority, approved_schema=()):
    """Private projection after a closed protocol reader verified exact metadata.

    This is no writer/admission API. Its caller must validate metadata rows and
    its complete schema first. Matching the exact object records again prevents
    an accidental name-only exclusion. The public reader never uses a projection.
    """
    source = _identity(plan, authority)
    if not isinstance(conn, sqlite3.Connection):
        raise RestoreQuarantineError(_INVALID)
    old_limit = None
    try:
        old_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(old_limit, MAX_CELL_BYTES))
        deadline = time.monotonic() + MAX_SECONDS
        objects = _schema_objects(conn, deadline)
        if (type(approved_schema) is not tuple or any(type(item) is not tuple or len(item) != 4
                for item in approved_schema) or len(set(approved_schema)) != len(approved_schema)
                or any(item not in objects for item in approved_schema)):
            raise RestoreQuarantineError(_INVALID)
        projected = [item for item in objects if item not in approved_schema]
        excluded_tables = tuple(item[1] for item in approved_schema if item[0] == 'table')
        relevant = [item for item in objects if item[1] in _KNOWN_OBJECTS
                    or item[2] in PROTOCOL_TABLES or item[1].startswith('restore_quarantine_')]
        if relevant and not _protocol_schema_valid(objects):
            raise RestoreQuarantineError(_INVALID)
        guard = [tuple(row) for row in _execute(conn,
            'SELECT id,restore_id,reconciliation_required,manifest_sha256 FROM maintenance_restore_guard LIMIT 2', deadline)]
        if guard != [(1, plan.input.restore_id, 1, plan.input.manifest_sha256)]:
            raise RestoreQuarantineError(_INVALID)
        if _business_snapshot(conn, projected, deadline, excluded_tables=excluded_tables) != (source.schema_sha256, source.logical_sha256,
                                                  source.tables, source.rows):
            raise RestoreQuarantineError(_INVALID)
        if not relevant:
            return None
        protocol = _read_rows(conn, PROTOCOL_TABLES[0], 2, deadline)
        sets = _read_rows(conn, PROTOCOL_TABLES[1], 2, deadline)
        if protocol != ((PROTOCOL_VERSION, plan.input.catalogue_sha256),) or len(sets) != 1:
            raise RestoreQuarantineError(_INVALID)
        identifier = sets[0][-1]
        receipt = _receipt(plan, authority, identifier)
        expected_set = _seal(plan, authority, receipt)
        entries, links = _records(plan, authority)
        if (sets != (expected_set,)
                or _read_rows(conn, PROTOCOL_TABLES[2], len(entries) + 1, deadline) != entries
                or _read_rows(conn, PROTOCOL_TABLES[3], len(links) + 1, deadline) != links):
            raise RestoreQuarantineError(_INVALID)
        return receipt
    except RestoreQuarantineError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        raise RestoreQuarantineError(_INVALID) from None
    finally:
        if old_limit is not None:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_limit)
