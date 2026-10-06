"""R4 prerequisite: restored Billing authority is diagnostic SQL only."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.domain.cloud_budget import BudgetError
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.team_backup import _restore_guard
from secretary.infrastructure.team_maintenance import MaintenanceRepository


SCRATCH = Path(__file__).resolve().parents[1] / '.runtime/team-rollout/r4-sql-opening-20261004/billing'


def fingerprint(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob('*') if path.is_file()}


@pytest.fixture
def restored():
    directory = SCRATCH / uuid4().hex
    path = directory / 'authority' / 'billing.sqlite3'
    repository = BudgetRepository(path)
    with repository.transaction() as conn:
        conn.execute("INSERT INTO billing_state VALUES('history-marker','preserve')")
        for ordinal, status in enumerate(('reserved', 'submitted', 'uncertain'), 1):
            conn.execute('''INSERT INTO billing_charges(operation_id,payload_hash,request_hash,
                category,period,key_tag,estimated_micro,reserved_micro,observed_cost_micro,
                status,provider_job_id,created_ms,updated_ms)
                VALUES(?,?,?,'meeting_stt','2026-10','synthetic-key',2000000,3000000,?,?,?,?,?)''',
                ('legacy-' + status, 'a' * 64, str(ordinal) * 64,
                 4000000 if status == 'uncertain' else None, status,
                 'synthetic-job-' + status if status != 'reserved' else None, ordinal, ordinal))
        conn.execute("INSERT INTO billing_scopes VALUES('old-scope','scope-operation',9000000,1)")
        conn.execute("INSERT INTO billing_meeting_scopes VALUES('old-meeting','old-scope')")
    with closing(sqlite3.connect(path)) as conn, conn:
        _restore_guard(conn, str(uuid4()), 'a' * 64)
    before = fingerprint(path.parent)
    assert set(before) == {'billing.sqlite3'}
    return SimpleNamespace(directory=directory, path=path, repository=repository, before=before)


def assert_history(conn):
    assert conn.execute('SELECT version FROM billing_schema').fetchone()[0] == 3
    assert [tuple(row) for row in conn.execute('''SELECT status,reserved_micro,observed_cost_micro,confirmed_micro
        FROM billing_charges ORDER BY created_ms''')] == [
            ('reserved', 3000000, None, None), ('submitted', 3000000, None, None), ('uncertain', 3000000, 4000000, None)]
    assert tuple(conn.execute('SELECT reconciliation_required,manifest_sha256 FROM maintenance_restore_guard').fetchone()) == (1, 'a' * 64)
    assert conn.execute('SELECT value FROM billing_state').fetchone()[0] == 'preserve'


def test_guarded_constructor_and_diagnostics_preserve_all_bytes_and_sidecars(restored, monkeypatch):
    original = sqlite3.connect
    statements = []

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            statements.append(sql)
            result = super().execute(sql, *args, **kwargs)
            # Inspect while handles remain open: transient WAL/SHM also fail.
            assert fingerprint(restored.path.parent) == restored.before
            return result

    def connect(database, *args, **kwargs):
        kwargs['factory'] = ObservedConnection
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    reopened = BudgetRepository(restored.path)
    with reopened.transaction() as conn:
        assert_history(conn)
    assert fingerprint(restored.path.parent) == restored.before
    forbidden = ('CREATE ', 'ALTER ', 'INSERT ', 'UPDATE ', 'DELETE ', 'DROP ', 'PRAGMA journal_mode', 'BEGIN IMMEDIATE')
    assert not any(sql.upper().startswith(tuple(item.upper() for item in forbidden)) for sql in statements)


def test_guarded_constructor_and_each_transaction_open_only_readonly_authority(restored, monkeypatch):
    original = sqlite3.connect
    opened = []

    def connect(database, *args, **kwargs):
        opened.append(str(database))
        assert kwargs.get('uri') is True and 'mode=ro' in str(database)
        assert 'immutable=1' in str(database)
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    reopened = BudgetRepository(restored.path)
    for repository in (restored.repository, reopened):
        with repository.transaction() as conn:
            assert_history(conn)
    assert len(opened) >= 3
    assert fingerprint(restored.path.parent) == restored.before


@pytest.mark.parametrize('statement', [
    "UPDATE billing_charges SET status='submitted' WHERE operation_id='legacy-reserved'",
    "UPDATE billing_charges SET status='confirmed',reserved_micro=0,confirmed_micro=1",
    'DELETE FROM billing_charges',
    "UPDATE billing_state SET value='changed'",
    'DELETE FROM billing_scopes',
    'DELETE FROM maintenance_restore_guard',
    "INSERT OR REPLACE INTO maintenance_restore_guard VALUES(1,'replacement',1,'fake')",
    'CREATE TABLE bypass(value TEXT)',
    'DROP TABLE maintenance_restore_guard',
    'PRAGMA user_version=99',
    "ATTACH DATABASE ':memory:' AS bypass",
])
def test_guarded_transactions_deny_mutation_with_budget_error(restored, statement):
    with pytest.raises(BudgetError, match='^maintenance_restore_blocked$') as failure:
        with restored.repository.transaction() as conn:
            conn.execute(statement)
    assert failure.value.code == 'maintenance_restore_blocked'
    assert fingerprint(restored.path.parent) == restored.before
    with restored.repository.transaction() as conn:
        assert_history(conn)


@pytest.mark.parametrize('statement', [
    "UPDATE billing_charges SET status='confirmed',reserved_micro=0,confirmed_micro=1",
    'DELETE FROM maintenance_restore_guard',
    'CREATE TABLE bypass(value TEXT)',
])
def test_clearing_connection_authorizer_cannot_enable_authority_writes(restored, statement):
    with pytest.raises(BudgetError, match='^maintenance_restore_blocked$'):
        with restored.repository.transaction() as conn:
            conn.set_authorizer(None)
            conn.execute(statement)
    assert fingerprint(restored.path.parent) == restored.before


def test_new_open_maintenance_does_not_activate_restored_authority(restored):
    maintenance = MaintenanceRepository(restored.directory / 'new-control.sqlite3', str(uuid4()),
        clock=lambda: datetime(2026, 10, 4, 12, tzinfo=timezone.utc))
    maintenance.register_participant('diagnostic', run_id=str(uuid4()), identity={
        'pid': 123, 'creation_time': 'synthetic', 'executable_sha256': 'b' * 64, 'argv_sha256': 'c' * 64})
    assert maintenance.status()['mode'] == 'open'
    reopened = BudgetRepository(restored.path, maintenance=maintenance, participant_id='diagnostic')
    with reopened.transaction() as conn:
        assert_history(conn)
    with pytest.raises(BudgetError, match='^maintenance_restore_blocked$'):
        with reopened.transaction() as conn:
            conn.execute("UPDATE billing_charges SET status='submitted' WHERE operation_id='legacy-reserved'")
    with pytest.raises(BudgetError, match='^maintenance_restore_blocked$'):
        reopened.assert_paid_allowed()
    assert fingerprint(restored.path.parent) == restored.before


def test_paid_guard_probe_preserves_bytes_without_transient_sidecars(restored, monkeypatch):
    original = sqlite3.connect

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            assert fingerprint(restored.path.parent) == restored.before
            return result

    def connect(database, *args, **kwargs):
        kwargs['factory'] = ObservedConnection
        return original(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with pytest.raises(BudgetError, match='^maintenance_restore_blocked$'):
        restored.repository.assert_paid_allowed()
    assert fingerprint(restored.path.parent) == restored.before


@pytest.mark.parametrize('version', [1, 2])
def test_ordinary_billing_migrations_and_transactions_still_work(version):
    path = SCRATCH / uuid4().hex / 'ordinary' / 'billing.sqlite3'
    repository = BudgetRepository(path)
    with repository.transaction() as conn:
        conn.execute('UPDATE billing_schema SET version=?', (version,))
        conn.execute('ALTER TABLE billing_accounts DROP COLUMN opening_request_watermark')
        conn.execute('ALTER TABLE billing_accounts DROP COLUMN latest_request_watermark')
        conn.execute('ALTER TABLE billing_charges DROP COLUMN scope_id')
        conn.execute("INSERT INTO billing_state VALUES('retained','yes')")
    reopened = BudgetRepository(path)
    with reopened.transaction() as conn:
        assert conn.execute('SELECT version FROM billing_schema').fetchone()[0] == 3
        assert {'opening_request_watermark', 'latest_request_watermark'} <= {
            row[1] for row in conn.execute('PRAGMA table_info(billing_accounts)')}
        assert 'scope_id' in {row[1] for row in conn.execute('PRAGMA table_info(billing_charges)')}
        conn.execute("UPDATE billing_state SET value='still-writable'")
    with reopened.transaction() as conn:
        assert conn.execute('SELECT value FROM billing_state').fetchone()[0] == 'still-writable'
    reopened.assert_paid_allowed()
