"""Real synthetic SQLite; prepared native epochs never enable ordinary writes."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import importlib
import json
import sqlite3
from uuid import uuid4

import pytest

from secretary.infrastructure import restore_native_history as history
from secretary.infrastructure.team_restore_reconciliation import _rowids


def module():
    return importlib.import_module('secretary.infrastructure.restore_native_preparation')


def fixture(tmp_path):
    m = module()
    path = tmp_path / 'inactive native.sqlite3'
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        for table in history.PINNED_REGISTERED_TABLES:
            definition = 'id TEXT PRIMARY KEY' if table == 'migration' else 'id INTEGER PRIMARY KEY, value TEXT'
            conn.execute(f'CREATE TABLE "{table}" ({definition})')
        conn.executemany('INSERT INTO migration VALUES(?)', [('SCHEMA_INIT',), ('20260928140648',)])
        for table in history.PINNED_REGISTERED_TABLES:
            if table != 'migration': conn.execute(f'INSERT INTO "{table}" VALUES(1,?)', ('unchanged',))
        descriptor = history.native_schema_descriptor(conn)
        baseline_rowids = _rowids(conn)
    native = history.NativeHistoryBinding(str(uuid4()), str(uuid4()), str(uuid4()),
        sha256(b'prepared proposal only').hexdigest(), descriptor.schema_sha256, descriptor.migration_ids)
    binding = m.NativePreparationBinding(native=native, restore_id=str(uuid4()), r3_id=str(uuid4()),
        generation_id=str(uuid4()), ledger_id=str(uuid4()), source_commitment_sha256='a' * 64,
        r3_preparation_sha256='b' * 64, baseline_file_sha256=sha256(path.read_bytes()).hexdigest(),
        source_path_sha256=sha256(str(path).encode()).hexdigest(), file_id=(path.stat().st_dev, path.stat().st_ino),
        baseline_rowids_sha256=baseline_rowids)
    return m, path, binding


def prepared(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute('BEGIN EXCLUSIVE')
        receipt = m.prepare_native_source_in_connection(conn, binding=binding)
        assert conn.in_transaction
        conn.commit()
    return m, path, binding, receipt


def actual_schema(conn):
    return tuple(conn.execute('SELECT name,type,tbl_name,sql FROM sqlite_master ORDER BY name'))


def business_rows(conn):
    return {table: conn.execute(f'SELECT _rowid_,* FROM "{table}" ORDER BY _rowid_').fetchall()
        for table in history.PINNED_REGISTERED_TABLES}


def test_hold_plan_is_declarative_and_never_mints_activation(tmp_path):
    m, path, binding = fixture(tmp_path)
    before = path.read_bytes()
    plan = m.build_hold_plan(binding)
    assert path.read_bytes() == before
    assert plan.state == 'source_prepared_blocked'
    assert plan.activation_supported is False
    assert plan.outbound_enabled is False
    assert sum(row[1] == 'trigger' and row[2] in history.PINNED_REGISTERED_TABLES
        for row in plan.protocol_schema) == 126


def test_owned_transaction_prepares_once_and_durable_reader_verifies_projection(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        before_rows = business_rows(conn)
        original_history_plan = history.build_install_plan(binding.native)
        conn.execute('BEGIN EXCLUSIVE')
        receipt = m.prepare_native_source_in_connection(conn, binding=binding)
        assert conn.in_transaction
        assert business_rows(conn) == before_rows
        native_objects = tuple(row for row in actual_schema(conn) if row[0] in {
            item[0] for item in original_history_plan.protocol_schema})
        assert native_objects == original_history_plan.protocol_schema
        conn.commit()
    before = path.read_bytes()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        again = m.read_prepared_native(conn, binding=binding)
        assert again == receipt
        assert again.native_snapshot.head.sequence == 0
        assert again.native_snapshot.head.nonce is None
        assert again.state == 'source_prepared_blocked'
        assert again.activation_supported is False and again.outbound_enabled is False
        assert set(again.business_tables) == set(history.PINNED_REGISTERED_TABLES)
        assert set(again.metadata_tables) == {history.EPOCH_TABLE, history.HISTORY_TABLE,
            'secretary_r4_native_prepared_hold', 'secretary_r4_native_future_grants'}
        assert m.native_business_rowids(conn, binding=binding) == binding.baseline_rowids_sha256
    assert path.read_bytes() == before
    assert not any(tmp_path.glob('*-wal')) and not any(tmp_path.glob('*-shm'))


@pytest.mark.parametrize('table', history.PINNED_REGISTERED_TABLES)
@pytest.mark.parametrize('verb', ('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))
def test_every_business_table_stays_denied_after_custody_release_and_authorizer_clear(tmp_path, table, verb):
    m, path, binding, receipt = prepared(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.set_authorizer(None)
        conn.execute('PRAGMA recursive_triggers=OFF')
        before = business_rows(conn)
        if table == 'migration':
            sql = {'INSERT': 'INSERT INTO migration VALUES(?)', 'UPDATE': 'UPDATE migration SET id=? WHERE id=\'SCHEMA_INIT\'',
                'DELETE': "DELETE FROM migration WHERE id='SCHEMA_INIT'", 'REPLACE': "INSERT OR REPLACE INTO migration VALUES('SCHEMA_INIT')"}[verb]
            params = ('20260928140649',) if verb in ('INSERT', 'UPDATE') else ()
        else:
            sql = {'INSERT': f'INSERT INTO "{table}" VALUES(2,?)', 'UPDATE': f'UPDATE "{table}" SET value=? WHERE id=1',
                'DELETE': f'DELETE FROM "{table}" WHERE id=1', 'REPLACE': f'INSERT OR REPLACE INTO "{table}" VALUES(1,?)'}[verb]
            params = ('forbidden',) if verb != 'DELETE' else ()
        with pytest.raises(sqlite3.IntegrityError, match='native_source_prepared_blocked'):
            conn.execute(sql, params)
        assert business_rows(conn) == before
        assert m.read_prepared_native(conn, binding=binding) == receipt


@pytest.mark.parametrize('verb', ('INSERT', 'REPLACE', 'UPDATE', 'DELETE'))
def test_future_grant_sql_is_denied_even_without_an_issuer(tmp_path, verb):
    m, path, binding, receipt = prepared(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        sql = {'INSERT': 'INSERT INTO secretary_r4_native_future_grants VALUES(1,?)',
            'REPLACE': 'INSERT OR REPLACE INTO secretary_r4_native_future_grants VALUES(1,?)',
            'UPDATE': 'UPDATE secretary_r4_native_future_grants SET grant_sha256=?',
            'DELETE': 'DELETE FROM secretary_r4_native_future_grants'}[verb]
        # SQLite row triggers do not fire for UPDATE/DELETE of an empty table.
        if verb in ('INSERT', 'REPLACE'):
            with pytest.raises(sqlite3.IntegrityError, match='native_source_prepared_blocked'):
                conn.execute(sql, ('f' * 64,))
        else:
            conn.execute(sql, () if verb == 'DELETE' else ('f' * 64,))
        assert conn.execute('SELECT count(*) FROM secretary_r4_native_future_grants').fetchone()[0] == 0
        assert m.read_prepared_native(conn, binding=binding) == receipt


@pytest.mark.parametrize('verb', ('INSERT', 'UPDATE', 'DELETE', 'REPLACE'))
def test_prepared_hold_is_immutable(tmp_path, verb):
    m, path, binding, receipt = prepared(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        row = conn.execute('SELECT * FROM secretary_r4_native_prepared_hold').fetchone()
        if verb in ('INSERT', 'REPLACE'):
            sql = ('INSERT' if verb == 'INSERT' else 'INSERT OR REPLACE') + ' INTO secretary_r4_native_prepared_hold VALUES(' + ','.join('?' for _ in row) + ')'
            params = row
        elif verb == 'UPDATE': sql, params = 'UPDATE secretary_r4_native_prepared_hold SET binding_sha256=binding_sha256', ()
        else: sql, params = 'DELETE FROM secretary_r4_native_prepared_hold', ()
        with pytest.raises(sqlite3.IntegrityError, match='native_source_prepared_blocked'):
            conn.execute(sql, params)
        assert m.read_prepared_native(conn, binding=binding) == receipt


def test_exact_prepared_repeat_executes_no_ddl_and_keeps_own_transaction(tmp_path):
    m, path, binding, receipt = prepared(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        conn.execute('BEGIN EXCLUSIVE')
        assert m.prepare_native_source_in_connection(conn, binding=binding) == receipt
        assert conn.in_transaction
        assert not any(sql.lstrip().upper().startswith(('CREATE', 'DROP', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'COMMIT')) for sql in statements)
        conn.rollback()


def test_install_rollback_leaves_original_source_and_same_tuple_can_retry(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        before_schema, before_rows = actual_schema(conn), business_rows(conn)
        conn.execute('BEGIN EXCLUSIVE')
        m.prepare_native_source_in_connection(conn, binding=binding)
        conn.rollback()
        assert actual_schema(conn) == before_schema
        assert business_rows(conn) == before_rows
        conn.execute('BEGIN EXCLUSIVE')
        result = m.prepare_native_source_in_connection(conn, binding=binding)
        assert result.native_snapshot.head.sequence == 0
        conn.commit()


@pytest.mark.parametrize('change', ('partial_hold', 'hold_trigger', 'history_trigger', 'unknown_hold_trigger', 'unknown_hold_table', 'different_epoch', 'business_data', 'migration_data', 'nonzero_history'))
def test_changed_or_partial_preparation_is_refused_without_repair_or_writes(tmp_path, change):
    m, path, binding, receipt = prepared(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        if change == 'partial_hold': conn.execute('DROP TRIGGER secretary_r4_native_tasks_deny_insert')
        elif change == 'hold_trigger':
            conn.execute('DROP TRIGGER secretary_r4_native_tasks_deny_insert')
            conn.execute('CREATE TRIGGER secretary_r4_native_tasks_deny_insert BEFORE INSERT ON tasks BEGIN SELECT 1; END')
        elif change == 'history_trigger':
            conn.execute('DROP TRIGGER secretary_native_activation_v1_tasks_history_insert')
        elif change == 'unknown_hold_trigger': conn.execute('CREATE TRIGGER secretary_r4_native_unknown AFTER INSERT ON tasks BEGIN SELECT 1; END')
        elif change == 'unknown_hold_table': conn.execute('CREATE TABLE secretary_r4_native_unknown(id INTEGER)')
        elif change == 'different_epoch': binding = replace(binding, native=replace(binding.native, epoch_id=str(uuid4())))
        elif change in ('business_data', 'migration_data'):
            table = 'tasks' if change == 'business_data' else 'migration'
            for name, in conn.execute('SELECT name FROM sqlite_master WHERE type=\'trigger\' AND tbl_name=?', (table,)).fetchall():
                conn.execute(f'DROP TRIGGER "{name}"')
            if table == 'tasks': conn.execute("UPDATE tasks SET value='changed' WHERE id=1")
            else: conn.execute("INSERT INTO migration VALUES('20260928140649')")
            for row in receipt.metadata_schema:
                if row[1] == 'trigger' and row[2] == table: conn.execute(row[3])
        else:
            conn.execute(f'INSERT INTO "{history.HISTORY_TABLE}"(epoch_id,nonce,previous_nonce,table_name,operation,native_rowid) VALUES(?,?,?,?,?,?)',
                (binding.native.epoch_id, 'c' * 64, None, 'tasks', 'UPDATE', 1))
        before_schema, before_rows = actual_schema(conn), business_rows(conn)
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(m.NativePreparationError, match='maintenance_restore_blocked'):
            m.prepare_native_source_in_connection(conn, binding=binding)
        assert conn.in_transaction
        assert actual_schema(conn) == before_schema and business_rows(conn) == before_rows
        conn.rollback()


def test_history_only_old_precursor_cannot_be_silently_upgraded(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        for statement in history.build_install_plan(binding.native).statements:
            conn.execute(statement.sql, statement.parameters)
        before = actual_schema(conn)
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(m.NativePreparationError): m.prepare_native_source_in_connection(conn, binding=binding)
        assert actual_schema(conn) == before
        conn.rollback()


def test_initial_watermark_and_descriptor_are_checked_before_first_write(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("UPDATE tasks SET value='foreign change' WHERE id=1")
        before = actual_schema(conn)
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(m.NativePreparationError): m.prepare_native_source_in_connection(conn, binding=binding)
        assert actual_schema(conn) == before
        conn.rollback()


@pytest.mark.parametrize('invalid', ('native', 'restore_id', 'r3_id', 'generation_id', 'ledger_id',
    'source_commitment_sha256', 'r3_preparation_sha256', 'baseline_file_sha256', 'source_path_sha256',
    'file_id', 'baseline_rowids_sha256'))
def test_invalid_binding_fails_before_schema_mutation(tmp_path, invalid):
    m, path, binding = fixture(tmp_path)
    bad = replace(binding, **{invalid: ('malformed secret must not escape' if invalid != 'file_id' else (True, 1))})
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        before = actual_schema(conn)
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(m.NativePreparationError, match='^maintenance_restore_blocked$') as error:
            m.prepare_native_source_in_connection(conn, binding=bad)
        assert 'secret' not in error.value.reason
        assert actual_schema(conn) == before
        conn.rollback()


def test_preparer_requires_a_caller_owned_transaction_and_does_not_begin_one(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        before = actual_schema(conn)
        with pytest.raises(m.NativePreparationError): m.prepare_native_source_in_connection(conn, binding=binding)
        assert not conn.in_transaction and actual_schema(conn) == before


def test_initial_business_projection_rejects_protocol_named_collisions(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        assert m.native_business_rowids(conn) == binding.baseline_rowids_sha256
        conn.execute('CREATE TRIGGER secretary_r4_native_unknown AFTER INSERT ON tasks BEGIN SELECT 1; END')
        with pytest.raises(m.NativePreparationError): m.native_business_rowids(conn)


def test_default_history_reader_does_not_ignore_hold_metadata(tmp_path):
    m, path, binding, receipt = prepared(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        with pytest.raises(history.NativeHistoryError): history.read_connection(conn, binding=binding.native)
        assert m.read_prepared_native(conn, binding=binding) == receipt


def test_non_connection_and_public_error_reasons_are_sanitized(tmp_path):
    m, path, binding = fixture(tmp_path)
    with pytest.raises(m.NativePreparationError, match='^maintenance_restore_blocked$'):
        m.read_prepared_native(object(), binding=binding)
    assert m.NativePreparationError('raw secret').reason == 'native_preparation_invalid'


@pytest.mark.parametrize('shadow', ('fake_grant', 'changed_business'))
def test_temp_table_cannot_hide_main_grant_or_changed_business(tmp_path, shadow):
    m, path, binding, receipt = prepared(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        table = 'secretary_r4_native_future_grants' if shadow == 'fake_grant' else 'tasks'
        triggers = [row for row in receipt.metadata_schema if row[1] == 'trigger' and row[2] == table]
        for row in triggers: conn.execute(f'DROP TRIGGER "{row[0]}"')
        if shadow == 'fake_grant':
            conn.execute('INSERT INTO main.secretary_r4_native_future_grants VALUES(1,?)', ('e' * 64,))
        else:
            conn.execute("UPDATE main.tasks SET value='foreign changed business' WHERE id=1")
        for row in triggers: conn.execute(row[3])
        if shadow == 'fake_grant':
            conn.execute('CREATE TEMP TABLE secretary_r4_native_future_grants(singleton INTEGER PRIMARY KEY,grant_sha256 TEXT)')
        else:
            conn.execute('CREATE TEMP TABLE tasks(id INTEGER PRIMARY KEY,value TEXT)')
            conn.execute("INSERT INTO temp.tasks VALUES(1,'unchanged')")
        before = actual_schema(conn)
        with pytest.raises(m.NativePreparationError, match='maintenance_restore_blocked'):
            m.read_prepared_native(conn, binding=binding)
        assert actual_schema(conn) == before


def test_initial_temp_alias_is_refused_before_any_installation(tmp_path):
    m, path, binding = fixture(tmp_path)
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute('CREATE TEMP TABLE tasks(id INTEGER PRIMARY KEY,value TEXT)')
        conn.execute("INSERT INTO temp.tasks VALUES(1,'unchanged')")
        before = actual_schema(conn)
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(m.NativePreparationError, match='maintenance_restore_blocked'):
            m.prepare_native_source_in_connection(conn, binding=binding)
        assert actual_schema(conn) == before
        conn.rollback()
