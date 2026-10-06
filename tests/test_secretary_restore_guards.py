"""Restored Secretary stores expose diagnostics, never legacy work or writes."""
import asyncio
from contextlib import closing
import hashlib
import sqlite3
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.api import create_app
from secretary.application.worker import Worker
from secretary.infrastructure.database import Database
from secretary.infrastructure.team_backup import _restore_guard
from secretary.settings import Settings
from secretary.team_settings import RuntimeConfigurationError


@pytest.fixture
def restored(tmp_path):
    path = tmp_path / 'secretary.sqlite3'
    db = Database(path)
    meeting = db.create_meeting('Synthetic restore')
    stamp = meeting['created_at']
    db.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at)
        VALUES('old-job',?,'transcribe','queued',?,?)""", (meeting['id'], stamp, stamp))
    with closing(sqlite3.connect(path)) as conn, conn:
        _restore_guard(conn, str(uuid4()), 'a' * 64)
    return SimpleNamespace(path=path, db=db, meeting=meeting)


@pytest.mark.parametrize('statement', [
    "UPDATE jobs SET status='running' WHERE id='old-job'",
    'DELETE FROM jobs',
    "INSERT INTO configuration VALUES('restored-bypass','true')",
    'CREATE TABLE bypass(value TEXT)',
    'DROP TABLE jobs',
    'PRAGMA user_version=777',
    "ATTACH DATABASE ':memory:' AS bypass",
])
def test_existing_database_handle_cannot_write_after_restore(restored, statement):
    # The object predates the guard: decisions must be made for each connection.
    with restored.db.connection() as conn:
        assert conn.execute('SELECT status FROM jobs').fetchone()[0] == 'queued'
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(statement)
    with sqlite3.connect(restored.path) as conn:
        assert conn.execute('SELECT status,attempts FROM jobs').fetchone() == ('queued', 0)
        assert conn.execute('PRAGMA user_version').fetchone() == (0,)
        assert conn.execute('SELECT count(*) FROM configuration').fetchone() == (0,)


def test_reopening_restore_preserves_bytes_and_provides_diagnostics(restored):
    before = hashlib.sha256(restored.path.read_bytes()).hexdigest()
    reopened = Database(restored.path)
    assert reopened.meeting(restored.meeting['id'])['title'] == 'Synthetic restore'
    assert reopened.job('old-job')['status'] == 'queued'
    assert hashlib.sha256(restored.path.read_bytes()).hexdigest() == before
    with pytest.raises(sqlite3.DatabaseError):
        reopened.claim()
    with pytest.raises(sqlite3.DatabaseError):
        reopened.recover()
    assert reopened.job('old-job')['attempts'] == 0


def test_restore_sqlite_handle_itself_is_read_only(restored):
    with restored.db.connection() as conn:
        # Even a caller changing its callback cannot turn diagnostics into a
        # write-capable SQLite handle. ATTACH is covered separately above.
        conn.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute("UPDATE jobs SET status='running' WHERE id='old-job'")
    assert restored.db.job('old-job')['status'] == 'queued'


def test_restored_diagnostics_never_open_a_write_capable_sqlite_handle(restored, monkeypatch):
    original = sqlite3.connect
    opened = []
    def connect(database, *args, **kwargs):
        value = str(database)
        if value == str(restored.path) or value.startswith(restored.path.as_uri()):
            assert 'mode=ro' in value, 'restored authority opened for writing before guard'
            opened.append(value)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    reopened = Database(restored.path)
    assert reopened.job('old-job')['status'] == 'queued'
    assert opened


def test_inactive_restored_diagnostics_do_not_create_sidecars(restored):
    def files():
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in restored.path.parent.iterdir() if path.is_file()}
    before = files()
    assert not any(name.endswith(('-wal', '-shm', '-journal')) for name in before)
    reopened = Database(restored.path)
    assert reopened.job('old-job')['status'] == 'queued'
    assert files() == before


def test_recovery_worker_refuses_restore_before_provider(restored, tmp_path):
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / 'runtime', cloud_enabled=False)
    calls = []
    def provider(*args):
        calls.append(args)
        pytest.fail('restored worker dispatched provider')
    with pytest.raises(sqlite3.DatabaseError):
        Worker(Database(restored.path), settings, provider)
    worker = Worker(Database(restored.path), settings, provider, recovery=False)
    with pytest.raises(sqlite3.DatabaseError):
        asyncio.run(worker.run_once())
    assert calls == []
    assert restored.db.job('old-job')['status'] == 'queued'


@pytest.mark.parametrize('outbound_enabled,run_worker', [(True, True), (False, False)])
def test_legacy_app_refuses_restore_before_factories_or_local_side_effects(
        restored, tmp_path, monkeypatch, outbound_enabled, run_worker):
    data = tmp_path / 'uncreated-runtime'
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=data, cloud_enabled=False)
    prior_tempdir = tempfile.tempdir
    def forbidden(*args, **kwargs):
        pytest.fail('restore opened an app dependency')
    monkeypatch.setattr('secretary.api.Database', forbidden)
    with pytest.raises(RuntimeConfigurationError, match='team_restore_reconciliation_required'):
        create_app(settings, database_path=restored.path, provider_factory=forbidden,
            capture=SimpleNamespace(close=lambda: None), task_publications_factory=forbidden,
            team_auth_factory=forbidden, publication_worker_factory=forbidden,
            outbound_enabled=outbound_enabled, run_worker=run_worker)
    assert not data.exists()
    assert tempfile.tempdir == prior_tempdir
    assert restored.db.job('old-job')['status'] == 'queued'


@pytest.mark.parametrize('explicit_billing', [False, True])
def test_legacy_app_checks_effective_billing_before_creating_secretary(tmp_path, monkeypatch, explicit_billing):
    from secretary.infrastructure.budget_repository import BudgetRepository
    data = tmp_path / 'data'
    data.mkdir()
    path = (tmp_path / 'explicit-billing.sqlite3') if explicit_billing else (data / 'billing.sqlite3')
    BudgetRepository(path)
    with sqlite3.connect(path) as conn:
        _restore_guard(conn, str(uuid4()), 'a' * 64)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=data, cloud_enabled=False)
    prior_tempdir = tempfile.tempdir
    def forbidden(*args, **kwargs):
        pytest.fail('restored Billing reached Secretary factory')
    monkeypatch.setattr('secretary.api.Database', forbidden)
    with pytest.raises(RuntimeConfigurationError, match='team_restore_reconciliation_required'):
        create_app(settings, billing_path=path if explicit_billing else None, run_worker=False,
                   outbound_enabled=False)
    assert not (data / 'secretary.sqlite3').exists()
    assert not (data / 'tmp').exists()
    assert tempfile.tempdir == prior_tempdir


def test_secretary_team_runtime_checks_vikunja_guard_before_control_store(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from secretary.orchestration.secretary_team_runtime import create_secretary_team_app

    native = tmp_path / 'native' / 'vikunja.sqlite3'
    native.parent.mkdir()
    with sqlite3.connect(native) as conn:
        conn.execute('CREATE TABLE files(id INTEGER PRIMARY KEY)')
        conn.execute('''CREATE TABLE maintenance_restore_guard(
            id INTEGER PRIMARY KEY CHECK(id=1), restore_id TEXT NOT NULL,
            reconciliation_required INTEGER NOT NULL CHECK(reconciliation_required=1),
            manifest_sha256 TEXT NOT NULL)''')
        conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,1,?)',
                     (str(uuid4()), 'a' * 64))

    config = SimpleNamespace(restore_blocked=False,
        secretary_database_path=tmp_path / 'secretary.sqlite3',
        team_database_path=tmp_path / 'team.sqlite3',
        billing_database_path=tmp_path / 'billing.sqlite3',
        vikunja_database_path=native,
        control_database_path=tmp_path / 'control' / 'maintenance.sqlite3')
    def forbidden(*args, **kwargs):
        pytest.fail('restore reached control-store creation')
    monkeypatch.setattr('secretary.infrastructure.team_maintenance.MaintenanceRepository', forbidden)
    with pytest.raises(RuntimeConfigurationError, match='team_restore_reconciliation_required'):
        create_secretary_team_app(config)
    assert not config.control_database_path.exists()


def test_legacy_app_cannot_bypass_guard_with_relative_database_path(restored, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / 'uncreated', cloud_enabled=False)
    with pytest.raises(RuntimeConfigurationError, match='team_restore_reconciliation_required'):
        create_app(settings, database_path=restored.path.name, run_worker=False)
    assert not settings.data_dir.exists()
