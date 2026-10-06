"""Restore probes keep inactive bytes and journals; no runtime/provider startup."""
from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.infrastructure.maintenance_binding import restore_guard_present
from secretary.infrastructure.team_maintenance import MaintenanceError
from secretary.infrastructure.team_runtime_recovery import _assert_not_restored
from secretary.orchestration.team_runtime import TeamRuntime


def snapshot(root):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.iterdir() if path.is_file()}


def guard(conn):
    conn.execute('''CREATE TABLE maintenance_restore_guard(
        id INTEGER PRIMARY KEY CHECK(id=1), restore_id TEXT NOT NULL,
        reconciliation_required INTEGER NOT NULL CHECK(reconciliation_required=1),
        manifest_sha256 TEXT NOT NULL)''')
    conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)', (str(uuid4()), 'a' * 64))


def probe(kind, path):
    if kind == 'binding':
        return restore_guard_present((path,))
    if kind == 'gateway':
        runtime = object.__new__(TeamRuntime)
        runtime.settings = SimpleNamespace(restore_blocked=False,
            secretary_database_path=path, team_database_path=path.parent / 'absent-team.sqlite3',
            billing_database_path=path.parent / 'absent-billing.sqlite3',
            vikunja_database_path=path.parent / 'absent-vikunja.sqlite3')
        return runtime._restore_guard_present()
    with pytest.raises(MaintenanceError, match='^maintenance_restore_blocked$'):
        _assert_not_restored((path,))
    return True


@pytest.mark.parametrize('kind', ['binding', 'gateway', 'recovery'])
def test_inactive_wal_header_probe_never_creates_auxiliary_files(tmp_path, monkeypatch, kind):
    path = tmp_path / 'inactive.sqlite3'
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA journal_mode=WAL')
        guard(conn)
    before = snapshot(tmp_path)
    assert set(before) == {'inactive.sqlite3'}
    observed = []
    original = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            # Check while the diagnostic reader is still open: closing a reader
            # afterwards must not hide temporary WAL/SHM creation.
            observed.append(snapshot(tmp_path))
            assert observed[-1] == before, 'restore probe created auxiliary files during diagnostic read'
            return result

    def connect(database, *args, **kwargs):
        kwargs['factory'] = ObservedConnection
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    assert probe(kind, path)
    assert observed and snapshot(tmp_path) == before


@pytest.mark.parametrize('kind', ['binding', 'gateway', 'recovery'])
def test_guard_in_live_wal_is_read_instead_of_ignoring_journal(tmp_path, kind):
    path = tmp_path / 'journaled.sqlite3'
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE synthetic_evidence(value TEXT)')
        writer.execute("INSERT INTO synthetic_evidence VALUES('retain')")
        writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        guard(writer)
        assert Path(str(path) + '-wal').stat().st_size > 0
        before = snapshot(tmp_path)
        assert probe(kind, path)
        after = snapshot(tmp_path)
        # Ordinary SQLite journal-aware readers may update volatile SHM reader
        # marks. The main DB and actual WAL must remain byte-identical, all
        # files stay present, and no persistent mutation or checkpoint occurs.
        assert set(after) == set(before)
        assert {k: v for k, v in after.items() if not k.endswith('-shm')} == {
            k: v for k, v in before.items() if not k.endswith('-shm')}
        assert writer.execute('SELECT reconciliation_required FROM maintenance_restore_guard').fetchone() == (1,)


@pytest.mark.parametrize('kind', ['binding', 'gateway', 'recovery'])
def test_missing_probe_paths_are_not_created(tmp_path, kind):
    path = tmp_path / 'absent.sqlite3'
    if kind == 'recovery':
        _assert_not_restored((path,))
    else:
        assert not probe(kind, path)
    assert snapshot(tmp_path) == {}


def test_explicit_restore_flag_refuses_without_sql_open(tmp_path, monkeypatch):
    runtime = object.__new__(TeamRuntime)
    runtime.settings = SimpleNamespace(restore_blocked=True)
    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: pytest.fail('flag denial opened SQLite'))
    assert runtime._restore_guard_present()
    assert snapshot(tmp_path) == {}
