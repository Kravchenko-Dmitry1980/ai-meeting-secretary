"""Declarative R2 preparation seals evidence, without acquiring a writer."""
from contextlib import closing
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
import sqlite3
from uuid import uuid4

import pytest

from secretary.domain.restore_quarantine import (
    Origin, QuarantineEntry, QuarantineInput, QuarantineLink, QuarantinePlan,
    RestoreQuarantineError, SourceWatermark, key_digest,
)


def module():
    name = 'secretary.infrastructure.restore_quarantine_preparation'
    assert importlib.util.find_spec(name), 'declarative quarantine preparation missing'
    return importlib.import_module(name)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def watermark(conn, role):
    tables = sorted(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    ddl = conn.execute('SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name').fetchall()
    schema_hash = hashlib.sha256(canonical(ddl)).hexdigest()
    counts = {name: conn.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
              for name in tables if not name.startswith('sqlite_')}
    schema = {'user_version': 0, 'schema_sha256': schema_hash, 'row_counts': counts}
    logical = hashlib.sha256(canonical(schema))
    for table in tables:
        hashes = [hashlib.sha256(canonical([(type(v).__name__, v) for v in row])).hexdigest()
                  for row in conn.execute('SELECT * FROM "' + table + '"')]
        logical.update(canonical([table, sorted(hashes)]))
    return SourceWatermark(role, '0' * 64, logical.hexdigest(), schema_hash, len(tables), sum(counts.values()))


@pytest.fixture
def sample():
    with closing(sqlite3.connect(':memory:')) as conn:
        restore_id = str(uuid4())
        conn.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT)')
        conn.execute('INSERT INTO jobs VALUES(?,?)', ('legacy-sensitive-id', 'uncertain'))
        conn.execute('CREATE TABLE maintenance_restore_guard(id INTEGER PRIMARY KEY,restore_id TEXT,reconciliation_required INTEGER,manifest_sha256 TEXT)')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)', (restore_id, '1' * 64))
        conn.commit()
        job = Origin('secretary', 'jobs', key_digest('secretary', 'jobs', ('legacy-sensitive-id',)))
        guard = Origin('secretary', 'maintenance_restore_guard', key_digest('secretary', 'maintenance_restore_guard', (1,)))
        entries = tuple(sorted((QuarantineEntry(job, 'a' * 64, 'b' * 64, 'no_replay'),
                                QuarantineEntry(guard, 'c' * 64, 'd' * 64, 'evidence_only')), key=lambda item: item.origin))
        sources = (watermark(conn, 'secretary'), *(SourceWatermark(role, '0' * 64, '0' * 64, '0' * 64, 0, 0)
                                                for role in ('team', 'billing', 'vikunja')))
        inputs = QuarantineInput(restore_id, '1' * 64, '2' * 64, '3' * 64,
                                 '4' * 64, '5' * 64, sources)
        plan = QuarantinePlan(inputs, entries, (QuarantineLink(job, guard, 'authorized_by'),),
                              (('secretary', 'jobs', 1), ('secretary', 'maintenance_restore_guard', 1)))
        yield conn, plan


def apply_test_only(conn, program):
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    try:
        for statement in program.statements:
            conn.execute(statement.sql, statement.params)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def prepared(sample):
    conn, plan = sample
    store = module()
    identifier = str(uuid4())
    program = store.preparation_program(plan, 'secretary', prepared_by_R3_id=identifier)
    apply_test_only(conn, program)
    return conn, plan, store, store.read_preparation(conn, plan=plan, authority='secretary')


def test_in_memory_preparation_is_sanitized_declarative_and_nonpermitting(sample):
    conn, plan, store, receipt = prepared(sample)
    assert receipt.activation_supported is False
    assert receipt.authority == 'secretary' and receipt.input_sha256 == plan.input.input_sha256
    assert not hasattr(store, 'execute_preparation')
    for table in store.PROTOCOL_TABLES:
        assert 'legacy-sensitive-id' not in repr(conn.execute('SELECT * FROM ' + table).fetchall())
    program = store.preparation_program(plan, 'secretary', prepared_by_R3_id=receipt.prepared_by_R3_id, existing=receipt)
    assert program.statements == () and program.activation_supported is False


@pytest.mark.parametrize('authority,identifier', [('vikunja', None), ('secretary', True), ('secretary', 'bad-uuid')])
def test_invalid_preparation_identity_cannot_produce_a_batch(sample, authority, identifier):
    _, plan = sample
    with pytest.raises(RestoreQuarantineError):
        module().preparation_program(plan, authority, prepared_by_R3_id=identifier)


@pytest.mark.parametrize('change', ['r3_id', 'input', 'authority', 'untyped'])
def test_replay_requires_an_exact_validated_receipt(sample, change):
    _, plan, store, receipt = prepared(sample)
    identifier = receipt.prepared_by_R3_id
    if change == 'r3_id': identifier = str(uuid4())
    elif change == 'input': receipt = replace(receipt, input_sha256='f' * 64)
    elif change == 'authority': receipt = replace(receipt, authority='team')
    else: receipt = True
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.preparation_program(plan, 'secretary', prepared_by_R3_id=identifier, existing=receipt)


@pytest.mark.parametrize('change', ['business_row', 'business_schema', 'guard', 'extra_table', 'extra_trigger', 'extra_index'])
def test_prepared_read_refuses_changed_business_or_protocol_objects(sample, change):
    conn, plan, store, _ = prepared(sample)
    if change == 'business_row': conn.execute("UPDATE jobs SET status='confirmed'")
    elif change == 'business_schema': conn.execute('ALTER TABLE jobs ADD COLUMN external_intent TEXT')
    elif change == 'guard': conn.execute('UPDATE maintenance_restore_guard SET restore_id=?', (str(uuid4()),))
    elif change == 'extra_table': conn.execute('CREATE TABLE restore_quarantine_hidden(id TEXT)')
    elif change == 'extra_trigger': conn.execute('CREATE TRIGGER restore_quarantine_hidden AFTER INSERT ON jobs BEGIN SELECT 1; END')
    else: conn.execute('CREATE INDEX restore_quarantine_hidden ON restore_quarantine_entries(family)')
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')


def test_no_seal_and_partial_schema_never_return_a_receipt(sample):
    conn, plan = sample
    store = module()
    assert store.read_preparation(conn, plan=plan, authority='secretary') is None
    program = store.preparation_program(plan, 'secretary', prepared_by_R3_id=str(uuid4()))
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY constraint failed'):
        apply_test_only(conn, replace(program, statements=program.statements[:-1]))
    assert store.read_preparation(conn, plan=plan, authority='secretary') is None
    # Deliberately corrupt only this synthetic store, bypassing its FK check.
    # A restored file with that committed partial state still cannot be read as sealed.
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute('BEGIN IMMEDIATE')
    for statement in program.statements[:-1]:
        conn.execute(statement.sql, statement.params)
    conn.commit()
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')


def test_sealed_entries_cannot_append_or_replace_even_without_recursive_triggers(sample):
    conn, _, store, _ = prepared(sample)
    conn.execute('PRAGMA recursive_triggers=OFF')
    for table in store.PROTOCOL_TABLES:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT OR REPLACE INTO ' + table + ' SELECT * FROM ' + table)
        with pytest.raises(sqlite3.IntegrityError): conn.execute('DELETE FROM ' + table)
        with pytest.raises(sqlite3.IntegrityError): conn.execute('UPDATE ' + table + ' SET rowid=rowid')
    row = list(conn.execute('SELECT * FROM restore_quarantine_entries LIMIT 1').fetchone())
    row[4] = 'e' * 64
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('INSERT INTO restore_quarantine_entries VALUES(' + ','.join('?' for _ in row) + ')', row)


def test_changed_ledger_content_or_missing_trigger_invalidates_receipt(sample):
    conn, plan, store, _ = prepared(sample)
    trigger = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='restore_quarantine_entries' AND name LIKE '%no_update'").fetchone()[0]
    conn.execute('DROP TRIGGER ' + trigger)
    conn.execute("UPDATE restore_quarantine_entries SET row_sha256=?", ('f' * 64,))
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')


@pytest.mark.parametrize('table,field,value', [
    ('restore_quarantine_protocol', 'catalogue_sha256', 'f' * 64),
    ('restore_quarantine_sets', 'entries_count', 999),
    ('restore_quarantine_entries', 'state_sha256', 'f' * 64),
    ('restore_quarantine_links', 'relation', 'source_reference'),
])
def test_exact_restored_triggers_do_not_hide_corrupt_ledger_contents(sample, table, field, value):
    conn, plan, store, _ = prepared(sample)
    name, ddl = conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND name LIKE '%no_update'", (table,)).fetchone()
    conn.execute('DROP TRIGGER ' + name)
    conn.execute('UPDATE ' + table + ' SET ' + field + '=?', (value,))
    conn.execute(ddl)
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')


@pytest.mark.parametrize('boundary', range(22))
def test_rollback_at_each_batch_boundary_never_leaves_prepared_evidence(sample, boundary):
    conn, plan = sample
    store = module()
    program = store.preparation_program(plan, 'secretary', prepared_by_R3_id=str(uuid4()))
    assert len(program.statements) == 21  # This fixture has two entries and one link.
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    for statement in program.statements[:boundary]:
        conn.execute(statement.sql, statement.params)
    conn.rollback()
    assert store.read_preparation(conn, plan=plan, authority='secretary') is None
    assert conn.execute('SELECT status FROM jobs').fetchone() == ('uncertain',)


@pytest.mark.parametrize('change', ['preview', 'lineage', 'authority'])
def test_receipt_cannot_be_rebound_to_a_different_plan_or_authority(sample, change):
    conn, plan, store, _ = prepared(sample)
    authority = 'secretary'
    if change == 'preview': plan = replace(plan, input=replace(plan.input, preview_sha256='e' * 64))
    elif change == 'lineage': plan = replace(plan, links=())
    else: authority = 'team'
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority=authority)


def test_partial_protocol_schema_is_never_unprepared(sample):
    conn, plan = sample
    store = module()
    program = store.preparation_program(plan, 'secretary', prepared_by_R3_id=str(uuid4()))
    conn.execute(program.statements[0].sql)
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')


def test_read_limits_are_restored_after_success_and_refusal(sample):
    conn, plan, store, _ = prepared(sample)
    original = conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    assert store.read_preparation(conn, plan=plan, authority='secretary')
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original
    conn.execute("UPDATE jobs SET status='changed'")
    with pytest.raises(RestoreQuarantineError):
        store.read_preparation(conn, plan=plan, authority='secretary')
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == original


def test_read_does_not_raise_a_caller_owned_sqlite_length_limit(sample):
    conn, plan, store, _ = prepared(sample)
    conn.execute('UPDATE jobs SET status=?', ('x' * 8192,))
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
    def bounded_row(cursor, row):
        assert all(not isinstance(value, str) or len(value) <= 4096 for value in row), 'caller limit was raised'
        return row
    conn.row_factory = bounded_row
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')
    assert conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == 4096


def test_closed_connection_failure_is_a_sanitized_refusal(sample):
    _, plan = sample
    conn = sqlite3.connect(':memory:')
    conn.close()
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        module().read_preparation(conn, plan=plan, authority='secretary')


def test_existing_caller_progress_handler_is_preserved(sample):
    conn, plan, store, _ = prepared(sample)
    calls = []
    def interrupt():
        calls.append(True)
        return 1
    conn.set_progress_handler(interrupt, 1)
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')
    assert calls
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        conn.execute('SELECT 1')
    conn.set_progress_handler(None, 0)


@pytest.mark.parametrize('limit', ['MAX_ROWS', 'MAX_CONTENT_BYTES', 'MAX_SECONDS'])
def test_business_read_refuses_an_incomplete_bounded_scan(sample, monkeypatch, limit):
    conn, plan, store, _ = prepared(sample)
    monkeypatch.setattr(store, limit, 0)
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.read_preparation(conn, plan=plan, authority='secretary')


@pytest.mark.parametrize('limit', ['MAX_PROTOCOL_RECORDS', 'MAX_CONTENT_BYTES'])
def test_program_refuses_overlarge_evidence_before_producing_sql(sample, monkeypatch, limit):
    _, plan = sample
    store = module()
    monkeypatch.setattr(store, limit, 0)
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
        store.preparation_program(plan, 'secretary', prepared_by_R3_id=str(uuid4()))
