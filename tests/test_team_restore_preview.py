"""Inactive restoration inventory uses actual schemas and never opens outbound."""
from contextlib import closing
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.infrastructure.team_backup import BackupError
from test_team_backup import full_fixture


NAME = 'secretary.infrastructure.team_restore_preview'
SENSITIVE = 'sensitive_restore_payload_DO_NOT_EMIT'


def module():
    assert importlib.util.find_spec(NAME), 'read-only restore reconciliation preview is missing'
    return importlib.import_module(NAME)


@pytest.fixture
def copy(tmp_path):
    with full_fixture(tmp_path) as (backup, _, service, _, sources):
        from secretary.infrastructure.database import Database
        db = Database(sources['secretary'])
        meeting = db.create_meeting('Synthetic restored meeting')
        db.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,payload)
            VALUES('legacy-job',?,'transcribe','queued',?,?,?)""",
            (meeting['id'], meeting['created_at'], meeting['created_at'], json.dumps({'text': SENSITIVE})))
        with closing(sqlite3.connect(sources['billing'])) as conn, conn:
            conn.execute('''INSERT INTO billing_charges(operation_id,payload_hash,request_hash,category,
                period,key_tag,estimated_micro,reserved_micro,status,created_ms,updated_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (str(uuid4()), 'a' * 64, 'b' * 64, 'short_voice', '2026-10', 'synthetic-key',
                 500000, 500000, 'uncertain', 1, 1))
        result = service.backup(tmp_path / 'backups' / 'full')
        assert result.full_recovery, result
        restored = backup.restore_backup(result.destination, tmp_path / 'restore', restore_root=tmp_path)
        yield SimpleNamespace(backup=result.destination, restored=restored, tmp=tmp_path)


def files(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def test_archive_validation_does_not_create_sqlite_sidecars(tmp_path):
    with full_fixture(tmp_path) as (backup, _, service, _, _):
        result = service.backup(tmp_path / 'backups' / 'full')
        assert result.full_recovery, result
        before = files(result.destination)
        backup._validate_archive(result.destination)
        assert files(result.destination) == before


def test_backup_produces_inactive_main_snapshots(tmp_path):
    with full_fixture(tmp_path) as (_, _, service, _, _):
        result = service.backup(tmp_path / 'backups' / 'full')
        assert result.full_recovery, result
        assert not any(name.endswith(('-wal', '-shm', '-journal')) for name in files(result.destination))


def test_backup_native_wal_header_does_not_create_archive_sidecars(tmp_path):
    with full_fixture(tmp_path) as (_, _, service, _, sources):
        with closing(sqlite3.connect(sources['vikunja'])) as conn:
            assert conn.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        result = service.backup(tmp_path / 'backups' / 'native-wal')
        assert result.full_recovery, result
        assert not any(name.endswith(('-wal', '-shm', '-journal')) for name in files(result.destination))


def test_preview_verifies_relocated_capture_manifest_without_changing_it(tmp_path):
    with full_fixture(tmp_path) as (backup, _, service, _, sources):
        from secretary.infrastructure.database import Database
        db = Database(sources['secretary'])
        meeting = db.create_meeting('Synthetic capture')
        folder = sources['secretary'].parent / 'audio' / meeting['id']
        folder.mkdir(parents=True)
        sound = folder / 'chunk.wav'
        sound.write_bytes(b'synthetic inactive audio bytes')
        (folder / 'capture.json').write_text(json.dumps({'chunks': [{'path': str(sound)}], 'in_progress': {}}))
        result = service.backup(tmp_path / 'backups' / 'captured')
        assert result.full_recovery, result
        restored = backup.restore_backup(result.destination, tmp_path / 'restore', restore_root=tmp_path)
        before = files(restored.destination)
        value = module().reconciliation_preview(result.destination, restored.destination)
        assert value['assets']['files'] == 3
        assert files(restored.destination) == before
        relocated = restored.asset_roots['secretary_data'] / 'audio' / meeting['id'] / 'capture.json'
        assert Path(json.loads(relocated.read_text())['chunks'][0]['path']).is_relative_to(restored.destination)


def test_preview_suppresses_unknown_state_contents(copy):
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute('UPDATE jobs SET status=?', (SENSITIVE,))
    value = module().reconciliation_preview(copy.backup, copy.restored.destination)
    assert value['inventory']['secretary']['jobs']['states'] == {'other': 1}
    assert SENSITIVE not in json.dumps(value)


def test_preview_keeps_all_files_and_liabilities_blocked(copy):
    before = files(copy.restored.destination)
    original = files(copy.backup)
    result = module().reconciliation_preview(copy.backup, copy.restored.destination)
    assert result['state'] == 'preview_blocked'
    assert result['outbound_enabled'] is False and result['activation_supported'] is False
    assert result['inventory']['secretary']['jobs']['states'] == {'queued': 1}
    assert result['inventory']['team']['team_auth_sessions']['rows'] == 0
    assert result['inventory']['team']['bot_buttons']['rows'] == 0
    assert result['billing_liabilities']['uncertain'] == {'rows': 1, 'reserved_micro': 500000, 'gross_hold_micro': 500000}
    assert set(result['sources']) == {'secretary', 'team', 'billing', 'vikunja'}
    assert all(len(item['logical_sha256']) == 64 for item in result['sources'].values())
    assert 'provider_reconciliation' in result['pending_requirements']
    assert SENSITIVE not in json.dumps(result)
    assert 'synthetic-key' not in json.dumps(result)
    assert result == module().reconciliation_preview(copy.backup, copy.restored.destination)
    assert files(copy.restored.destination) == before
    assert files(copy.backup) == original
    from secretary.infrastructure.budget_repository import assert_paid_billing_allowed
    from secretary.domain.cloud_budget import BudgetError
    with pytest.raises(BudgetError, match='maintenance_restore_blocked'):
        assert_paid_billing_allowed(copy.restored.databases['billing'])


def test_preview_binds_exact_restore_location_even_when_database_bytes_match(copy):
    preview = module()
    first = preview.reconciliation_preview(copy.backup, copy.restored.destination)
    other = copy.tmp / 'other-restore'
    shutil.copytree(copy.restored.destination, other)
    path = other / 'restore.json'
    doc = json.loads(path.read_text())
    for key in ('databases', 'asset_roots'):
        doc[key] = {name: str(other / Path(value).relative_to(copy.restored.destination))
                    for name, value in doc[key].items()}
    path.write_text(json.dumps(doc))
    second = preview.reconciliation_preview(copy.backup, other)
    assert first['sources'] == second['sources']
    assert first['preview_sha256'] != second['preview_sha256']


def test_preview_preserves_observed_cost_above_original_reservation(copy):
    with closing(sqlite3.connect(copy.restored.databases['billing'])) as conn, conn:
        conn.execute('UPDATE billing_charges SET observed_cost_micro=700000')
    result = module().reconciliation_preview(copy.backup, copy.restored.destination)
    assert result['billing_liabilities']['uncertain']['reserved_micro'] == 500000
    assert result['billing_liabilities']['uncertain']['gross_hold_micro'] == 700000


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_preview_refuses_active_sidecars_without_ignoring_them(copy, suffix):
    sidecar = Path(str(copy.restored.databases['team']) + suffix)
    sidecar.write_bytes(b'synthetic active sidecar')
    before = files(copy.restored.destination)
    with pytest.raises(BackupError, match='restore_preview_active_source'):
        module().reconciliation_preview(copy.backup, copy.restored.destination)
    assert files(copy.restored.destination) == before


@pytest.mark.parametrize('change', ['external', 'overlap', 'manifest', 'outbound', 'partial'])
def test_preview_rejects_changed_metadata_before_opening_sources(copy, change, monkeypatch):
    path = copy.restored.destination / 'restore.json'
    doc = json.loads(path.read_text())
    if change == 'external': doc['databases']['team'] = str(copy.tmp / 'external.sqlite3')
    elif change == 'overlap': doc['databases']['team'] = doc['databases']['secretary']
    elif change == 'manifest': doc['source_manifest_sha256'] = 'f' * 64
    elif change == 'outbound': doc['outbound_enabled'] = True
    else: doc['state'] = 'copying'
    path.write_text(json.dumps(doc))
    preview = module()
    monkeypatch.setattr(preview, '_source_snapshot', lambda *a, **k: pytest.fail('invalid metadata opened source'))
    before = files(copy.restored.destination)
    with pytest.raises(BackupError): preview.reconciliation_preview(copy.backup, copy.restored.destination)
    assert files(copy.restored.destination) == before


def test_preview_rejects_mismatching_guard(copy):
    with closing(sqlite3.connect(copy.restored.databases['team'])) as conn, conn:
        conn.execute('DROP TRIGGER backup_restore_no_update')
        conn.execute("UPDATE maintenance_restore_guard SET manifest_sha256=?", ('c' * 64,))
    before = files(copy.restored.destination)
    with pytest.raises(BackupError, match='restore_preview_guard_mismatch'):
        module().reconciliation_preview(copy.backup, copy.restored.destination)
    assert files(copy.restored.destination) == before


def test_preview_refuses_unsupported_schema(copy):
    with closing(sqlite3.connect(copy.restored.databases['team'])) as conn, conn:
        conn.execute('INSERT INTO team_schema VALUES(999)')
    with pytest.raises(BackupError, match='restore_preview_schema_unsupported'):
        module().reconciliation_preview(copy.backup, copy.restored.destination)


def test_preview_does_not_accept_truncated_inventory(copy, monkeypatch):
    preview = module()
    monkeypatch.setattr(preview, 'MAX_ROWS', 0)
    with pytest.raises(BackupError, match='restore_preview_inventory_bounded'):
        preview.reconciliation_preview(copy.backup, copy.restored.destination)


def test_preview_rejects_missing_restored_asset(copy):
    (copy.restored.asset_roots['vikunja_files'] / '1').unlink()
    with pytest.raises(BackupError):
        module().reconciliation_preview(copy.backup, copy.restored.destination)


def test_preview_rechecks_sources_changed_after_inventory(copy, monkeypatch):
    preview = module()
    original = preview._source_snapshot
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == 'secretary':
            with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
                conn.execute("UPDATE meetings SET title='concurrent change'")
        return result
    monkeypatch.setattr(preview, '_source_snapshot', changed)
    with pytest.raises(BackupError, match='restore_preview_input_changed'):
        preview.reconciliation_preview(copy.backup, copy.restored.destination)


@pytest.mark.parametrize('kind', ['restored_asset', 'archive_asset', 'archive_database', 'archive_sidecar'])
def test_preview_rechecks_every_member_after_last_asset_read(copy, monkeypatch, kind):
    preview = module()
    original = preview._assets
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if kind == 'restored_asset': path = copy.restored.asset_roots['vikunja_files'] / '1'
        elif kind == 'archive_asset': path = copy.backup / 'assets/vikunja_files/1'
        elif kind == 'archive_database': path = copy.backup / 'databases/vikunja.sqlite3'
        else: path = copy.backup / 'databases/vikunja.sqlite3-wal'
        path.write_bytes((path.read_bytes() if path.exists() else b'') + b'changed after read')
        return result
    monkeypatch.setattr(preview, '_assets', changed)
    with pytest.raises(BackupError, match='restore_preview_(input_changed|active_source)'):
        preview.reconciliation_preview(copy.backup, copy.restored.destination)


def test_preview_bounds_large_sqlite_cells_before_extracting_them(copy, monkeypatch):
    preview = module()
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute('UPDATE jobs SET payload=?', (json.dumps({'text': 'x' * 65536}),))
    original = preview._ro
    def guarded(*args, **kwargs):
        conn = original(*args, **kwargs)
        def row_factory(cursor, row):
            assert not any(isinstance(value, (str, bytes)) and len(value) > 4096 for value in row), 'oversize cell extracted before limit'
            return row
        conn.row_factory = row_factory
        return conn
    monkeypatch.setattr(preview, '_ro', guarded)
    monkeypatch.setattr(preview, 'MAX_CELL_BYTES', 4096, raising=False)
    with pytest.raises(BackupError, match='restore_preview_inventory_bounded'):
        preview.reconciliation_preview(copy.backup, copy.restored.destination)


def cli():
    source = Path(__file__).parents[1] / 'scripts/team/backup_restore.py'
    spec = importlib.util.spec_from_file_location('restore_preview_cli', source)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry


def test_cli_emits_only_blocked_metadata(copy, capsys):
    assert cli().main(['reconciliation-preview', '--backup', str(copy.backup),
                       '--restore', str(copy.restored.destination)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)['state'] == 'preview_blocked'
    assert SENSITIVE not in output


def test_cli_does_not_accept_apply_or_default_paths(tmp_path, capsys):
    entry = cli()
    with pytest.raises(SystemExit) as error:
        entry.main(['reconciliation-preview', '--backup', str(tmp_path / 'missing'),
                    '--restore', str(tmp_path / 'missing'), '--apply'])
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []
