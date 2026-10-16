"""Schema 10 lineage stays deny-only across inactive synthetic restores."""
from contextlib import closing
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.domain.restore_quarantine import Origin, RestoreQuarantineError, key_digest
from secretary.infrastructure.database import Database
from secretary.infrastructure.team_backup import BackupError
from secretary.infrastructure.team_restore_preview import reconciliation_preview
from secretary.infrastructure.team_restore_quarantine import collect_quarantine
from test_team_backup import full_fixture
from test_team_restore_preview import copy, files


def secretary_version(sample, version):
    """Build exact older/newer test shapes without opening owner stores."""
    with closing(sqlite3.connect(sample.restored.databases['secretary'])) as conn, conn:
        present = conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='summary_resume_operations'").fetchone()
        if version == 9:
            # The current application may create schema 10. Model an old backup,
            # removing only that migration's table and its dependent triggers.
            triggers = conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND sql LIKE '%summary_resume_operations%'").fetchall()
            for name, in triggers:
                conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            if present:
                conn.execute('DROP TABLE summary_resume_operations')
            conn.execute('DROP INDEX jobs_one_active')
            conn.execute("""CREATE UNIQUE INDEX jobs_one_active ON jobs(meeting_id,stage,IFNULL(chunk_id,''),version)
                WHERE stage!='identify_speakers' AND status IN ('queued','running','waiting_config','paused_budget','uncertain')""")
            conn.execute('DELETE FROM schema_migrations WHERE version=10')
        else:
            assert present, 'the real application must produce migration 10'


def collect(sample):
    preview = reconciliation_preview(sample.backup, sample.restored.destination)
    return collect_quarantine(sample.backup, sample.restored.destination,
                              expected_preview_sha256=preview['preview_sha256'])


@pytest.mark.parametrize('version', [9, 10])
def test_known_secretary_schema_remains_inactive_and_fully_classified(copy, version):
    secretary_version(copy, version)
    before = files(copy.restored.destination)
    preview = reconciliation_preview(copy.backup, copy.restored.destination)
    plan = collect(copy)
    assert preview['activation_supported'] is False and preview['outbound_enabled'] is False
    assert plan.report()['activation_supported'] is False
    assert ('summary_resume_operations' in plan.coverage['secretary']) is (version == 10)
    assert files(copy.restored.destination) == before


def test_schema_10_missing_lineage_family_refuses_completeness(copy):
    secretary_version(copy, 10)
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute('DROP TABLE summary_resume_operations')
    before = files(copy.restored.destination)
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_catalogue_unsupported'):
        collect(copy)
    assert files(copy.restored.destination) == before


@pytest.mark.parametrize('role,version', [('secretary', 11), ('team', 10)])
def test_unknown_authority_schema_cannot_inherit_secretary_10_support(copy, role, version):
    table = 'schema_migrations' if role == 'secretary' else 'team_schema'
    with closing(sqlite3.connect(copy.restored.databases[role])) as conn, conn:
        if role == 'secretary':
            conn.execute('INSERT INTO schema_migrations VALUES(?,?)', (version, '2026-10-16T00:00:00+00:00'))
        else:
            conn.execute('INSERT INTO team_schema VALUES(?)', (version,))
    before = files(copy.restored.destination)
    with pytest.raises(BackupError, match='restore_preview_schema_unsupported'):
        reconciliation_preview(copy.backup, copy.restored.destination)
    assert files(copy.restored.destination) == before


def test_partial_secretary_migration_sequence_refuses_completeness(copy):
    secretary_version(copy, 10)
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute('DELETE FROM schema_migrations WHERE version=8')
    with pytest.raises(BackupError, match='restore_preview_schema_unsupported'):
        reconciliation_preview(copy.backup, copy.restored.destination)


def test_schema_9_cannot_smuggle_continuation_family(copy):
    # Keeping the schema 10 table while claiming schema 9 is not compatibility.
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute('DELETE FROM schema_migrations WHERE version=10')
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_catalogue_unsupported'):
        collect(copy)


@pytest.mark.parametrize('change', ['column', 'primary_key'])
def test_schema_10_continuation_shape_is_closed_even_when_no_rows_exist(copy, change):
    secretary_version(copy, 10)
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        if change == 'column':
            conn.execute('ALTER TABLE summary_resume_operations ADD COLUMN hidden_replay_intent TEXT')
        else:
            ddl = conn.execute("SELECT sql FROM sqlite_schema WHERE name='summary_resume_operations'").fetchone()[0]
            conn.execute('DROP TABLE summary_resume_operations')
            ddl = ddl.replace('operation_id TEXT PRIMARY KEY', 'operation_id TEXT')
            ddl = ddl.replace('source_job_id TEXT NOT NULL UNIQUE', 'source_job_id TEXT NOT NULL PRIMARY KEY')
            conn.execute(ddl)
    before = files(copy.restored.destination)
    with pytest.raises(RestoreQuarantineError, match='restore_quarantine_catalogue_unsupported'):
        collect(copy)
    assert files(copy.restored.destination) == before


def test_restored_continuation_is_no_replay_and_cannot_claim_its_queued_child(copy):
    secretary_version(copy, 10)
    path = copy.restored.databases['secretary']
    source, child, operation = str(uuid4()), str(uuid4()), str(uuid4())
    stamp, context = '2026-10-16T00:00:00+00:00', 'a' * 64
    # Synthetic historical lineage is inserted in the same deferred-FK order
    # as the real command. No provider, live store, or activation is involved.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('PRAGMA foreign_keys=ON')
        meeting = conn.execute('SELECT id FROM meetings').fetchone()[0]
        conn.execute("UPDATE jobs SET status='succeeded' WHERE id='legacy-job'")
        conn.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload)
            VALUES(?,?,'summarize','uncertain',?,?,1,'{}')""", (source, meeting, stamp, stamp))
        conn.execute("INSERT INTO summary_input_contexts VALUES(?,?,1,'synthetic','{}',?)", (source, meeting, context))
        conn.execute('INSERT INTO summary_resume_operations VALUES(?,?,1,?,?,?,?,1,?)',
                     (operation, meeting, source, child, 'b' * 64, context, stamp))
        conn.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload)
            VALUES(?,?,'summarize','queued',?,?,1,'{}')""", (child, meeting, stamp, stamp))
        conn.execute("INSERT INTO usage(id,meeting_id,job_id,kind,estimated_rub,status,created_at) VALUES(?,?,?,'summary',1.5,'unknown',?)",
                     (str(uuid4()), meeting, source, stamp))
    before = files(copy.restored.destination)
    plan = collect(copy)
    operation_origin = Origin('secretary', 'summary_resume_operations', key_digest('secretary', 'summary_resume_operations', (operation,)))
    child_origin = Origin('secretary', 'jobs', key_digest('secretary', 'jobs', (child,)))
    source_origin = Origin('secretary', 'jobs', key_digest('secretary', 'jobs', (source,)))
    assert plan.disposition(operation_origin) == 'no_replay'
    assert plan.disposition(child_origin) == 'no_replay'
    assert any(link.child == child_origin and link.parent == operation_origin and link.relation == 'authorized_by' for link in plan.links)
    assert any(link.child == operation_origin and link.parent == source_origin and link.relation == 'source_reference' for link in plan.links)
    with pytest.raises(RestoreQuarantineError, match='restore_legacy_work_blocked'):
        plan.assert_no_legacy_authority((operation_origin, child_origin))
    db = Database(path)
    with pytest.raises(sqlite3.DatabaseError):
        db.claim()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT status,attempts FROM jobs WHERE id=?', (child,)).fetchone() == ('queued', 0)
        assert conn.execute('SELECT status FROM jobs WHERE id=?', (source,)).fetchone() == ('uncertain',)
        assert conn.execute('SELECT status,estimated_rub FROM usage').fetchall() == [('unknown', 1.5)]
    assert files(copy.restored.destination) == before


def test_schema_9_diagnostics_do_not_require_a_migration_or_a_new_table(copy):
    secretary_version(copy, 9)
    path = copy.restored.databases['secretary']
    before = files(copy.restored.destination)
    db = Database(path)
    jobs = db.jobs(db.list_meetings()[0]['id'])
    assert len(jobs) == 1 and jobs[0]['id'] == 'legacy-job'
    assert jobs[0]['resumed_from_job_id'] is None
    assert files(copy.restored.destination) == before


def test_actual_schema_9_archive_still_restores_to_deny_only_diagnostics(tmp_path):
    with full_fixture(tmp_path) as (backup, _, service, _, sources):
        db = Database(sources['secretary'])
        meeting = db.create_meeting('Synthetic schema 9 backup')
        job = db.enqueue(meeting['id'], 'transcribe')
        secretary_version(SimpleNamespace(restored=SimpleNamespace(databases=sources)), 9)
        archived = service.backup(tmp_path / 'backups' / 'schema-9')
        assert archived.full_recovery
        restored = backup.restore_backup(archived.destination, tmp_path / 'restore', restore_root=tmp_path)
        sample = SimpleNamespace(backup=archived.destination, restored=restored)
        before = files(restored.destination)
        archive_before = files(archived.destination)
        plan = collect(sample)
        assert 'summary_resume_operations' not in plan.coverage['secretary']
        reopened = Database(restored.databases['secretary'])
        assert reopened.jobs(meeting['id'])[0]['resumed_from_job_id'] is None
        assert reopened.job(job['id'])['status'] == 'queued'
        with pytest.raises(sqlite3.DatabaseError):
            reopened.claim()
        assert files(restored.destination) == before
        assert files(archived.destination) == archive_before
