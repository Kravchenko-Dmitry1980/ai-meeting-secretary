"""Actual isolated R3->R4 source commits; operator/runtime ports are synthetic."""
from contextlib import closing
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from test_team_restore_preview import files, SENSITIVE
from test_team_backup import full_fixture
from test_restore_reconciliation import setup, sql
from test_restore_activation_context import prepared, read, observed_files as r3_observed_files
from test_restore_activation_staging import stage, ledger_path


@pytest.fixture
def copy(tmp_path, monkeypatch):
    # Full registered-catalogue synthetic source. The old R3 fixture has one
    # files table only and rightly fails the closed 42-table native reader.
    # This tests SQL orchestration, not native-binary compatibility.
    from types import SimpleNamespace
    from uuid import uuid4
    from secretary.infrastructure.database import Database
    from secretary.infrastructure.restore_native_history import PINNED_REGISTERED_TABLES
    import subprocess
    original_init = subprocess.Popen.__init__
    def catalogue_writer(self, args, *positional, **kwargs):
        # Keep the existing owned-child/lifecycle fixture and offline guard.
        # Its extra instrumentation table would correctly be unsupported by
        # the pinned native catalogue; use one registered synthetic table.
        if (isinstance(args, list) and len(args) == 6 and args[3] == '-c'
                and args[-1] == str(tmp_path / 'vikunja.sqlite3')
                and 'lifecycle_fixture_ticks' in args[4]):
            args = [*args]
            args[4] = args[4].replace('lifecycle_fixture_ticks', 'notifications')
        original_init(self, args, *positional, **kwargs)
    monkeypatch.setattr(subprocess.Popen, '__init__', catalogue_writer)
    with full_fixture(tmp_path) as (backup, _, service, _, sources):
        with closing(sqlite3.connect(sources['vikunja'])) as conn, conn:
            for table in PINNED_REGISTERED_TABLES:
                if table in ('files', 'notifications'): continue
                columns = 'id TEXT PRIMARY KEY' if table == 'migration' else 'id INTEGER PRIMARY KEY, value TEXT'
                conn.execute(f'CREATE TABLE "{table}" ({columns})')
            conn.executemany('INSERT INTO migration VALUES(?)', [('SCHEMA_INIT',), ('20260928140648',)])
        db = Database(sources['secretary'])
        meeting = db.create_meeting('Synthetic restored meeting')
        db.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,payload)
            VALUES('legacy-job',?,'transcribe','queued',?,?,?)""",
            (meeting['id'], meeting['created_at'], meeting['created_at'], json.dumps({'text': SENSITIVE})))
        with closing(sqlite3.connect(sources['billing'])) as conn, conn:
            conn.execute('''INSERT INTO billing_charges(operation_id,payload_hash,request_hash,category,
                period,key_tag,estimated_micro,reserved_micro,status,created_ms,updated_ms)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (str(uuid4()), 'a'*64, 'b'*64, 'short_voice', '2026-10', 'synthetic-key',
                 500000, 500000, 'uncertain', 1, 1))
        result = service.backup(tmp_path / 'backups' / 'full')
        assert result.full_recovery
        restored = backup.restore_backup(result.destination, tmp_path / 'restore', restore_root=tmp_path)
        yield SimpleNamespace(backup=result.destination, restored=restored, tmp=tmp_path)


def module():
    name = 'secretary.infrastructure.restore_source_epoch_writer'
    assert importlib.util.find_spec(name), 'actual four-source epoch writer is missing'
    return importlib.import_module(name)


@pytest.fixture
def staged(prepared, monkeypatch):
    stage(prepared, capabilities=('fresh_work', 'native_runtime', 'sql_write'))
    sample, collector, _, proof = prepared
    target = module()
    monkeypatch.setattr(target, 'OperatorAuthority', collector.OperatorAuthority)
    monkeypatch.setattr(target, 'protected_scope', collector.protected_scope)
    monkeypatch.setattr(target, 'verify_stopped_runtime', lambda **kwargs: dict(proof))
    return prepared, target


def perform(staged, **changes):
    prepared, target = staged
    sample = prepared[0]
    args = dict(project_root=sample.tmp, backup_dir=sample.backup,
        restore_dir=sample.restored.destination,
        maintenance_path=sample.tmp / 'original-control.sqlite3',
        lifecycle_path=sample.tmp / 'original-lifecycle.json',
        writer=lambda value: None)
    args.update(changes)
    return target.prepare_source_epochs(**args)


def companion(sample):
    # The fixed location is derived from the actual immutable ledger. Plain
    # Windows rglob cannot observe a descendant beyond MAX_PATH in this host.
    epoch = module()._locator(ledger_path(sample)).snapshot()['epoch']['epoch_id']
    return ledger_path(sample).parent / 'source-epochs' / epoch / 'source-epochs.sqlite3'


def io_path(path):
    from secretary.infrastructure.restore_operator import _win32_path
    return Path(_win32_path(path))


def observed_files(sample):
    result = r3_observed_files(sample)
    # Include every independent companion byte, even at >330 characters.
    # This is test-only spelling under the exact synthetic fixture root.
    result.update({'operator/' + key: value for key, value in
                   files(io_path(sample.tmp / '.runtime/team-operator')).items()})
    return result


def test_four_actual_source_preparations_preserve_history_and_stay_blocked(staged):
    prepared, _ = staged
    sample, _, r3_decision, _ = prepared
    original_archive = files(sample.backup)
    old_ledger = ledger_path(sample).read_bytes()
    old_r3 = next((sample.tmp / '.runtime/team-operator/restores').rglob('control.sqlite3')).read_bytes()
    before = {role: path.read_bytes() for role, path in sample.restored.databases.items()}
    charge = sql(sample.restored.databases['billing'], 'SELECT * FROM billing_charges')
    r2 = {role: sql(sample.restored.databases[role], 'SELECT * FROM restore_quarantine_sets')
          for role in ('secretary', 'team', 'billing')}
    result = perform(staged)
    assert result['state'] == 'all_prepared_blocked'
    assert result['activation_supported'] is False and result['outbound_enabled'] is False
    assert set(result['preparations']) == {'secretary', 'team', 'billing', 'vikunja'}
    for role, path in sample.restored.databases.items():
        assert path.read_bytes() != before[role]
        assert result['preparations'][role]['file_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        if role != 'vikunja':
            assert sql(path, 'SELECT reconciliation_required FROM maintenance_restore_guard') == [(1,)]
            assert sql(path, 'SELECT * FROM restore_quarantine_sets') == r2[role]
    assert sql(sample.restored.databases['billing'], 'SELECT * FROM billing_charges') == charge
    assert files(sample.backup) == original_archive
    assert ledger_path(sample).read_bytes() == old_ledger
    assert next((sample.tmp / '.runtime/team-operator/restores').rglob('control.sqlite3')).read_bytes() == old_r3
    head = result['preparations']['vikunja']['native_head']
    assert head['sequence'] == 0 and head['nonce'] is None
    assert head['epoch_id'] == result['binding']['epoch_id']
    assert result['binding']['context']['r3_decision_sha256'] == r3_decision['decision_sha256']
    assert SENSITIVE not in json.dumps(result) and 'synthetic-key' not in json.dumps(result)


def test_repeat_keeps_proposal_epoch_generation_receipts_and_source_bytes(staged):
    prepared, _ = staged
    first = perform(staged)
    before = files(prepared[0].restored.destination)
    second = perform(staged)
    for key in ('preparation_id', 'binding', 'binding_sha256', 'preparations', 'state'):
        assert first[key] == second[key]
    assert files(prepared[0].restored.destination) == before


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_crash_after_local_commit_before_record_resumes_same_proposal(staged, monkeypatch, role):
    _, target = staged
    original = target.SourcePreparationControl.record_preparation
    def crash(self, observed_role, *args, **kwargs):
        if observed_role == role:
            raise RuntimeError('synthetic crash after source commit before independent record')
        return original(self, observed_role, *args, **kwargs)
    monkeypatch.setattr(target.SourcePreparationControl, 'record_preparation', crash)
    with pytest.raises(RuntimeError): perform(staged)
    before = target.SourcePreparationControl.open_existing(companion(staged[0][0])).snapshot()
    assert role not in before['preparations']
    monkeypatch.setattr(target.SourcePreparationControl, 'record_preparation', original)
    result = perform(staged)
    assert result['preparation_id'] == before['preparation_id']
    assert result['binding'] == before['binding'] and result['state'] == 'all_prepared_blocked'


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_crash_before_source_commit_rolls_back_only_current_preparation(staged, monkeypatch, role):
    prepared, target = staged
    sample = prepared[0]
    before = {name: path.read_bytes() for name, path in sample.restored.databases.items()}
    if role == 'vikunja':
        original = target.prepare_native_source_in_connection
        def crash(conn, **kwargs):
            result = original(conn, **kwargs)
            assert conn.in_transaction and conn.total_changes > 0
            raise RuntimeError('synthetic crash before native source commit')
        monkeypatch.setattr(target, 'prepare_native_source_in_connection', crash)
    else:
        original = target.read_source_epoch
        def crash(conn, **kwargs):
            result = original(conn, **kwargs)
            if (kwargs['binding'].role == role and result is not None
                    and conn.in_transaction and conn.total_changes > 0):
                raise RuntimeError('synthetic crash before SQL source commit')
            return result
        monkeypatch.setattr(target, 'read_source_epoch', crash)
    with pytest.raises(RuntimeError): perform(staged)
    snapshot = target.SourcePreparationControl.open_existing(companion(sample)).snapshot()
    from secretary.domain.restore_quarantine import SOURCE_ROLES
    index = SOURCE_ROLES.index(role)
    assert set(snapshot['preparations']) == set(SOURCE_ROLES[:index])
    for name in SOURCE_ROLES[index:]:
        assert sample.restored.databases[name].read_bytes() == before[name]
    if role == 'vikunja':
        monkeypatch.setattr(target, 'prepare_native_source_in_connection', original)
    else:
        monkeypatch.setattr(target, 'read_source_epoch', original)
    result = perform(staged)
    assert result['preparation_id'] == snapshot['preparation_id']
    assert result['state'] == 'all_prepared_blocked'


@pytest.mark.parametrize('store', ['missing', 'empty', 'corrupt'])
def test_existing_epoch_directory_never_recreates_invalid_companion(staged, store):
    prepared, target = staged
    sample = prepared[0]
    epoch = target._locator(ledger_path(sample)).snapshot()['epoch']['epoch_id']
    base = ledger_path(sample).parent / 'source-epochs'
    directory = base / epoch
    target.create_private_directory(base)
    target.create_private_directory(directory)
    path = directory / 'source-epochs.sqlite3'
    if store != 'missing':
        io_path(path).write_bytes(b'' if store == 'empty' else b'synthetic invalid companion')
    before = observed_files(sample)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    assert observed_files(sample) == before


def test_epoch_directory_creation_race_does_not_adopt_store_or_write_sources(staged, monkeypatch):
    prepared, target = staged
    sample = prepared[0]
    epoch = target._locator(ledger_path(sample)).snapshot()['epoch']['epoch_id']
    original = target.create_private_directory
    def concurrent_create(path):
        if path.name == epoch:
            original(path)  # Another creator has won before our exclusive create.
        return original(path)
    monkeypatch.setattr(target, 'create_private_directory', concurrent_create)
    before = files(sample.restored.destination)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    directory = ledger_path(sample).parent / 'source-epochs' / epoch
    assert io_path(directory).is_dir() and not io_path(directory / 'source-epochs.sqlite3').exists()
    assert files(sample.restored.destination) == before


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_recorded_source_rollback_to_original_baseline_is_refused(staged, role):
    sample = staged[0][0]
    baseline = sample.restored.databases[role].read_bytes()
    perform(staged)
    sample.restored.databases[role].write_bytes(baseline)
    before = observed_files(sample)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    assert observed_files(sample) == before


def test_lost_companion_is_not_recreated_after_preparation(staged):
    sample = staged[0][0]
    perform(staged)
    path = companion(sample)
    io_path(path).unlink()
    before = observed_files(sample)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    assert not io_path(path).exists() and observed_files(sample) == before


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_foreign_writer_blocks_all_sources_before_any_epoch_metadata(staged, role):
    sample = staged[0][0]
    before = files(sample.restored.destination)
    with closing(sqlite3.connect(sample.restored.databases[role], isolation_level=None)) as conn:
        conn.execute('BEGIN EXCLUSIVE')
        with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
        conn.rollback()
    assert files(sample.restored.destination) == before


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_source_journal_is_preserved_and_refused(staged, suffix):
    sample = staged[0][0]
    path = Path(str(sample.restored.databases['team']) + suffix)
    path.write_bytes(b'synthetic active journal')
    before = observed_files(sample)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    assert observed_files(sample) == before


def test_missing_fresh_operator_approval_prevents_all_source_writes(staged, monkeypatch):
    prepared, target = staged
    sample = prepared[0]
    old = target.OperatorAuthority
    class RefusingOperator(old):
        def issue(self, *args, **kwargs): raise ValueError(SENSITIVE)
    monkeypatch.setattr(target, 'OperatorAuthority', RefusingOperator)
    before = files(sample.restored.destination)
    with pytest.raises(ValueError, match='maintenance_restore_blocked') as result: perform(staged)
    assert SENSITIVE not in repr(result.value)
    assert files(sample.restored.destination) == before


@pytest.mark.parametrize('phase', ['issue', 'before_commit'])
def test_operator_enrollment_rotation_is_refused_before_any_source_commit(staged, monkeypatch, phase):
    prepared, target = staged
    sample = prepared[0]
    operator = target.OperatorAuthority
    rotations = []
    if phase == 'issue':
        original = operator.issue
        def rotate(self, *args, **kwargs):
            monkeypatch.setattr(operator, 'operator_digest', 'd' * 64)
            rotations.append(True)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(operator, 'issue', rotate)
    else:
        original = operator.authenticate
        calls = []
        def rotate(self, *args, **kwargs):
            calls.append(True)
            if len(calls) == 3:  # Consume, prewrite, then immediately precommit.
                monkeypatch.setattr(operator, 'operator_digest', 'd' * 64)
                rotations.append(True)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(operator, 'authenticate', rotate)
    before = files(sample.restored.destination)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    assert rotations, 'Must exercise the changed current operator, not an earlier refusal'
    assert files(sample.restored.destination) == before


@pytest.mark.parametrize('role', ['secretary', 'team', 'billing', 'vikunja'])
def test_operator_rotation_during_durable_readback_prevents_independent_append(staged, monkeypatch, role):
    prepared, target = staged
    sample = prepared[0]
    original = target._PreparedSourceObserver.inspect
    rotations = []
    def rotate(self, conn, observed_role, *args):
        result = original(self, conn, observed_role, *args)
        if observed_role == role and result is not None and conn.total_changes == 0 and not rotations:
            monkeypatch.setattr(target.OperatorAuthority, 'operator_digest', 'e' * 64)
            rotations.append(True)
        return result
    monkeypatch.setattr(target._PreparedSourceObserver, 'inspect', rotate)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    assert rotations, 'Must reach the actual durable readback boundary'
    snapshot = target.SourcePreparationControl.open_existing(companion(sample)).snapshot()
    from secretary.domain.restore_quarantine import SOURCE_ROLES
    assert set(snapshot['preparations']) == set(SOURCE_ROLES[:SOURCE_ROLES.index(role)])
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False


def test_current_runtime_change_after_source_commit_keeps_all_blocked(staged, monkeypatch):
    prepared, target = staged
    sample, _, _, proof = prepared
    original = target.SourcePreparationControl.record_preparation
    def changed(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        monkeypatch.setattr(target, 'verify_stopped_runtime', lambda **kwargs: dict(proof, evidence_sha256='c'*64))
        return value
    monkeypatch.setattr(target.SourcePreparationControl, 'record_preparation', changed)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    for role in ('secretary', 'team', 'billing'):
        assert sql(sample.restored.databases[role], 'SELECT reconciliation_required FROM maintenance_restore_guard') == [(1,)]


def test_native_business_and_fake_grant_stay_denied_outside_writer_custody(staged):
    sample = staged[0][0]
    perform(staged)
    from secretary.infrastructure.restore_native_preparation import GRANT_TABLE
    with closing(sqlite3.connect(sample.restored.databases['vikunja'])) as conn:
        with pytest.raises(sqlite3.IntegrityError): conn.execute('UPDATE files SET size=size+1')
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError): conn.execute(f'INSERT INTO "{GRANT_TABLE}" DEFAULT VALUES')
        conn.rollback()


def test_original_baseline_collector_remains_strict_after_source_preparation(staged):
    prepared, _ = staged
    perform(staged)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): read(prepared)


def test_unknown_source_epoch_object_is_not_hidden_by_projection(staged):
    sample = staged[0][0]
    perform(staged)
    sql(sample.restored.databases['team'], 'CREATE TABLE restore_source_epoch_unknown(value INTEGER)')
    before = observed_files(sample)
    with pytest.raises(ValueError, match='maintenance_restore_blocked'): perform(staged)
    assert observed_files(sample) == before


def test_metadata_authorizer_allows_only_temp_catalog_inspection():
    """The native closed reader must inspect TEMP before trusting main SQL."""
    with closing(sqlite3.connect(':memory:')) as conn:
        observed = []
        policy = module()._metadata_authorizer(())
        def authorize(*event):
            observed.append(event)
            return policy(*event)
        conn.set_authorizer(authorize)
        try:
            assert conn.execute('SELECT 1 FROM temp.sqlite_master LIMIT 1').fetchone() is None
        except sqlite3.DatabaseError:
            pytest.fail(f'Native TEMP catalog inspection was denied: {observed!r}')
        assert any(event[0] == sqlite3.SQLITE_READ and event[3] == 'temp' for event in observed)


@pytest.mark.parametrize('statement', [
    'SELECT value FROM temp.shadow',
    'INSERT INTO temp.shadow VALUES (2)',
    'UPDATE temp.shadow SET value=2',
    'DELETE FROM temp.shadow',
    'CREATE TEMP TABLE other(value INTEGER)',
    'DROP TABLE temp.shadow',
    "ATTACH ':memory:' AS extra",
])
def test_metadata_authorizer_does_not_allow_temp_work_or_attach(statement):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TEMP TABLE shadow(value INTEGER)')
        conn.execute('INSERT INTO temp.shadow VALUES (1)')
        conn.commit()
        conn.set_authorizer(module()._metadata_authorizer(()))
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(statement)
        conn.set_authorizer(None)
        assert conn.execute('SELECT value FROM temp.shadow').fetchall() == [(1,)]
        assert [row[1] for row in conn.execute('PRAGMA database_list')] == ['main', 'temp']
