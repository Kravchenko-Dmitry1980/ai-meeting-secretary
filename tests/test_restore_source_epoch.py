"""Pure R4 source evidence on synthetic in-memory SQL; never activation."""
from contextlib import closing
from dataclasses import asdict, replace
import importlib
import importlib.util
import sqlite3
from uuid import uuid4

import pytest

from secretary.domain.restore_quarantine import (
    Origin, QuarantineEntry, QuarantineInput, QuarantinePlan, SourceWatermark,
    digest, key_digest,
)
from secretary.infrastructure.restore_quarantine_preparation import (
    preparation_program, read_preparation,
)
from secretary.infrastructure.team_restore_reconciliation import _rowids
from test_restore_quarantine_preparation_validation import watermark, apply_test_only


def module():
    name = 'secretary.infrastructure.restore_source_epoch'
    assert importlib.util.find_spec(name), 'R4 pure source-epoch protocol is missing'
    return importlib.import_module(name)


def make_source_sample(conn, *, role='secretary'):
        restore_id, r3_id = str(uuid4()), str(uuid4())
        conn.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT)')
        conn.execute('INSERT INTO jobs VALUES(?,?)', ('synthetic-sensitive-job', 'uncertain'))
        conn.execute('CREATE TABLE maintenance_restore_guard(id INTEGER PRIMARY KEY,restore_id TEXT,reconciliation_required INTEGER,manifest_sha256 TEXT)')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)', (restore_id, '1' * 64))
        conn.commit()
        origin = Origin(role, 'jobs', key_digest(role, 'jobs', ('synthetic-sensitive-job',)))
        entry = QuarantineEntry(origin, 'a' * 64, 'b' * 64, 'no_replay')
        inputs = QuarantineInput(restore_id, '1' * 64, '2' * 64, '3' * 64, '4' * 64, '5' * 64,
            (watermark(conn, role), *(SourceWatermark(item, '0' * 64, '0' * 64, '0' * 64, 0, 0)
                for item in ('secretary', 'team', 'billing', 'vikunja') if item != role)))
        plan = QuarantinePlan(inputs, (entry,), (), ((role, 'jobs', 1),))
        apply_test_only(conn, preparation_program(plan, role, prepared_by_R3_id=r3_id))
        r3 = read_preparation(conn, plan=plan, authority=role)
        values = dict(restore_id=restore_id, r3_id=r3_id, ledger_id=str(uuid4()),
            generation_id=str(uuid4()), epoch_id=str(uuid4()), preparation_id=str(uuid4()),
            preparation_sha256='6' * 64, role=role, source_commitment_sha256='7' * 64,
            r3_preparation_sha256=digest(asdict(r3)), baseline_file_sha256='8' * 64,
            source_path_sha256='9' * 64, file_id=(12, 34), baseline_rowids_sha256=_rowids(conn))
        return conn, plan, r3, values


@pytest.fixture
def source_sample():
    with closing(sqlite3.connect(':memory:')) as conn:
        yield make_source_sample(conn)


def bind(values):
    return module().NewSourceEpochBinding(**values)


def apply_epoch(conn, binding):
    apply_test_only(conn, module().source_epoch_program(binding))


def test_actual_r3_baseline_returns_none_and_pure_program_has_no_effect(source_sample):
    conn, plan, r3, values = source_sample
    target, binding = module(), bind(values)
    before = conn.serialize()
    assert target.read_source_epoch(conn, binding=binding, quarantine_plan=plan) is None
    program = target.source_epoch_program(binding)
    assert program.statements and program.state == 'source_epoch_prepared_only'
    assert program.activation_supported is False and program.outbound_enabled is False
    assert conn.serialize() == before and not conn.in_transaction
    assert 'synthetic-sensitive-job' not in repr(program)
    assert not hasattr(target, 'apply_source_epoch')


def test_exact_durable_epoch_preserves_business_guard_r2_and_default_reader_strictness(source_sample):
    conn, plan, r3, values = source_sample
    target, binding = module(), bind(values)
    before_business = conn.execute('SELECT rowid,* FROM jobs').fetchall()
    before_r2 = {name: conn.execute('SELECT * FROM ' + name).fetchall() for name in
        ('restore_quarantine_protocol', 'restore_quarantine_sets', 'restore_quarantine_entries', 'restore_quarantine_links')}
    apply_epoch(conn, binding)
    before = conn.serialize()
    receipt = target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)
    assert receipt.binding == binding and receipt.binding_sha256 == digest(asdict(binding))
    assert receipt.state == 'source_epoch_prepared_blocked'
    assert receipt.activation_supported is False and receipt.outbound_enabled is False
    assert receipt.receipt_sha256 == digest(asdict(receipt))
    projection = target.prepared_source_projection(conn, binding=binding, quarantine_plan=plan)
    source = next(item for item in plan.input.sources if item.authority == binding.role)
    assert (projection.schema_sha256, projection.logical_sha256, projection.tables, projection.rows) == (
        source.schema_sha256, source.logical_sha256, source.tables, source.rows)
    assert projection.rowids_sha256 == binding.baseline_rowids_sha256
    assert projection.r3_preparation_sha256 == digest(asdict(r3))
    assert conn.execute('SELECT rowid,* FROM jobs').fetchall() == before_business
    assert conn.execute('SELECT reconciliation_required FROM maintenance_restore_guard').fetchall() == [(1,)]
    assert before_r2 == {name: conn.execute('SELECT * FROM ' + name).fetchall() for name in before_r2}
    assert conn.serialize() == before
    with pytest.raises(ValueError, match='restore_quarantine_preparation_invalid'):
        read_preparation(conn, plan=plan, authority='secretary')


@pytest.mark.parametrize('field,value', [
    ('restore_id', True), ('r3_id', 'not-uuid'), ('ledger_id', str(uuid4()).upper()),
    ('generation_id', None), ('epoch_id', 4), ('preparation_id', ''),
    ('preparation_sha256', 'f' * 63), ('source_commitment_sha256', 'F' * 64),
    ('r3_preparation_sha256', b'0' * 64), ('baseline_file_sha256', None),
    ('source_path_sha256', 'g' * 64), ('baseline_rowids_sha256', ' ' * 64),
    ('role', 'vikunja'), ('role', True), ('file_id', [12, 34]),
    ('file_id', (True, 34)), ('file_id', (12, -1)), ('file_id', (12, 34, 56)),
])
def test_binding_refuses_malformed_or_untyped_fields(source_sample, field, value):
    _, _, _, values = source_sample
    target = module()
    with pytest.raises(target.SourceEpochError, match='restore_source_epoch_invalid'):
        target.NewSourceEpochBinding(**dict(values, **{field: value}))


@pytest.mark.parametrize('field', ['restore_id', 'r3_id', 'ledger_id', 'generation_id', 'epoch_id',
    'preparation_id', 'preparation_sha256', 'source_commitment_sha256',
    'r3_preparation_sha256', 'baseline_file_sha256', 'source_path_sha256', 'baseline_rowids_sha256'])
def test_existing_epoch_cannot_be_rebound(source_sample, field):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    apply_epoch(conn, binding)
    other = str(uuid4()) if field.endswith('_id') else 'e' * 64
    changed = replace(binding, **{field: other})
    with pytest.raises(target.SourceEpochError):
        target.read_source_epoch(conn, binding=changed, quarantine_plan=plan)


@pytest.mark.parametrize('change', ['business', 'business_schema', 'rowid', 'guard', 'r2_trigger',
    'r2_receipt', 'epoch_trigger', 'unknown_prefix', 'unknown_trigger', 'unknown_index'])
def test_changed_business_history_or_schema_is_not_hidden_by_projection(source_sample, change):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    apply_epoch(conn, binding)
    if change == 'business': conn.execute("UPDATE jobs SET status='confirmed'")
    elif change == 'business_schema': conn.execute('ALTER TABLE jobs ADD COLUMN extra TEXT')
    elif change == 'rowid': conn.execute('UPDATE jobs SET rowid=rowid+10')
    elif change == 'guard': conn.execute('UPDATE maintenance_restore_guard SET reconciliation_required=0')
    elif change == 'r2_trigger': conn.execute('DROP TRIGGER restore_quarantine_sets_no_update')
    elif change == 'r2_receipt':
        conn.execute('DROP TRIGGER restore_quarantine_sets_no_update')
        conn.execute('UPDATE restore_quarantine_sets SET prepared_by_R3_id=?', (str(uuid4()),))
    elif change == 'epoch_trigger': conn.execute('DROP TRIGGER restore_source_epoch_binding_no_update')
    elif change == 'unknown_prefix': conn.execute('CREATE TABLE restore_source_epoch_unknown(id INTEGER)')
    elif change == 'unknown_trigger': conn.execute('CREATE TRIGGER restore_source_epoch_hidden AFTER UPDATE ON jobs BEGIN SELECT 1; END')
    else: conn.execute('CREATE INDEX restore_source_epoch_hidden ON restore_source_epoch_binding(binding_sha256)')
    with pytest.raises(target.SourceEpochError):
        target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)
    with pytest.raises(target.SourceEpochError):
        target.prepared_source_projection(conn, binding=binding, quarantine_plan=plan)


@pytest.mark.parametrize('table', ['restore_source_epoch_binding', 'restore_source_epoch_seal'])
@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE', 'INSERT', 'REPLACE'])
def test_metadata_is_immutable_without_recursive_triggers(source_sample, table, operation):
    conn, _, _, values = source_sample
    binding = bind(values)
    apply_epoch(conn, binding)
    conn.execute('PRAGMA recursive_triggers=OFF')
    row = conn.execute('SELECT * FROM ' + table).fetchone()
    if operation == 'UPDATE': statement, params = 'UPDATE ' + table + ' SET id=id', ()
    elif operation == 'DELETE': statement, params = 'DELETE FROM ' + table, ()
    else:
        statement = ('INSERT' if operation == 'INSERT' else 'INSERT OR REPLACE') + ' INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')'
        params = row
    with pytest.raises(sqlite3.IntegrityError, match='restore_source_epoch_immutable'):
        conn.execute(statement, params)
    conn.rollback()
    assert conn.execute('SELECT * FROM ' + table).fetchall() == [row]


@pytest.mark.parametrize('boundary', range(11))
def test_rollback_at_each_program_boundary_leaves_original_r3(source_sample, boundary):
    conn, plan, r3, values = source_sample
    target, binding = module(), bind(values)
    program = target.source_epoch_program(binding)
    assert len(program.statements) == 10
    conn.execute('BEGIN IMMEDIATE')
    for statement in program.statements[:boundary]: conn.execute(statement.sql, statement.params)
    conn.rollback()
    assert target.read_source_epoch(conn, binding=binding, quarantine_plan=plan) is None
    assert read_preparation(conn, plan=plan, authority='secretary') == r3


def test_partial_schema_or_missing_seal_is_never_an_unprepared_baseline(source_sample):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    program = target.source_epoch_program(binding)
    apply_test_only(conn, replace(program, statements=program.statements[:-1]))
    with pytest.raises(target.SourceEpochError):
        target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)


def test_reader_preserves_caller_length_limit_and_progress_handler(source_sample):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    apply_epoch(conn, binding)
    prior = conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 8192)
    calls = []
    conn.set_progress_handler(lambda: calls.append(1) or 0, 1)
    receipt = target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)
    assert receipt and calls and conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == 8192
    conn.set_progress_handler(lambda: 1, 1)
    with pytest.raises(sqlite3.OperationalError, match='interrupted'): conn.execute('SELECT 1')
    conn.set_progress_handler(None, 0)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, prior)


@pytest.mark.parametrize('changed', ['role', 'r3_id', 'r3_preparation_sha256', 'restore_id', 'baseline_rowids_sha256'])
def test_unprepared_baseline_requires_matching_actual_r3_and_rowids(source_sample, changed):
    conn, plan, _, values = source_sample
    target = module()
    values = dict(values, **{changed: 'team' if changed == 'role' else str(uuid4()) if changed.endswith('_id') else 'e' * 64})
    with pytest.raises(target.SourceEpochError):
        target.read_source_epoch(conn, binding=bind(values), quarantine_plan=plan)


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing'])
def test_each_guarded_authority_receipt_binds_the_actual_source_role(role):
    with closing(sqlite3.connect(':memory:')) as conn:
        _, plan, r3, values = make_source_sample(conn, role=role)
        target, binding = module(), bind(values)
        assert target.read_source_epoch(conn, binding=binding, quarantine_plan=plan) is None
        apply_epoch(conn, binding)
        receipt = target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)
        assert receipt.binding.role == role and receipt.r3_preparation_sha256 == digest(asdict(r3))
        with pytest.raises(target.SourceEpochError):
            target.read_source_epoch(conn, binding=replace(binding, role='team' if role != 'team' else 'billing'), quarantine_plan=plan)


@pytest.mark.parametrize('table,column,value', [
    ('restore_source_epoch_binding', 'binding_json', '{}'),
    ('restore_source_epoch_binding', 'binding_sha256', 'f' * 64),
    ('restore_source_epoch_seal', 'binding_sha256', 'f' * 64),
    ('restore_source_epoch_seal', 'seal_sha256', 'f' * 64),
])
def test_exact_reinstalled_schema_never_hides_corrupt_epoch_rows(source_sample, table, column, value):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    apply_epoch(conn, binding)
    name = table + '_no_update'
    original = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
    conn.execute('DROP TRIGGER ' + name)
    conn.execute('UPDATE ' + table + ' SET ' + column + '=?', (value,))
    conn.execute(original)
    with pytest.raises(target.SourceEpochError):
        target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)


@pytest.mark.parametrize('bound', ['MAX_SECONDS', 'MAX_ROWS', 'MAX_CONTENT_BYTES'])
def test_incomplete_rowid_scan_never_returns_a_projection(source_sample, monkeypatch, bound):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    apply_epoch(conn, binding)
    monkeypatch.setattr(target, bound, 0)
    with pytest.raises(target.SourceEpochError):
        target.prepared_source_projection(conn, binding=binding, quarantine_plan=plan)


def test_closed_connection_is_a_sanitized_refusal(source_sample):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    conn.close()
    with pytest.raises(target.SourceEpochError, match='^restore_source_epoch_invalid$'):
        target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)


@pytest.mark.parametrize('prepared', [False, True])
@pytest.mark.parametrize('table', ['restore_source_epoch_binding', 'restore_source_epoch_seal',
    'jobs', 'restore_quarantine_sets'])
def test_temp_aliases_are_refused_before_absent_or_prepared_evidence_read(source_sample, prepared, table):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    if prepared:
        apply_epoch(conn, binding)
    if table.startswith('restore_source_epoch_') and not prepared:
        conn.execute('CREATE TEMP TABLE ' + table + '(id INTEGER)')
    else:
        conn.execute('CREATE TEMP TABLE ' + table + ' AS SELECT * FROM main.' + table)
        if table == 'jobs':
            conn.execute("UPDATE main.jobs SET status='confirmed'")
        else:
            name = table + '_no_update'
            trigger = conn.execute('SELECT sql FROM main.sqlite_master WHERE name=?', (name,)).fetchone()[0]
            conn.execute('DROP TRIGGER main.' + name)
            column = 'prepared_by_R3_id' if table == 'restore_quarantine_sets' else 'binding_sha256'
            conn.execute('UPDATE main.' + table + ' SET ' + column + '=?',
                (str(uuid4()) if column == 'prepared_by_R3_id' else 'f' * 64,))
            conn.execute(trigger)
    before_main, before_temp = conn.serialize(name='main'), conn.serialize(name='temp')
    with pytest.raises(target.SourceEpochError, match='^restore_source_epoch_invalid$'):
        target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)
    with pytest.raises(target.SourceEpochError):
        target.prepared_source_projection(conn, binding=binding, quarantine_plan=plan)
    assert conn.serialize(name='main') == before_main
    assert conn.serialize(name='temp') == before_temp


@pytest.mark.parametrize('prepared', [False, True])
def test_attached_database_is_refused_even_without_namesake_objects(source_sample, prepared):
    conn, plan, _, values = source_sample
    target, binding = module(), bind(values)
    if prepared:
        apply_epoch(conn, binding)
    conn.execute("ATTACH ':memory:' AS foreign_source")
    conn.execute('CREATE TABLE foreign_source.unrelated(id INTEGER)')
    conn.execute('INSERT INTO foreign_source.unrelated VALUES(7)')
    before_main = conn.serialize(name='main')
    before_foreign = conn.serialize(name='foreign_source')
    with pytest.raises(target.SourceEpochError, match='^restore_source_epoch_invalid$'):
        target.read_source_epoch(conn, binding=binding, quarantine_plan=plan)
    with pytest.raises(target.SourceEpochError):
        target.prepared_source_projection(conn, binding=binding, quarantine_plan=plan)
    assert conn.serialize(name='main') == before_main
    assert conn.serialize(name='foreign_source') == before_foreign
