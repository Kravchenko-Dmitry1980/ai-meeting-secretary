"""Legacy quarantine is durable deny evidence, never an activation permission."""
from contextlib import closing
from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from test_team_restore_preview import copy, files, SENSITIVE
from secretary.infrastructure.team_restore_preview import reconciliation_preview


def module(name):
    fullname = 'secretary.' + name
    assert importlib.util.find_spec(fullname), 'R2 quarantine implementation is missing'
    return importlib.import_module(fullname)


def collect(sample):
    preview = reconciliation_preview(sample.backup, sample.restored.destination)
    return module('infrastructure.team_restore_quarantine').collect_quarantine(
        sample.backup, sample.restored.destination,
        expected_preview_sha256=preview['preview_sha256'])


def origin(role, family, values, kind='row'):
    domain = module('domain.restore_quarantine')
    return domain.Origin(role, family, domain.key_digest(role, family, values, kind=kind), kind)


def execute_program(conn, program):
    # Trusted test execution only. Production R2 has no writer/apply entrypoint.
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    try:
        for statement in program.statements:
            conn.execute(statement.sql, statement.params)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def test_typed_composite_keys_do_not_collapse_types_or_separators():
    domain = module('domain.restore_quarantine')
    values = [(1,), ('1',), ('a/b', 'c'), ('a', 'b/c'), (b'1',)]
    hashes = {domain.key_digest('secretary', 'jobs', item) for item in values}
    assert len(hashes) == len(values)
    assert domain.key_digest('secretary', 'jobs', ('x',)) != domain.key_digest('team', 'jobs', ('x',))


def test_actual_restore_collection_preserves_files_holds_and_all_families(copy):
    before = files(copy.restored.destination)
    archive = files(copy.backup)
    plan = collect(copy)
    catalogue = module('infrastructure.restore_quarantine_catalogue')
    families = {(item.origin.authority, item.origin.family) for item in plan.entries}
    assert ('secretary', 'jobs') in families
    assert plan.disposition(origin('secretary', 'jobs', ('legacy-job',))) == 'no_replay'
    assert set(plan.coverage) == {'secretary', 'team', 'billing'}
    for role, counts in plan.coverage.items():
        assert set(counts) == set(catalogue.FAMILY_DISPOSITIONS[role])
    assert len(plan.input.sources) == 4
    assert all(item.disposition in {'no_replay', 'authority_invalid', 'evidence_only'} for item in plan.entries)
    encoded = json.dumps(plan.report())
    assert SENSITIVE not in encoded and 'legacy-job' not in encoded and 'synthetic-key' not in encoded
    assert plan.report()['activation_supported'] is False
    assert collect(copy).plan_sha256 == plan.plan_sha256
    assert files(copy.restored.destination) == before and files(copy.backup) == archive


def test_changed_preview_is_refused_without_writes(copy):
    target = module('infrastructure.team_restore_quarantine')
    domain = module('domain.restore_quarantine')
    before = files(copy.restored.destination)
    with pytest.raises(domain.RestoreQuarantineError, match='restore_quarantine_input_changed'):
        target.collect_quarantine(copy.backup, copy.restored.destination, expected_preview_sha256='0' * 64)
    assert files(copy.restored.destination) == before


def test_unknown_business_family_refuses_completeness(copy):
    with closing(sqlite3.connect(copy.restored.databases['team'])) as conn, conn:
        conn.execute('CREATE TABLE unknown_external_intent(id TEXT PRIMARY KEY,payload TEXT)')
        conn.execute('INSERT INTO unknown_external_intent VALUES(?,?)', ('unknown', SENSITIVE))
    domain = module('domain.restore_quarantine')
    before = files(copy.restored.destination)
    with pytest.raises(domain.RestoreQuarantineError, match='restore_quarantine_catalogue_unsupported'):
        collect(copy)
    assert files(copy.restored.destination) == before


def test_new_column_on_existing_schema_version_refuses_completeness(copy):
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute('ALTER TABLE jobs ADD COLUMN hidden_external_intent TEXT')
    domain = module('domain.restore_quarantine')
    before = files(copy.restored.destination)
    with pytest.raises(domain.RestoreQuarantineError, match='restore_quarantine_catalogue_unsupported'):
        collect(copy)
    assert files(copy.restored.destination) == before


@pytest.mark.parametrize('status', ['queued', 'running', 'paused_budget', 'failed', 'cancelled', 'completed'])
def test_terminal_or_waiting_job_status_never_erases_no_replay(copy, status):
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute('UPDATE jobs SET status=?', (status,))
    before = files(copy.restored.destination)
    value = collect(copy)
    assert value.disposition(origin('secretary', 'jobs', ('legacy-job',))) == 'no_replay'
    assert files(copy.restored.destination) == before


def test_shared_nullable_stage_slot_aggregates_both_old_attempts(copy):
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
        conn.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,chunk_id,version,payload)
            SELECT 'second-attempt',meeting_id,stage,'failed',created_at,updated_at,chunk_id,version,payload
            FROM jobs WHERE id='legacy-job'""")
    value = collect(copy)
    roots = [item for item in value.entries if item.origin.authority == 'secretary'
             and item.origin.family == 'jobs' and item.origin.key_kind == 'semantic:stage_slot']
    assert len(roots) == 1 and value.coverage['secretary']['jobs'] == 2
    actual = {link.child for link in value.links if link.parent == roots[0].origin and link.relation == 'authorized_by'}
    assert actual == {origin('secretary', 'jobs', ('legacy-job',)), origin('secretary', 'jobs', ('second-attempt',))}
    assert collect(copy).plan_sha256 == value.plan_sha256


def test_inputs_changed_during_collection_refuse_old_preview(copy, monkeypatch):
    target = module('infrastructure.team_restore_quarantine')
    domain = module('domain.restore_quarantine')
    expected = reconciliation_preview(copy.backup, copy.restored.destination)['preview_sha256']
    original = target._metadata
    def changed(*args):
        value = original(*args)
        with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn, conn:
            conn.execute("UPDATE jobs SET status='completed'")
        return value
    monkeypatch.setattr(target, '_metadata', changed)
    with pytest.raises(domain.RestoreQuarantineError, match='restore_quarantine_input_changed'):
        target.collect_quarantine(copy.backup, copy.restored.destination, expected_preview_sha256=expected)


@pytest.mark.parametrize('bound', ['MAX_ROWS', 'MAX_ENTRIES', 'MAX_LINKS', 'MAX_CONTENT_BYTES', 'MAX_PROTOCOL_RECORDS'])
def test_inventory_growth_is_bounded_without_mutating_sources(copy, monkeypatch, bound):
    target = module('infrastructure.team_restore_quarantine')
    domain = module('domain.restore_quarantine')
    before = files(copy.restored.destination)
    monkeypatch.setattr(target, bound, 0)
    with pytest.raises(domain.RestoreQuarantineError, match='restore_quarantine_inventory_bounded'):
        collect(copy)
    assert files(copy.restored.destination) == before


def test_non_unique_declared_identity_is_not_silently_collapsed(copy):
    with closing(sqlite3.connect(copy.restored.databases['billing'])) as conn, conn:
        conn.execute('INSERT INTO billing_schema VALUES(3)')
    domain = module('domain.restore_quarantine')
    with pytest.raises(domain.RestoreQuarantineError, match='restore_quarantine_duplicate_key'):
        collect(copy)


def test_new_uuid_cannot_reauthorize_an_old_job_or_evidence(copy):
    domain = module('domain.restore_quarantine')
    plan = collect(copy)
    new_job = origin('secretary', 'jobs', (str(uuid4()),))
    old_job = origin('secretary', 'jobs', ('legacy-job',))
    assert plan.disposition(new_job) is None  # Absence is not a new work grant.
    with pytest.raises(domain.RestoreQuarantineError, match='restore_legacy_work_blocked'):
        plan.assert_no_legacy_authority((new_job, old_job))
    old_evidence = next(item.origin for item in plan.entries if item.disposition == 'evidence_only')
    with pytest.raises(domain.RestoreQuarantineError, match='restore_legacy_work_blocked'):
        plan.assert_no_legacy_authority((old_evidence,))
    with pytest.raises(domain.RestoreQuarantineError, match='restore_lineage_unbound'):
        plan.assert_no_legacy_authority(())
    assert plan.assert_no_legacy_authority((new_job,)) is None  # Still no activation grant.


def test_actual_team_command_keeps_source_action_even_without_local_publication(copy):
    from secretary.domain.team import TaskCommand, TaskOrigin
    operation, actor, publication = str(uuid4()), str(uuid4()), str(uuid4())
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn:
        meeting = conn.execute('SELECT id FROM meetings').fetchone()[0]
    command = TaskCommand(operation_id=operation, project_id='7', action='create',
        values={'title': 'Synthetic restored source action', 'assignee_id': actor},
        origin=TaskOrigin(source_kind='meeting', publication_id=publication,
                          meeting_id=meeting, transcript_version=0, summary_version=0, action_id='a0'))
    with closing(sqlite3.connect(copy.restored.databases['team'])) as conn, conn:
        conn.execute('INSERT INTO team_commands VALUES(?,?,?,?,?,?,?,?)',
            (operation, actor, '7', None, 'a' * 64, command.model_dump_json(), '{}', '2026-10-04T00:00:00Z'))
    before = files(copy.restored.destination)
    value = collect(copy)
    source = origin('secretary', 'task_publication_items', (meeting, 0, 0, '7', 'a0'), 'semantic:source_action')
    child = origin('team', 'team_commands', (operation,))
    assert value.disposition(source) == 'no_replay'
    assert any(link.child == child and link.parent == source and link.relation == 'authorized_by' for link in value.links)
    with pytest.raises(module('domain.restore_quarantine').RestoreQuarantineError, match='restore_legacy_work_blocked'):
        value.assert_no_legacy_authority((source,))
    assert files(copy.restored.destination) == before


def test_preparation_program_creates_durable_immutable_quarantine_only(copy, tmp_path):
    plan = collect(copy)
    store = module('infrastructure.restore_quarantine_preparation')
    target = tmp_path / 'synthetic-authority.sqlite3'
    target.write_bytes(copy.restored.databases['secretary'].read_bytes())
    program = store.preparation_program(plan, 'secretary', prepared_by_R3_id=str(uuid4()))
    assert program.activation_supported is False and program.state == 'quarantine_prepared_only'
    assert not hasattr(store, 'execute_preparation')
    with closing(sqlite3.connect(target)) as conn:
        original = conn.execute('SELECT * FROM jobs').fetchall()
        guard = conn.execute('SELECT * FROM maintenance_restore_guard').fetchall()
        execute_program(conn, program)
        receipt = store.read_preparation(conn, plan=plan, authority='secretary')
        assert receipt.plan_sha256 == plan.plan_sha256
        assert receipt.activation_supported is False
        assert conn.execute('SELECT * FROM jobs').fetchall() == original
        assert conn.execute('SELECT * FROM maintenance_restore_guard').fetchall() == guard
        replay = store.preparation_program(plan, 'secretary',
            prepared_by_R3_id=receipt.prepared_by_R3_id, existing=receipt)
        assert replay.statements == ()
        for table in store.PROTOCOL_TABLES:
            with pytest.raises(sqlite3.IntegrityError): conn.execute('DELETE FROM ' + table)
            with pytest.raises(sqlite3.IntegrityError): conn.execute('UPDATE ' + table + ' SET rowid=rowid')
            with pytest.raises(sqlite3.IntegrityError): conn.execute('INSERT OR REPLACE INTO ' + table + ' SELECT * FROM ' + table)
        assert store.read_preparation(conn, plan=plan, authority='secretary') == receipt


def test_partial_local_preparation_rolls_back_to_original_copy(copy, tmp_path):
    store = module('infrastructure.restore_quarantine_preparation')
    plan = collect(copy)
    target = tmp_path / 'partial.sqlite3'
    target.write_bytes(copy.restored.databases['billing'].read_bytes())
    program = store.preparation_program(plan, 'billing', prepared_by_R3_id=str(uuid4()))
    with closing(sqlite3.connect(target)) as conn:
        charges = conn.execute('SELECT * FROM billing_charges').fetchall()
        conn.execute('BEGIN IMMEDIATE')
        for statement in program.statements[:len(program.statements) // 2]:
            conn.execute(statement.sql, statement.params)
        conn.rollback()
        assert store.read_preparation(conn, plan=plan, authority='billing') is None
        assert conn.execute('SELECT * FROM billing_charges').fetchall() == charges


def test_corrupt_prepared_protocol_or_entries_cannot_be_read_as_complete(copy, tmp_path):
    domain = module('domain.restore_quarantine')
    store = module('infrastructure.restore_quarantine_preparation')
    plan = collect(copy)
    target = tmp_path / 'corrupt.sqlite3'
    target.write_bytes(copy.restored.databases['team'].read_bytes())
    with closing(sqlite3.connect(target)) as conn:
        execute_program(conn, store.preparation_program(plan, 'team', prepared_by_R3_id=str(uuid4())))
        trigger = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='restore_quarantine_entries' AND name LIKE '%no_delete'").fetchone()[0]
        conn.execute('DROP TRIGGER ' + trigger)
        conn.execute('DELETE FROM restore_quarantine_entries')
        with pytest.raises(domain.RestoreQuarantineError, match='restore_quarantine_preparation_invalid'):
            store.read_preparation(conn, plan=plan, authority='team')


def test_prepared_copy_remains_blocked_at_ordinary_startup(copy, tmp_path):
    plan = collect(copy)
    store = module('infrastructure.restore_quarantine_preparation')
    with closing(sqlite3.connect(copy.restored.databases['secretary'])) as conn:
        execute_program(conn, store.preparation_program(plan, 'secretary', prepared_by_R3_id=str(uuid4())))
    from secretary.api import create_app
    from secretary.settings import Settings
    from secretary.team_settings import RuntimeConfigurationError
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=copy.restored.databases['secretary'].parent,
                        cloud_enabled=False)
    with pytest.raises(RuntimeConfigurationError, match='team_restore_reconciliation_required'):
        create_app(settings=settings, run_worker=False, outbound_enabled=False)
