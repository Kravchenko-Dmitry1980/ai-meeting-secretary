"""Pure, immutable R4 source preparation for three guarded SQL authorities.

The caller owns a controlled connection and all custody/consent/transactions.
Nothing here opens a path, executes the returned program, commits, authorizes
activation, normalizes a journal, or changes a guard/business/R2/R3 row. Typed
bindings and receipts are diagnostic commitments, never source custody or grants.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
import sqlite3
import time
from uuid import UUID

from secretary.domain.restore_quarantine import (
    AUTHORITIES, QuarantinePlan, RestoreQuarantineError, digest, encoded, typed,
)
from .restore_quarantine_preparation import (
    MAX_CELL_BYTES, MAX_CONTENT_BYTES, MAX_ROWS, MAX_SECONDS, PROTOCOL_TABLES,
    SQLStatement, _read_preparation_projected,
)

_INVALID = 'restore_source_epoch_invalid'
_PREFIX = 'restore_source_epoch_'
_TABLES = (_PREFIX + 'binding', _PREFIX + 'seal')
_SHA = re.compile('[0-9a-f]{64}')
_MAX_OBJECTS = 4096


class SourceEpochError(ValueError):
    code = _INVALID

    def __init__(self):
        super().__init__(self.code)


def _fail():
    raise SourceEpochError() from None


def _uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


@dataclass(frozen=True)
class NewSourceEpochBinding:
    restore_id: str
    r3_id: str
    ledger_id: str
    generation_id: str
    epoch_id: str
    preparation_id: str
    preparation_sha256: str
    role: str
    source_commitment_sha256: str
    r3_preparation_sha256: str
    baseline_file_sha256: str
    source_path_sha256: str
    file_id: tuple[int, int]
    baseline_rowids_sha256: str

    def __post_init__(self):
        if (any(not _uuid(value) for value in (self.restore_id, self.r3_id, self.ledger_id,
                self.generation_id, self.epoch_id, self.preparation_id))
                or any(not _sha(value) for value in (self.preparation_sha256,
                    self.source_commitment_sha256, self.r3_preparation_sha256,
                    self.baseline_file_sha256, self.source_path_sha256, self.baseline_rowids_sha256))
                or type(self.role) is not str or self.role not in AUTHORITIES
                or type(self.file_id) is not tuple or len(self.file_id) != 2
                or any(type(value) is not int or value < 0 or value > 2 ** 128 - 1
                    for value in self.file_id)):
            _fail()


@dataclass(frozen=True)
class SourceEpochProgram:
    statements: tuple[SQLStatement, ...]
    protocol_schema: tuple[tuple[str, str, str, str], ...]
    state: str = 'source_epoch_prepared_only'
    activation_supported: bool = False
    outbound_enabled: bool = False


@dataclass(frozen=True)
class SourceBusinessProjection:
    schema_sha256: str
    logical_sha256: str
    tables: int
    rows: int
    rowids_sha256: str
    r3_preparation_sha256: str
    activation_supported: bool = False
    outbound_enabled: bool = False


@dataclass(frozen=True)
class SourceEpochReceipt:
    binding: NewSourceEpochBinding
    binding_sha256: str
    source_schema_sha256: str
    source_logical_sha256: str
    source_tables: int
    source_rows: int
    rowids_sha256: str
    r3_preparation_sha256: str
    state: str = 'source_epoch_prepared_blocked'
    activation_supported: bool = False
    outbound_enabled: bool = False

    @property
    def receipt_sha256(self):
        return digest(asdict(self))


_TABLE_DDL = {
    _TABLES[0]: '''CREATE TABLE restore_source_epoch_binding(
        id INTEGER PRIMARY KEY CHECK(id=1),
        protocol INTEGER NOT NULL CHECK(protocol=1),
        binding_json TEXT NOT NULL CHECK(json_valid(binding_json) AND length(binding_json)<=4096),
        binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64))''',
    _TABLES[1]: '''CREATE TABLE restore_source_epoch_seal(
        id INTEGER PRIMARY KEY CHECK(id=1),
        binding_id INTEGER NOT NULL CHECK(binding_id=1),
        binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64),
        seal_sha256 TEXT NOT NULL CHECK(length(seal_sha256)=64),
        FOREIGN KEY(binding_id) REFERENCES restore_source_epoch_binding(id)
            DEFERRABLE INITIALLY DEFERRED)''',
}
_TRIGGER_DDL = {}
for _table in _TABLES:
    for _verb in ('update', 'delete', 'insert'):
        _name = _table + '_no_' + _verb
        _when = f' WHEN EXISTS(SELECT 1 FROM {_table})' if _verb == 'insert' else ''
        _sql = f'''CREATE TRIGGER {_name} BEFORE {_verb.upper()} ON {_table}{_when}
            BEGIN SELECT RAISE(ABORT,'restore_source_epoch_immutable'); END'''
        _TRIGGER_DDL[_name] = (_table, _sql)
_SCHEMA = tuple(sorted(
    [(kind, name, table, sql.strip()) for kind, name, table, sql in
        [('table', name, name, sql) for name, sql in _TABLE_DDL.items()] +
        [('trigger', name, table, sql) for name, (table, sql) in _TRIGGER_DDL.items()]]
))


def _binding(binding):
    if type(binding) is not NewSourceEpochBinding:
        _fail()
    binding.__post_init__()
    return digest(asdict(binding))


def _seal(binding):
    return digest(['restore-source-epoch-seal-v1', asdict(binding)])


def source_epoch_program(binding):
    """Build inert exact metadata SQL; never an execution/admission permission."""
    binding_sha = _binding(binding)
    statements = [SQLStatement(sql) for sql in _TABLE_DDL.values()]
    statements.extend(SQLStatement(sql) for _, sql in _TRIGGER_DDL.values())
    statements.append(SQLStatement('INSERT INTO restore_source_epoch_binding VALUES(?,?,?,?)',
        (1, 1, encoded(asdict(binding)), binding_sha)))
    statements.append(SQLStatement('INSERT INTO restore_source_epoch_seal VALUES(?,?,?,?)',
        (1, 1, binding_sha, _seal(binding))))
    return SourceEpochProgram(tuple(statements), _SCHEMA)


def _execute(conn, sql, deadline, parameters=()):
    if time.monotonic() >= deadline:
        _fail()
    cursor = conn.execute(sql, parameters)
    if time.monotonic() >= deadline:
        _fail()
    return cursor


def _connection(conn, deadline):
    # SQLite resolves unqualified data names through TEMP before main. A
    # caller-owned connection must not substitute otherwise genuine main rows.
    # ATTACH also lies outside this three-source reader's closed main authority.
    databases = _execute(conn, 'PRAGMA database_list', deadline).fetchall()
    if (not databases or len(databases) > 2
            or databases[0][0:2] != (0, 'main')
            or any(len(row) != 3 or type(row[0]) is not int
                or type(row[1]) is not str or type(row[2]) is not str
                for row in databases)
            or len(databases) == 2 and databases[1][0:2] != (1, 'temp')):
        _fail()
    if _execute(conn, 'SELECT 1 FROM temp.sqlite_master LIMIT 1', deadline).fetchone() is not None:
        _fail()


def _metadata(conn, binding, deadline):
    rows = _execute(conn, 'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name LIMIT ?',
        deadline, (_MAX_OBJECTS + 1,)).fetchall()
    if len(rows) > _MAX_OBJECTS:
        _fail()
    own = []
    for kind, name, target, sql in rows:
        if (type(kind) is not str or type(name) is not str or type(target) is not str
                or sql is not None and type(sql) is not str):
            _fail()
        if name.startswith(_PREFIX) or target in _TABLES:
            own.append((kind, name, target, sql.strip() if sql is not None else None))
    if not own:
        return False
    if tuple(sorted(own)) != _SCHEMA:
        _fail()
    binding_sha = _binding(binding)
    expected = (1, 1, encoded(asdict(binding)), binding_sha)
    actual = _execute(conn, 'SELECT * FROM restore_source_epoch_binding LIMIT 2', deadline).fetchall()
    seal = _execute(conn, 'SELECT * FROM restore_source_epoch_seal LIMIT 2', deadline).fetchall()
    if actual != [expected] or seal != [(1, 1, binding_sha, _seal(binding))]:
        _fail()
    return True


def _rowids_projection(conn, deadline, *, prepared):
    excluded = (*PROTOCOL_TABLES, *(_TABLES if prepared else ()), 'sqlite_schema')
    tables = sorted((row[1], row[4]) for row in _execute(conn, 'PRAGMA table_list', deadline)
        if row[0] == 'main' and row[2] == 'table' and row[1] not in excluded)
    if len(tables) > 512:
        _fail()
    result = hashlib.sha256()
    rows = size = 0
    for table, without_rowid in tables:
        if without_rowid:
            continue
        result.update(encoded(table).encode('utf-8'))
        quoted = '"' + table.replace('"', '""') + '"'
        for row in _execute(conn, 'SELECT _rowid_,* FROM ' + quoted + ' ORDER BY _rowid_', deadline):
            if time.monotonic() >= deadline:
                _fail()
            raw = encoded(typed(row)).encode('utf-8')
            rows += 1
            size += len(raw)
            if rows > MAX_ROWS or size > MAX_CONTENT_BYTES:
                _fail()
            result.update(len(raw).to_bytes(8, 'big'))
            result.update(raw)
    return result.hexdigest()


def _read(conn, *, binding, quarantine_plan):
    _binding(binding)
    if not isinstance(conn, sqlite3.Connection) or type(quarantine_plan) is not QuarantinePlan:
        _fail()
    if quarantine_plan.input.restore_id != binding.restore_id:
        _fail()
    old_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(old_limit, MAX_CELL_BYTES))
        deadline = time.monotonic() + MAX_SECONDS
        _connection(conn, deadline)
        prepared = _metadata(conn, binding, deadline)
        receipt = _read_preparation_projected(conn, plan=quarantine_plan, authority=binding.role,
            approved_schema=_SCHEMA if prepared else ())
        if (receipt is None or receipt.prepared_by_R3_id != binding.r3_id
                or digest(asdict(receipt)) != binding.r3_preparation_sha256):
            _fail()
        rowids = _rowids_projection(conn, deadline, prepared=prepared)
        if rowids != binding.baseline_rowids_sha256:
            _fail()
        source = next(item for item in quarantine_plan.input.sources if item.authority == binding.role)
        projection = SourceBusinessProjection(source.schema_sha256, source.logical_sha256,
            source.tables, source.rows, rowids, digest(asdict(receipt)))
        return prepared, projection
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_limit)


def prepared_source_projection(conn, *, binding, quarantine_plan):
    """Actual validated baseline projection, including exact prepared metadata."""
    try:
        return _read(conn, binding=binding, quarantine_plan=quarantine_plan)[1]
    except SourceEpochError:
        raise
    except (RestoreQuarantineError, sqlite3.Error, ValueError, TypeError, KeyError,
            AttributeError, OverflowError, RecursionError, UnicodeError):
        _fail()


def read_source_epoch(conn, *, binding, quarantine_plan):
    """Read exact inert preparation; an unchanged actual R3 baseline returns None."""
    try:
        prepared, projection = _read(conn, binding=binding, quarantine_plan=quarantine_plan)
        if not prepared:
            return None
        return SourceEpochReceipt(binding, _binding(binding), projection.schema_sha256,
            projection.logical_sha256, projection.tables, projection.rows,
            projection.rowids_sha256, projection.r3_preparation_sha256)
    except SourceEpochError:
        raise
    except (RestoreQuarantineError, sqlite3.Error, ValueError, TypeError, KeyError,
            AttributeError, OverflowError, RecursionError, UnicodeError):
        _fail()
