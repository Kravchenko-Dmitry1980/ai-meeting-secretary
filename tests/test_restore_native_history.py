"""Native history precursor checks use synthetic SQLite only; DTOs are not permits."""
from contextlib import closing
from dataclasses import replace
import hashlib
import importlib
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


def module():
    return importlib.import_module('secretary.infrastructure.restore_native_history')


def make_source(tmp_path):
    m=module()
    path=tmp_path/'native fixture % safe.sqlite3'
    with closing(sqlite3.connect(path)) as conn:
        for table in m.PINNED_REGISTERED_TABLES:
            definition='id TEXT PRIMARY KEY' if table=='migration' else 'id INTEGER PRIMARY KEY, value TEXT'
            conn.execute(f'CREATE TABLE "{table}" ({definition})')
        conn.executemany('INSERT INTO migration(id) VALUES(?)',(('20260928140648',),('SCHEMA_INIT',)))
        conn.commit()
        descriptor=m.native_schema_descriptor(conn)
    binding=m.NativeHistoryBinding(deployment_id=str(uuid4()),epoch_id=str(uuid4()),
        decision_id=str(uuid4()),decision_sha256=hashlib.sha256(b'approved synthetic decision').hexdigest(),
        expected_native_schema_sha256=descriptor.schema_sha256,
        expected_migration_ids=descriptor.migration_ids)
    return m,path,binding


def install(tmp_path):
    m,path,binding=make_source(tmp_path)
    plan=m.build_install_plan(binding)
    with closing(sqlite3.connect(path)) as conn:
        for statement in plan.statements:
            conn.execute(statement.sql,statement.parameters)
        conn.commit()
    snapshot=m.read_existing(path,binding=binding,retained_head=None)
    return m,path,binding,plan,snapshot


def tree_bytes(root):
    return {p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}


def test_missing_protocol_is_blocked_before_any_writes(tmp_path):
    m,path,binding=make_source(tmp_path)
    before=tree_bytes(tmp_path)
    with pytest.raises(m.NativeHistoryError,match='maintenance_restore_blocked') as error:
        m.read_existing(path,binding=binding,retained_head=None)
    assert error.value.reason=='native_protocol_missing'
    assert tree_bytes(tmp_path)==before


def test_install_is_declarative_and_full_catalogue(tmp_path):
    m,path,binding=make_source(tmp_path)
    before=tree_bytes(tmp_path)
    plan=m.build_install_plan(binding)
    assert tree_bytes(tmp_path)==before
    assert len(m.PINNED_REGISTERED_TABLES)==42
    assert len([row for row in plan.protocol_schema if row[1]=='trigger' and row[2] in m.PINNED_REGISTERED_TABLES])==42*6
    assert m.catalogue_metadata()['native_catalogue']=='OBSERVED_EXACT_PINNED_NATIVE_DUMP'


def test_empty_history_reopens_without_sidecars_or_changes(tmp_path):
    m,path,binding,plan,snapshot=install(tmp_path)
    before=tree_bytes(tmp_path)
    again=m.read_existing(path,binding=binding,retained_head=snapshot.head)
    assert again==snapshot
    assert again.head.sequence==0 and again.head.nonce is None
    assert tree_bytes(tmp_path)==before


@pytest.mark.parametrize('table',('projects','users','files','license_status','notifications','migration_status'))
def test_committed_native_table_mutations_advance_chained_history(tmp_path,table):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f'INSERT INTO "{table}" VALUES(1,?)',('first',))
        conn.execute(f'UPDATE "{table}" SET value=? WHERE id=1',('second',))
        conn.execute(f'DELETE FROM "{table}" WHERE id=1')
        conn.commit()
    observed=m.read_existing(path,binding=binding,retained_head=start.head)
    assert observed.head.sequence==3
    assert observed.event_count==3 and len(observed.head.nonce)==64
    assert observed.head.digest!=start.head.digest


def test_sql_rollback_returns_business_and_history_to_committed_state(tmp_path):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('INSERT INTO tasks VALUES(1,?)',('committed',)); conn.commit()
    committed=m.read_existing(path,binding=binding,retained_head=start.head)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE tasks SET value=? WHERE id=1',('rolled back',))
        assert conn.execute(f'SELECT count(*) FROM "{m.HISTORY_TABLE}"').fetchone()[0]==2
        conn.rollback()
        assert conn.execute('SELECT value FROM tasks').fetchone()[0]=='committed'
    assert m.read_existing(path,binding=binding,retained_head=committed.head)==committed


@pytest.mark.parametrize('table',('epoch','history'))
@pytest.mark.parametrize('verb',('UPDATE','DELETE','REPLACE'))
def test_protocol_rows_reject_update_delete_and_replace(tmp_path,table,verb):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('INSERT INTO tasks VALUES(1,?)',('committed',)); conn.commit()
        target=m.EPOCH_TABLE if table=='epoch' else m.HISTORY_TABLE
        original=conn.execute(f'SELECT * FROM "{target}"').fetchall()
        if verb=='DELETE': sql=f'DELETE FROM "{target}"'
        elif verb=='UPDATE': sql=f'UPDATE "{target}" SET '+('epoch_id=epoch_id' if table=='epoch' else 'nonce=nonce')
        else:
            placeholders=','.join('?' for _ in original[0])
            sql=f'INSERT OR REPLACE INTO "{target}" VALUES({placeholders})'
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql,original[0] if verb=='REPLACE' else ())
        conn.rollback()
        assert conn.execute(f'SELECT * FROM "{target}"').fetchall()==original


def test_replaced_business_row_has_history_without_recursive_triggers(tmp_path):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        conn.execute('INSERT INTO tasks VALUES(1,?)',('old',)); conn.commit()
        conn.execute('INSERT OR REPLACE INTO tasks VALUES(1,?)',('new',)); conn.commit()
    observed=m.read_existing(path,binding=binding,retained_head=start.head)
    assert observed.head.sequence==2


@pytest.mark.parametrize('change',('missing_trigger','changed_trigger','extra_protocol_object','base_schema','migration_set','epoch_marker','history_nonce','history_gap','history_previous'))
def test_changed_marker_schema_migrations_or_history_is_blocked(tmp_path,change):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('INSERT INTO tasks VALUES(1,?)',('first',)); conn.commit()
    saved=m.read_existing(path,binding=binding,retained_head=start.head)
    with closing(sqlite3.connect(path)) as conn:
        if change in ('missing_trigger','changed_trigger'):
            row=next(row for row in plan.protocol_schema if row[1]=='trigger' and row[2]=='tasks')
            conn.execute(f'DROP TRIGGER "{row[0]}"')
            if change=='changed_trigger': conn.execute(f'CREATE TRIGGER "{row[0]}" AFTER UPDATE ON tasks BEGIN SELECT 1; END')
        elif change=='extra_protocol_object': conn.execute(f'CREATE TABLE "{m.PROTOCOL_PREFIX}unexpected"(id INTEGER)')
        elif change=='base_schema': conn.execute('CREATE TABLE unexpected_native_table(id INTEGER)')
        elif change=='migration_set': conn.execute('INSERT INTO migration(id) VALUES(?)',('unapproved_migration',))
        else:
            target=m.EPOCH_TABLE if change=='epoch_marker' else m.HISTORY_TABLE
            for name, in conn.execute('SELECT name FROM sqlite_master WHERE type=? AND tbl_name=?',('trigger',target)).fetchall():
                conn.execute(f'DROP TRIGGER "{name}"')
            if change=='epoch_marker': conn.execute(f'UPDATE "{target}" SET epoch_id=?',(str(uuid4()),))
            elif change=='history_nonce': conn.execute(f'UPDATE "{target}" SET nonce=?',('a'*64,))
            elif change=='history_gap': conn.execute(f'UPDATE "{target}" SET seq=2')
            else: conn.execute(f'UPDATE "{target}" SET previous_nonce=?',('a'*64,))
            for row in plan.protocol_schema:
                if row[1]=='trigger' and row[2]==target: conn.execute(row[3])
        conn.commit()
    before=tree_bytes(tmp_path)
    with pytest.raises(m.NativeHistoryError,match='maintenance_restore_blocked'):
        m.read_existing(path,binding=binding,retained_head=saved.head)
    assert tree_bytes(tmp_path)==before


def test_retain_head_detects_same_epoch_snapshot_rollback(tmp_path):
    m,path,binding,plan,start=install(tmp_path)
    backup=tmp_path/'older.sqlite3'
    with closing(sqlite3.connect(path)) as conn, closing(sqlite3.connect(backup)) as old:
        conn.execute('INSERT INTO tasks VALUES(1,?)',('first',)); conn.commit(); conn.backup(old)
        conn.execute('UPDATE tasks SET value=? WHERE id=1',('second',)); conn.commit()
    head=m.read_existing(path,binding=binding,retained_head=start.head).head
    assert head.sequence==2
    with pytest.raises(m.NativeHistoryError) as error:
        m.read_existing(backup,binding=binding,retained_head=head)
    assert error.value.reason=='native_history_rollback'


@pytest.mark.parametrize('field',('epoch_id','nonce','digest','sequence'))
def test_wrong_independent_head_is_rejected(tmp_path,field):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('INSERT INTO tasks VALUES(1,?)',('first',)); conn.commit()
    saved=m.read_existing(path,binding=binding,retained_head=start.head)
    wrong={'epoch_id':str(uuid4()),'nonce':'f'*64,'digest':'f'*64,'sequence':2}[field]
    with pytest.raises(m.NativeHistoryError):
        m.read_existing(path,binding=binding,retained_head=replace(saved.head,**{field:wrong}))


@pytest.mark.parametrize('suffix',('-wal','-shm','-journal'))
def test_journaled_source_is_refused_without_immutable_ignore_or_sidecars(tmp_path,suffix):
    m,path,binding,plan,start=install(tmp_path)
    Path(str(path)+suffix).write_bytes(b'synthetic pending journal')
    before=tree_bytes(tmp_path)
    with pytest.raises(m.NativeHistoryError) as error:
        m.read_existing(path,binding=binding,retained_head=start.head)
    assert error.value.reason=='native_source_journaled'
    assert tree_bytes(tmp_path)==before


def test_history_bounds_are_enforced_without_mutation(tmp_path,monkeypatch):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('INSERT INTO tasks VALUES(1,?)',('first',)); conn.commit()
    monkeypatch.setattr(m,'MAX_HISTORY_EVENTS',0)
    before=tree_bytes(tmp_path)
    with pytest.raises(m.NativeHistoryError) as error:
        m.read_existing(path,binding=binding,retained_head=start.head)
    assert error.value.reason=='native_history_bounded'
    assert tree_bytes(tmp_path)==before


@pytest.mark.parametrize('invalid',('relative','missing','bad_binding'))
def test_unusable_paths_or_bindings_have_sanitized_failures(tmp_path,invalid):
    m,path,binding,plan,start=install(tmp_path)
    if invalid=='relative': path=Path('relative-private.sqlite3')
    elif invalid=='missing': path=tmp_path/'missing.sqlite3'
    else: binding=replace(binding,decision_id='private arbitrary content')
    with pytest.raises(m.NativeHistoryError) as error:
        m.read_existing(path,binding=binding,retained_head=start.head)
    assert str(error.value)=='maintenance_restore_blocked'
    assert error.value.code=='maintenance_restore_blocked'
    assert 'private' not in str(error.value)


def test_import_and_plan_never_create_files(tmp_path,monkeypatch):
    m,path,binding=make_source(tmp_path)
    monkeypatch.chdir(tmp_path)
    before=tree_bytes(tmp_path)
    canonical_binding_type=m.NativeHistoryBinding
    # A fresh module execution tests import effects without replacing classes
    # already retained by other production modules in this pytest process.
    name=f'{m.__package__}._native_history_import_probe_{uuid4().hex}'
    spec=importlib.util.spec_from_file_location(name,m.__file__)
    assert spec is not None and spec.loader is not None
    probe=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,name,probe)
    spec.loader.exec_module(probe)
    probe.build_install_plan(probe.NativeHistoryBinding(**binding.__dict__))
    assert module() is m and m.NativeHistoryBinding is canonical_binding_type
    assert tree_bytes(tmp_path)==before


def test_unknown_protocol_named_trigger_stays_in_base_fingerprint(tmp_path):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        original=m.native_schema_descriptor(conn,binding=binding)
        conn.execute(f'CREATE TRIGGER "{m.PROTOCOL_PREFIX}unexpected_trigger" AFTER INSERT ON tasks BEGIN SELECT 1; END')
        conn.commit()
        changed=m.native_schema_descriptor(conn,binding=binding)
    assert changed.schema_sha256!=original.schema_sha256
    with pytest.raises(m.NativeHistoryError) as error:
        m.read_existing(path,binding=binding,retained_head=start.head)
    assert error.value.reason=='native_base_schema_changed'


def test_connection_reader_uses_actual_transaction_view_and_restores_limits(tmp_path):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        limit=conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO tasks VALUES(1,?)',('transaction local',))
        diagnostic=m.read_connection(conn,binding=binding,retained_head=start.head)
        assert diagnostic.head.sequence==1 and diagnostic.qualification=='DIAGNOSTIC_ONLY'
        assert conn.in_transaction and conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)==limit
        conn.rollback()
    assert m.read_existing(path,binding=binding,retained_head=start.head)==start


def test_history_replace_nonce_collision_cannot_delete_an_older_record(tmp_path):
    m,path,binding,plan,start=install(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        conn.execute('INSERT INTO tasks VALUES(1,?)',('committed',)); conn.commit()
        original=conn.execute(f'SELECT * FROM "{m.HISTORY_TABLE}"').fetchall()
        nonce=original[-1][2]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f'INSERT OR REPLACE INTO "{m.HISTORY_TABLE}" VALUES(?,?,?,?,?,?,?)',
                         (None,binding.epoch_id,nonce,nonce,'tasks','UPDATE',1))
        conn.rollback()
        assert conn.execute(f'SELECT * FROM "{m.HISTORY_TABLE}"').fetchall()==original


@pytest.mark.parametrize('unsafe',(
    r'\\remote.invalid\share\source.sqlite3',
    '//remote.invalid/share/source.sqlite3',
    r'\/remote.invalid\share\source.sqlite3',
    r'/\remote.invalid/share/source.sqlite3',
    r'\\?\C:\source.sqlite3',
    r'\\.\C:\source.sqlite3',
    r'C:\source.sqlite3:private-stream',
    r'C:\source.sqlite3::$DATA',
    r'C:\NUL.sqlite3',
))
def test_nonlocal_device_and_ads_paths_are_rejected_before_filesystem_access(tmp_path,monkeypatch,unsafe):
    m,path,binding,plan,start=install(tmp_path)
    def no_filesystem(*args,**kwargs):
        raise AssertionError('unsafe lexical path must not reach the filesystem')
    with monkeypatch.context() as scoped:
        scoped.setattr(Path,'stat',no_filesystem)
        scoped.setattr(Path,'lstat',no_filesystem)
        with pytest.raises(m.NativeHistoryError) as error:
            m.read_existing(unsafe,binding=binding,retained_head=start.head)
    assert error.value.reason=='native_source_path_invalid'


def test_hardlinked_source_is_rejected_without_reading_or_mutation(tmp_path,monkeypatch):
    m,path,binding,plan,start=install(tmp_path)
    alias=tmp_path/'synthetic hardlink.sqlite3'
    os.link(path,alias)
    assert path.stat().st_nlink==2
    before=tree_bytes(tmp_path)
    def no_read(*args,**kwargs):
        raise AssertionError('hardlinked source must be rejected before reading')
    with monkeypatch.context() as scoped:
        scoped.setattr(m,'_identity',no_read)
        with pytest.raises(m.NativeHistoryError) as error:
            m.read_existing(path,binding=binding,retained_head=start.head)
        assert error.value.reason=='native_source_path_invalid'
    assert tree_bytes(tmp_path)==before


@pytest.mark.parametrize('untrusted',('private arbitrary caller reason',None,{},['native_source_invalid']))
def test_public_error_constructor_keeps_reason_in_sanitized_allowlist(untrusted):
    m=module()
    error=m.NativeHistoryError(untrusted)
    assert error.reason=='native_source_invalid'
    assert str(error)==error.code=='maintenance_restore_blocked'


def test_reparse_ancestor_is_rejected_before_descendant_traversal(tmp_path,monkeypatch):
    m,path,binding,plan,start=install(tmp_path)
    original=Path.lstat
    ancestor=path.parent
    info=ancestor.lstat()
    visited=[]
    def bounded_lstat(current,*args,**kwargs):
        visited.append(current)
        if current==path:
            raise AssertionError('leaf traversal must wait until ancestors are validated')
        if current==ancestor:
            return SimpleNamespace(st_mode=info.st_mode,st_file_attributes=0x400)
        return original(current,*args,**kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path,'lstat',bounded_lstat)
        with pytest.raises(m.NativeHistoryError) as error:
            m.read_existing(path,binding=binding,retained_head=start.head)
        assert error.value.reason=='native_source_reparse'
    assert path not in visited and visited[-1]==ancestor
