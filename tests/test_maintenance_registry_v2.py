"""Versioned maintenance registry migration on disposable SQLite stores only."""
import sqlite3
from uuid import uuid4

import pytest

from secretary.infrastructure.team_maintenance import MaintenanceError, MaintenanceRepository


def test_new_maintenance_store_uses_exact_v2_ticket_commitment_registry(tmp_path):
    path = tmp_path / 'maintenance.sqlite3'
    MaintenanceRepository(path, str(uuid4()))

    with sqlite3.connect(path) as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        admission_columns = tuple(row[1] for row in conn.execute(
            'PRAGMA table_info(maintenance_ticket_commitments)'))

    assert version == 2
    assert tables == {
        'maintenance_state', 'maintenance_participants', 'maintenance_tickets',
        'maintenance_events', 'maintenance_recovery_checkpoints', 'maintenance_sources',
        'maintenance_native_containment', 'maintenance_ticket_commitments',
    }
    assert admission_columns == ('ticket_id', 'record', 'record_sha256', 'created_at')


@pytest.mark.parametrize('legacy_version', [0, 1])
def test_v1_maintenance_store_migrates_without_rewriting_lifecycle_tickets(tmp_path, legacy_version):
    path = tmp_path / f'legacy-{legacy_version}.sqlite3'
    deployment_id = str(uuid4())
    MaintenanceRepository(path, deployment_id)
    ticket_row = (str(uuid4()), 'synthetic-worker', str(uuid4()), 'outbound_bot',
                  str(uuid4()), 3, 'finished', '2026-10-12T00:00:00+00:00')
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO maintenance_tickets VALUES(?,?,?,?,?,?,?,?)', ticket_row)
        conn.execute('DROP TABLE maintenance_ticket_commitments')
        conn.execute(f'PRAGMA user_version={legacy_version}')

    MaintenanceRepository(path, deployment_id)
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        assert conn.execute('SELECT * FROM maintenance_tickets').fetchone() == ticket_row
        assert conn.execute('SELECT COUNT(*) FROM maintenance_ticket_commitments').fetchone()[0] == 0


def test_v1_stopped_runtime_reader_is_read_only_and_keeps_legacy_layout(tmp_path):
    from secretary.infrastructure.restore_runtime_evidence import _read_control

    path = tmp_path / 'legacy-reader.sqlite3'
    MaintenanceRepository(path, str(uuid4()))
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE maintenance_ticket_commitments')
        conn.execute('PRAGMA user_version=1')
    before = path.read_bytes()

    snapshot = _read_control(path, immutable=False)

    assert snapshot['_registry_version'] == 1
    assert 'maintenance_ticket_commitments' not in snapshot
    assert path.read_bytes() == before


def test_v2_schema_damage_fails_before_repository_can_recreate_table(tmp_path):
    path = tmp_path / 'damaged.sqlite3'
    deployment_id = str(uuid4())
    MaintenanceRepository(path, deployment_id)
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE maintenance_ticket_commitments')
        conn.execute('PRAGMA user_version=2')
    before = path.read_bytes()

    with pytest.raises(MaintenanceError, match='maintenance_database_invalid'):
        MaintenanceRepository(path, deployment_id)

    assert path.read_bytes() == before


def test_ticket_commitment_table_is_append_only_and_requires_existing_ticket(tmp_path):
    path = tmp_path / 'append-only.sqlite3'
    repo = MaintenanceRepository(path, str(uuid4()))
    ticket_id = str(uuid4())
    values = (ticket_id, '{}', 'a' * 64, '2026-10-12T00:00:00+00:00')
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match='maintenance_ticket_commitment_ticket_missing'):
            conn.execute('INSERT INTO maintenance_ticket_commitments VALUES(?,?,?,?)', values)
        conn.execute('INSERT INTO maintenance_tickets VALUES(?,?,?,?,?,?,?,?)',
            (ticket_id, 'synthetic-worker', str(uuid4()), 'outbound_bot', str(uuid4()),
             1, 'finished', '2026-10-12T00:00:00+00:00'))
        conn.execute('INSERT INTO maintenance_ticket_commitments VALUES(?,?,?,?)', values)
        with pytest.raises(sqlite3.IntegrityError, match='maintenance_ticket_commitment_immutable'):
            conn.execute('UPDATE maintenance_ticket_commitments SET record=? WHERE ticket_id=?', ('[]', ticket_id))
        with pytest.raises(sqlite3.IntegrityError, match='maintenance_ticket_commitment_immutable'):
            conn.execute('DELETE FROM maintenance_ticket_commitments WHERE ticket_id=?', (ticket_id,))
        with pytest.raises(sqlite3.IntegrityError, match='maintenance_ticket_commitment_immutable'):
            conn.execute('INSERT OR REPLACE INTO maintenance_ticket_commitments VALUES(?,?,?,?)', values)
    assert repo.status()['mode'] == 'open'
