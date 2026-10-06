"""Restored Team authority stays read-only before SQLite setup or migrations."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from secretary.infrastructure import maintenance_binding
from secretary.infrastructure.team_backup import _restore_guard
from secretary.infrastructure.team_database import SCHEMA, SCHEMA_VERSION, TeamDatabase


def _snapshot(path):
    return path.read_bytes(), tuple(sorted(item.name for item in path.parent.iterdir()))


def _sidecars(path):
    return tuple(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))


def _guard(path):
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            _restore_guard(conn, str(uuid4()), 'a' * 64)


def _legacy_store(path, *, guarded=True, version=SCHEMA_VERSION):
    if version == SCHEMA_VERSION:
        db = TeamDatabase(path)
    else:
        with closing(sqlite3.connect(path)) as conn:
            with conn:
                for sql in SCHEMA:
                    conn.execute(sql)
                conn.execute('INSERT INTO team_schema VALUES(?)', (version,))
        db = None
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            conn.execute("INSERT INTO team_messages VALUES('legacy-message','notice','legacy-dedup',?, ?, ?)",
                         ('b' * 64, '{"text":"Synthetic old message"}', '2026-10-04T00:00:00Z'))
            conn.execute("INSERT INTO team_message_state(id,state) VALUES('legacy-message','queued')")
            if guarded:
                _restore_guard(conn, str(uuid4()), 'a' * 64)
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    # A real persisted WAL header without live connections is the important
    # case: even an ordinary mode=ro identification probe can create sidecars.
    assert path.read_bytes()[18:20] == b'\x02\x02'
    assert not any(item.exists() for item in _sidecars(path))
    return db


@pytest.fixture
def restored(tmp_path):
    path = tmp_path / 'team.sqlite3'
    old_handle = _legacy_store(path)
    return path, old_handle


def _assert_legacy(conn, version=SCHEMA_VERSION):
    assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == version
    assert conn.execute("SELECT state FROM team_message_state WHERE id='legacy-message'").fetchone()[0] == 'queued'
    assert conn.execute('SELECT COUNT(*) FROM team_messages').fetchone()[0] == 1


def test_guarded_constructor_never_creates_sidecars_even_during_identification(restored, monkeypatch):
    path, _ = restored
    before = _snapshot(path)
    observations, openings = [], []
    original = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            observations.append(_snapshot(path))
            return result

    def observed(database, *args, **kwargs):
        openings.append((str(database), kwargs.get('uri', False)))
        kwargs['factory'] = ObservedConnection
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', observed)
    db = TeamDatabase(path)
    with db.connection() as conn:
        _assert_legacy(conn)
    assert observations and all(value == before for value in observations)
    assert openings and all(uri and 'mode=ro' in name and 'immutable=1' in name
                            for name, uri in openings)
    assert _snapshot(path) == before


def test_guarded_constructor_does_not_call_mkdir(restored, monkeypatch):
    path, _ = restored

    def forbidden(*args, **kwargs):
        raise AssertionError('guarded constructor attempted mkdir')

    monkeypatch.setattr(Path, 'mkdir', forbidden)
    TeamDatabase(path)


def test_guarded_historical_schema_remains_unmigrated(tmp_path):
    path = tmp_path / 'schema-one.sqlite3'
    _legacy_store(path, version=1)
    before = _snapshot(path)
    db = TeamDatabase(path)
    with db.transaction() as conn:
        _assert_legacy(conn, version=1)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='voice_request_aborts'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='team_commands_immutable_insert'").fetchone() is None
    assert _snapshot(path) == before


def test_existing_handle_rechecks_guard_before_wal(restored):
    path, old_handle = restored
    before = _snapshot(path)
    with old_handle.connection() as conn:
        _assert_legacy(conn)
        assert _snapshot(path) == before
    assert _snapshot(path) == before


@pytest.mark.parametrize('statement', [
    "UPDATE team_message_state SET state='sent'",
    'DELETE FROM team_message_state',
    "INSERT INTO team_resources(resource_key) VALUES('new-resource')",
    'CREATE TABLE injected(value TEXT)',
    'CREATE TEMP TABLE injected(value TEXT)',
    'DROP TABLE team_message_state',
    'ALTER TABLE team_message_state ADD COLUMN injected TEXT',
    "ATTACH DATABASE ':memory:' AS injected",
    'PRAGMA journal_mode=DELETE',
    'PRAGMA writable_schema=ON',
])
def test_guarded_connection_denies_dml_ddl_and_escape_without_mutation(restored, statement):
    path, db = restored
    before = _snapshot(path)
    with db.connection() as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(statement)
        _assert_legacy(conn)
        assert conn.execute('PRAGMA table_info(team_message_state)').fetchall()
    assert _snapshot(path) == before


def test_guarded_transaction_allows_diagnostics_and_rolls_back_denied_write(restored):
    path, db = restored
    before = _snapshot(path)
    with db.transaction() as conn:
        _assert_legacy(conn)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with pytest.raises(sqlite3.DatabaseError):
        with db.transaction() as conn:
            conn.execute("UPDATE team_message_state SET state='sent'")
    with db.transaction() as conn:
        _assert_legacy(conn)
    assert _snapshot(path) == before


@pytest.mark.parametrize('statement', [
    "UPDATE team_message_state SET state='sent'",
    'CREATE TABLE injected(value TEXT)',
])
def test_clearing_authorizer_cannot_make_restored_file_writable(restored, statement):
    path, db = restored
    before = _snapshot(path)
    with db.connection() as conn:
        conn.set_authorizer(None)
        # Main DML proves mode=ro independently. Structural SQL also retains
        # the mandatory diagnostic boundary after clearing caller callbacks.
        error = sqlite3.OperationalError if statement.startswith('UPDATE') else sqlite3.DatabaseError
        code = 'readonly' if statement.startswith('UPDATE') else 'not authorized'
        with pytest.raises(error, match=code):
            conn.execute(statement)
        _assert_legacy(conn)
    assert _snapshot(path) == before


def test_fresh_open_maintenance_repository_cannot_grant_old_authority(restored, tmp_path):
    from secretary.infrastructure.team_maintenance import MaintenanceRepository

    path, _ = restored
    private = tmp_path / 'new-control'
    private.mkdir()
    gate = MaintenanceRepository(private / 'control.sqlite3', str(uuid4()))
    gate.register_participant('secretary', run_id=str(uuid4()), identity={
        'pid': 123, 'creation_time': '2026-10-04T00:00:00Z',
        'executable_sha256': 'a' * 64, 'argv_sha256': 'b' * 64})
    assert gate.status()['mode'] == 'open'
    before = _snapshot(path)
    db = TeamDatabase(path, maintenance=gate, participant_id='secretary')
    with db.connection() as conn:
        _assert_legacy(conn)
        conn.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute("UPDATE team_message_state SET state='sent'")
    assert _snapshot(path) == before


def test_guard_installed_after_probe_fails_closed_before_wal_or_handle_exposure(tmp_path, monkeypatch):
    path = tmp_path / 'raced.sqlite3'
    db = _legacy_store(path, guarded=False)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
    original = maintenance_binding.restore_guard_present
    installed = []

    def race(paths):
        result = original(paths)
        assert not result
        _guard(path)
        installed.append(_snapshot(path))
        return result

    monkeypatch.setattr(maintenance_binding, 'restore_guard_present', race)
    with pytest.raises(sqlite3.DatabaseError, match='maintenance_restore_guard_changed'):
        with db.connection():
            pytest.fail('raced connection exposed a write-capable handle')
    assert installed and _snapshot(path) == installed[0]
    with closing(sqlite3.connect(maintenance_binding.readonly_authority_uri(path), uri=True)) as conn:
        _assert_legacy(conn)
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'


def test_guarded_live_wal_is_observed_without_ignoring_committed_guard(tmp_path):
    path = tmp_path / 'active.sqlite3'
    db = _legacy_store(path, guarded=False)
    with closing(sqlite3.connect(path)) as writer:
        _restore_guard(writer, str(uuid4()), 'a' * 64)
        writer.commit()
        assert Path(str(path) + '-wal').exists()
        reader = TeamDatabase(path)
        with reader.connection() as conn:
            _assert_legacy(conn)
            conn.set_authorizer(None)
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                conn.execute("UPDATE team_message_state SET state='sent'")
    with db.connection() as conn:
        _assert_legacy(conn)


def test_ordinary_new_and_existing_team_database_still_migrate_and_write(tmp_path):
    path = tmp_path / 'new-parent' / 'team # normal.sqlite3'
    db = TeamDatabase(path)
    with db.transaction() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == SCHEMA_VERSION
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        conn.execute("INSERT INTO team_resources(resource_key) VALUES('new-resource')")
    reopened = TeamDatabase(path)
    with reopened.transaction() as conn:
        conn.execute("UPDATE team_resources SET fence=1 WHERE resource_key='new-resource'")
    with db.connection() as conn:
        assert conn.execute('SELECT fence FROM team_resources').fetchone()[0] == 1


def test_foreign_wal_database_is_rejected_without_any_sidecar_or_byte_change(tmp_path):
    path = tmp_path / 'foreign.sqlite3'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE unrelated(value TEXT)')
        conn.commit()
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    before = _snapshot(path)
    with pytest.raises(ValueError, match='not_a_team_database'):
        TeamDatabase(path)
    assert _snapshot(path) == before
