"""T3 storage contracts against isolated SQLite, with no provider or app startup."""
from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError


def ident():
    return str(uuid4())


def load_api():
    modules = {}
    for name in ('domain.team', 'domain.task_delivery', 'infrastructure.team_database',
                 'infrastructure.team_repository'):
        full = 'secretary.' + name
        assert importlib.util.find_spec(full) is not None, f'T3 module missing: {full}'
        modules.update(vars(importlib.import_module(full)))
    return SimpleNamespace(**modules)


@pytest.fixture
def api():
    return load_api()


def test_empty_database_has_no_claimable_command(tmp_path):
    api = load_api()
    repository = api.TeamRepository(api.TeamDatabase(tmp_path / 'empty.sqlite3'))
    assert repository.claim_command('worker') is None


@pytest.fixture
def case(tmp_path, api):
    time = [datetime(2026, 10, 3, 8, tzinfo=timezone.utc)]
    db = api.TeamDatabase(tmp_path / 'team.sqlite3')
    repo = api.TeamRepository(db, clock=lambda: time[0])
    owner = api.TeamMember(id=ident(), display_name='Owner', role='owner',
                           max_user_id='9007199254740993', vikunja_user_id='11', project_ids=('7',))
    member = api.TeamMember(id=ident(), display_name='Member', role='member',
                            max_user_id='9007199254740995', vikunja_user_id='12', project_ids=('7',))
    repo.upsert_member(owner, expected_revision=None)
    repo.upsert_member(member, expected_revision=None)
    snapshot = api.TaskSnapshot(task_id='9007199254740997', project_id='7', revision=0,
        remote_fingerprint='a' * 64, title='Synthetic task', assignee_id=member.id,
        bucket='inbox', important=False, urgent=False, classification_confirmed=True)
    repo.save_projection(snapshot, expected_revision=None)
    return SimpleNamespace(api=api, db=db, repo=repo, owner=owner, member=member,
                           snapshot=snapshot, time=time, path=db.path)


def command(c, **edits):
    return c.api.TaskCommand(operation_id=edits.pop('operation_id', ident()), project_id='7',
        task_id=c.snapshot.task_id, expected_revision=0, expected_fingerprint='a' * 64,
        action='set_state', values={'bucket': 'doing'}, **edits)


def test_operation_replay_returns_original_acceptance_and_current_execution(case):
    c = case
    cmd = command(c)
    accepted = c.repo.accept_command(c.member.id, cmd)
    claim = c.repo.claim_command('worker')
    applied = c.snapshot.model_copy(update={'revision': 1, 'bucket': 'doing', 'remote_fingerprint': 'b' * 64})
    c.repo.record_remote_result(claim, state='applied', snapshot=applied)
    replay = c.repo.accept_command(c.member.id, cmd)
    assert replay.acceptance_receipt == accepted.acceptance_receipt
    assert replay.execution_state.state == 'applied'
    assert replay.current.revision == 1
    assert c.repo.claim_command('other') is None


def test_same_operation_different_payload_or_actor_conflicts(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    changed = cmd.model_copy(update={'values': c.api.TaskChange(bucket='blocked')})
    for actor, value in ((c.member.id, changed), (c.owner.id, cmd)):
        with pytest.raises(c.api.TeamConflict, match='operation_id_conflict'):
            c.repo.accept_command(actor, value)


@pytest.mark.parametrize('value', [9007199254740993, 1.0, True, '01', '1e2', '-1'])
def test_external_ids_reject_lossy_or_noncanonical_values(api, value):
    with pytest.raises(ValidationError):
        api.TeamMember(id=ident(), display_name='Person', max_user_id=value, vikunja_user_id='2')


def test_decimal_string_ids_roundtrip_exactly_and_uuid_revision_validate(case):
    c = case
    person = c.repo.resolve_member('9007199254740993')
    assert json.loads(person.model_dump_json())['max_user_id'] == '9007199254740993'
    assert c.repo.get_projection(c.owner.id, c.snapshot.task_id).task_id == '9007199254740997'
    with pytest.raises(ValidationError):
        c.api.TeamMember(id='not-uuid', display_name='Person', max_user_id='1', vikunja_user_id='2')
    with pytest.raises(ValidationError):
        c.api.TaskSnapshot.model_validate({**c.snapshot.model_dump(), 'revision': True})


def test_scope_permissions_are_checked_at_accept_and_before_execution(case):
    c = case
    foreign = c.member.model_copy(update={'id': ident(), 'max_user_id': '77', 'vikunja_user_id': '78', 'project_ids': ('8',)})
    c.repo.upsert_member(foreign, expected_revision=None)
    with pytest.raises(c.api.TeamForbidden):
        c.repo.get_projection(foreign.id, c.snapshot.task_id)
    rejected = c.repo.accept_command(foreign.id, command(c))
    assert rejected.acceptance_receipt.decision == 'rejected'
    assert rejected.current is None
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    claim = c.repo.claim_command('worker')
    c.repo.upsert_member(c.member.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    with pytest.raises(c.api.TeamForbidden):
        c.repo.authorize_claim(claim)
    assert c.repo.claim_command('next') is None
    assert c.repo.resolve_member(c.member.max_user_id) is None


def test_revoked_member_cannot_apply_queued_command(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    c.repo.upsert_member(c.member.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    assert c.repo.claim_command('worker') is None
    receipt = c.repo.get_receipt(c.owner.id, cmd.operation_id)
    assert receipt.execution_state.state == 'rejected'
    assert receipt.execution_state.error_code == 'actor_disabled'


def test_member_cannot_assign_change_final_due_cancel_or_close_important(case):
    c = case
    requests = [('assign', {'assignee_id': c.owner.id}),
                ('set_due', {'due_at': None, 'reason': 'Changed', 'due_confirmed': True}),
                ('set_state', {'bucket': 'cancelled'}),
                ('classify', {'important': True, 'urgent': False, 'classification_confirmed': True})]
    for action, values in requests:
        cmd = command(c).model_copy(update={'action': action, 'values': c.api.TaskChange(**values)})
        assert c.repo.accept_command(c.member.id, cmd).acceptance_receipt.decision == 'rejected'
    important = c.snapshot.model_copy(update={'revision': 1, 'important': True})
    c.repo.save_projection(important, expected_revision=0)
    cmd = command(c).model_copy(update={'expected_revision': 1, 'values': c.api.TaskChange(bucket='done', result='Done')})
    assert c.repo.accept_command(c.member.id, cmd).acceptance_receipt.decision == 'rejected'


def test_stale_revision_conflicts_and_task_lease_serializes_commands(case):
    c = case
    one, two = command(c), command(c)
    c.repo.accept_command(c.member.id, one)
    c.repo.accept_command(c.member.id, two)
    first = c.repo.claim_command('worker-a')
    assert c.repo.claim_command('worker-b') is None
    c.repo.record_remote_result(first, state='applied', snapshot=c.snapshot.model_copy(
        update={'revision': 1, 'bucket': 'doing', 'remote_fingerprint': 'b' * 64}))
    assert c.repo.claim_command('worker-b') is None
    assert c.repo.get_receipt(c.owner.id, two.operation_id).execution_state.state == 'conflict'


def test_claim_is_atomic_across_processes(case):
    c = case
    c.repo.accept_command(c.member.id, command(c))
    code = ('import sys; from secretary.infrastructure.team_database import TeamDatabase; '
            'from secretary.infrastructure.team_repository import TeamRepository; '
            'r=TeamRepository(TeamDatabase(sys.argv[1])); '
            'print("claimed" if r.claim_command(sys.argv[2]) else "empty")')
    def run(worker):
        return subprocess.run([sys.executable, '-B', '-c', code, str(c.path), worker],
                              text=True, capture_output=True, check=True).stdout.strip()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ('one', 'two')))
    assert sorted(results) == ['claimed', 'empty']


def test_expired_claim_is_uncertain_not_requeued_and_fence_rejects_late_writer(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    old = c.repo.claim_command('old', lease_seconds=5)
    c.time[0] += timedelta(seconds=6)
    assert c.repo.recover_expired() == 1
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'uncertain'
    c.repo.accept_command(c.member.id, command(c))
    assert c.repo.claim_command('new') is None
    fresh = c.repo.claim_reconciliation(cmd.operation_id, 'reconciler')
    assert fresh.reconciliation is True
    assert fresh.fence > old.fence
    with pytest.raises(c.api.TeamConflict, match='stale_claim'):
        c.repo.record_remote_result(old, state='applied', snapshot=c.snapshot.model_copy(update={'revision': 1}))
    with pytest.raises(c.api.TeamConflict, match='reconciliation_is_read_only'):
        c.repo.authorize_claim(fresh)
    c.repo.record_remote_result(fresh, state='applied', snapshot=c.snapshot.model_copy(update={'revision': 1}))
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'applied'


def test_applied_requires_verified_scoped_snapshot_and_atomic_rollback(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    claim = c.repo.claim_command('worker')
    for snapshot in (None, c.snapshot.model_copy(update={'project_id': '8', 'revision': 1})):
        with pytest.raises(c.api.TeamConflict):
            c.repo.record_remote_result(claim, state='applied', snapshot=snapshot)
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).execution_state.state == 'running'
    assert c.repo.get_projection(c.owner.id, c.snapshot.task_id).revision == 0


def test_acceptance_payload_and_journal_are_immutable_and_restore_is_idempotent(case, tmp_path):
    c = case
    cmd = command(c)
    before = c.repo.accept_command(c.member.id, cmd)
    with c.db.connection() as conn:
        for sql in ('UPDATE team_commands SET payload=\'{}\'', 'DELETE FROM team_commands',
                    'UPDATE team_journal SET event=\'changed\''):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        restored = tmp_path / 'restored.sqlite3'
        with sqlite3.connect(restored) as dest:
            conn.backup(dest)
    other = c.api.TeamRepository(c.api.TeamDatabase(restored), clock=lambda: c.time[0])
    c.api.TeamDatabase(restored)
    assert other.get_receipt(c.owner.id, cmd.operation_id) == before
    assert other.resolve_member(c.member.max_user_id).id == c.member.id


def test_inbox_outbox_dedup_scope_and_uncertain_recovery(case):
    c = case
    event = c.api.DurableMessage(id=ident(), kind='inbox', dedup_key='bot:chat:mid:message_created',
        actor_id=c.member.id, project_id='7', payload={'message_id': '9007199254740999'})
    assert c.repo.enqueue_message(event).state == 'pending'
    assert c.repo.enqueue_message(event).state == 'pending'
    with pytest.raises(c.api.TeamConflict):
        c.repo.enqueue_message(event.model_copy(update={'payload': {'message_id': 'different'}}))
    claim = c.repo.claim_message('inbox', 'worker', lease_seconds=5)
    c.time[0] += timedelta(seconds=6)
    c.repo.recover_expired()
    assert c.repo.get_message(event.id).state == 'uncertain'
    assert c.repo.claim_message('inbox', 'worker') is None
    with pytest.raises(c.api.TeamConflict):
        c.repo.finish_message(claim, state='sent')
    out = event.model_copy(update={'id': ident(), 'kind': 'outbox', 'dedup_key': 'task:due:recipient:rule:time'})
    c.repo.enqueue_message(out)
    c.repo.upsert_member(c.member.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    assert c.repo.claim_message('outbox', 'worker') is None
    assert c.repo.get_message(out.id).state == 'cancelled'


def test_projection_cas_null_classification_and_due_confirmation(case):
    c = case
    unknown = c.snapshot.model_copy(update={'revision': 1, 'important': None, 'urgent': None,
                                            'classification_confirmed': False})
    c.repo.save_projection(unknown, expected_revision=0)
    assert c.repo.get_projection(c.owner.id, c.snapshot.task_id).important is None
    with pytest.raises(c.api.TeamConflict):
        c.repo.save_projection(c.snapshot, expected_revision=0)
    with pytest.raises(ValidationError):
        c.api.TaskSnapshot.model_validate({**unknown.model_dump(), 'classification_confirmed': True})
    with pytest.raises(ValidationError):
        c.api.TaskChange(due_at='2026-10-04T18:00:00', due_confirmed=True)
    with pytest.raises(ValidationError):
        c.api.TaskCommand.model_validate({**command(c).model_dump(), 'values': {'bucket': 'doing', 'assignee_id': c.owner.id}})


def test_publication_source_scope_and_watermarks_remain_distinct(api):
    scope = api.PublicationScope(meeting_id='meeting', transcript_version=1, summary_version=2,
                                destination_project_id='7')
    marks = api.PublicationWatermarks(assignment_revision=4, roster_revision=3,
        attribution_revision=2, context_hash='a' * 64)
    item = api.PublicationTask(action_id='action', title='Report', assignee_id=ident(),
                               source_segment_ids=('segment',), evidence_quote='I will report')
    preview = api.PublishPreview(preview_id=ident(), scope=scope, watermarks=marks, items=(item,))
    assert len(preview.preview_hash) == 64
    with pytest.raises(ValidationError):
        api.PublishCommand(operation_id=ident(), preview_id=preview.preview_id, scope=scope,
            watermarks=marks, preview_hash='a', items=(item,))
    changed = api.PublishPreview(preview_id=preview.preview_id, scope=scope,
        watermarks=marks.model_copy(update={'assignment_revision': 5}), items=(item,))
    assert changed.preview_hash != preview.preview_hash


def test_rejected_cross_project_task_never_leaks_in_replay_or_receipt(case):
    c = case
    secret = c.snapshot.model_copy(update={'task_id': '99', 'project_id': '8', 'title': 'Foreign task'})
    c.repo.save_projection(secret, expected_revision=None)
    cmd = command(c).model_copy(update={'task_id': '99'})
    rejected = c.repo.accept_command(c.owner.id, cmd)
    assert rejected.acceptance_receipt.decision == 'rejected'
    assert rejected.current is None
    assert c.repo.accept_command(c.owner.id, cmd).current is None
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).current is None


def test_message_claim_rechecks_revocation_before_send_and_renews_safely(case):
    c = case
    event = c.api.DurableMessage(id=ident(), kind='outbox', dedup_key='notification',
        actor_id=c.member.id, project_id='7', payload={'task_id': c.snapshot.task_id})
    c.repo.enqueue_message(event)
    claim = c.repo.claim_message('outbox', 'worker', lease_seconds=10)
    assert c.repo.authorize_message(claim) == event
    c.time[0] += timedelta(seconds=5)
    renewed = c.repo.renew_message(claim, lease_seconds=10)
    c.time[0] += timedelta(seconds=6)
    assert c.repo.recover_expired() == 0
    assert c.repo.authorize_message(renewed) == event
    c.repo.upsert_member(c.member.model_copy(update={'enabled': False, 'revision': 1}), expected_revision=0)
    with pytest.raises(c.api.TeamForbidden):
        c.repo.authorize_message(renewed)


def test_message_uncertain_reconciliation_cannot_resend_and_fences_previous_claim(case):
    c = case
    event = c.api.DurableMessage(id=ident(), kind='inbox', dedup_key='work',
        actor_id=c.member.id, project_id='7', payload={'mid': '42'})
    c.repo.enqueue_message(event)
    old = c.repo.claim_message('inbox', 'worker')
    c.repo.finish_message(old, state='uncertain')
    claim = c.repo.claim_message_reconciliation(event.id, 'reconciler')
    with pytest.raises(c.api.TeamConflict, match='reconciliation_is_read_only'):
        c.repo.authorize_message(claim)
    with pytest.raises(c.api.TeamConflict):
        c.repo.finish_message(old, state='sent')
    c.repo.finish_message(claim, state='sent')
    assert c.repo.get_message(event.id).state == 'sent'


def test_command_renewal_rechecks_database_deadline_and_never_reuses_terminal_claim(case):
    c = case
    c.repo.accept_command(c.member.id, command(c))
    claim = c.repo.claim_command('worker', lease_seconds=10)
    assert c.repo.authorize_claim(claim).task_id == c.snapshot.task_id
    c.time[0] += timedelta(seconds=5)
    renewed = c.repo.renew_claim(claim, lease_seconds=10)
    c.time[0] += timedelta(seconds=6)
    assert c.repo.recover_expired() == 0
    c.repo.record_remote_result(renewed, state='rejected', error_code='provider_rejected')
    with pytest.raises(c.api.TeamConflict, match='stale_claim'):
        c.repo.renew_claim(claim)


def test_claim_cannot_add_implicit_null_patch_fields(case):
    c = case
    c.repo.accept_command(c.member.id, command(c))
    claim = c.repo.claim_command('worker')
    poisoned = claim.command.model_copy(update={'values': c.api.TaskChange(bucket='doing', assignee_id=None)})
    forged = claim.model_copy(update={'command': poisoned})
    with pytest.raises(c.api.TeamConflict, match='stale_claim'):
        c.repo.authorize_claim(forged)


def test_create_requires_complete_confirmed_classification(case):
    c = case
    with pytest.raises(ValidationError):
        c.api.TaskCommand(operation_id=ident(), project_id='7', action='create',
            values={'title': 'New task', 'assignee_id': c.member.id, 'classification_confirmed': True})


def test_live_partial_progress_keeps_fence_but_expiry_allows_only_read_reconciliation(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    claim = c.repo.claim_command('worker', lease_seconds=5)
    c.repo.record_remote_result(claim, state='reconciling')
    assert c.repo.authorize_claim(claim).task_id == cmd.task_id
    assert c.repo.claim_command('other') is None
    c.time[0] += timedelta(seconds=6)
    assert c.repo.recover_expired() == 1
    read_claim = c.repo.claim_reconciliation(cmd.operation_id, 'reconciler')
    with pytest.raises(c.api.TeamConflict):
        c.repo.authorize_claim(claim)
    with pytest.raises(c.api.TeamConflict, match='reconciliation_is_read_only'):
        c.repo.authorize_claim(read_claim)


def test_unrelated_database_is_refused_without_changing_its_bytes(tmp_path, api):
    path = tmp_path / 'unrelated.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE original(value TEXT)')
        conn.execute("INSERT INTO original VALUES('preserve')")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='not_a_team_database'):
        api.TeamDatabase(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not path.with_name(path.name + '-wal').exists()


@pytest.mark.parametrize('revision', [True, False, 0.0, -1])
def test_administrative_cas_rejects_untyped_revisions(case, revision):
    c = case
    with pytest.raises(ValueError):
        c.repo.upsert_member(c.member.model_copy(update={'revision': 1}), expected_revision=revision)
    with pytest.raises(ValueError):
        c.repo.save_projection(c.snapshot.model_copy(update={'revision': 1}), expected_revision=revision)


def test_create_assignment_mapping_then_verified_result_keeps_exact_remote_id(case):
    c = case
    cmd = c.api.TaskCommand(operation_id=ident(), project_id='7', action='create',
        values={'title': 'Created', 'assignee_id': c.member.id})
    assert c.repo.accept_command(c.owner.id, cmd).acceptance_receipt.decision == 'accepted'
    claim = c.repo.claim_command('creator')
    snapshot = c.snapshot.model_copy(update={'task_id': '9007199254740999', 'title': 'Created'})
    result = c.repo.record_remote_result(claim, state='applied', snapshot=snapshot)
    assert result.execution_state.task_id == '9007199254740999'
    assert c.repo.get_receipt(c.owner.id, cmd.operation_id).current.title == 'Created'


def test_provider_message_dedup_ignores_fresh_local_uuid(case):
    c = case
    original = c.api.DurableMessage(id=ident(), kind='inbox', dedup_key='bot:mid:event',
        actor_id=c.member.id, project_id='7', payload={'message_id': '9007199254740999'})
    c.repo.enqueue_message(original)
    replay = c.repo.enqueue_message(original.model_copy(update={'id': ident()}))
    assert replay.message.id == original.id
    claim = c.repo.claim_message('inbox', 'worker')
    c.repo.finish_message(claim, state='sent')
    assert c.repo.claim_message('inbox', 'worker') is None


@pytest.mark.parametrize('table', ['team_commands', 'team_journal', 'team_messages'])
def test_replace_cannot_rewrite_immutable_evidence_when_recursive_triggers_are_off(case, table):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    message = c.api.DurableMessage(id=ident(), kind='inbox', dedup_key='immutable',
        actor_id=c.member.id, project_id='7', payload={'message_id': '123'})
    c.repo.enqueue_message(message)
    with c.db.connection() as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        before = tuple(conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone())
        placeholders = ','.join('?' for _ in before)
        with pytest.raises(sqlite3.IntegrityError, match='Immutable team evidence'):
            conn.execute(f'INSERT OR REPLACE INTO {table} VALUES({placeholders})', before)
        assert tuple(conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()) == before


def test_replace_cannot_rewrite_message_by_alternate_dedup_key(case):
    c = case
    message = c.api.DurableMessage(id=ident(), kind='inbox', dedup_key='provider-identity',
        actor_id=c.member.id, project_id='7', payload={'message_id': '123'})
    c.repo.enqueue_message(message)
    with c.db.connection() as conn:
        conn.execute('PRAGMA recursive_triggers=OFF')
        conn.execute('PRAGMA foreign_keys=OFF')  # SQL clients must still respect immutable evidence.
        before = tuple(conn.execute('SELECT * FROM team_messages').fetchone())
        replacement = (ident(), *before[1:])
        with pytest.raises(sqlite3.IntegrityError, match='Immutable team evidence'):
            conn.execute('INSERT OR REPLACE INTO team_messages VALUES(?,?,?,?,?,?)', replacement)
        assert tuple(conn.execute('SELECT * FROM team_messages').fetchone()) == before


def test_reconciling_partial_snapshot_is_refused_before_any_write(case):
    c = case
    cmd = command(c)
    c.repo.accept_command(c.member.id, cmd)
    claim = c.repo.claim_command('worker')
    before = c.repo.get_receipt(c.member.id, cmd.operation_id)
    partial = c.snapshot.model_copy(update={'revision': 1, 'remote_fingerprint': 'b' * 64, 'bucket': 'doing'})
    with c.db.connection() as conn:
        journal_count = conn.execute('SELECT COUNT(*) FROM team_journal').fetchone()[0]
    with pytest.raises(c.api.TeamConflict, match='partial_snapshot_requires_durable_step_protocol'):
        c.repo.record_remote_result(claim, state='reconciling', snapshot=partial)
    assert c.repo.get_receipt(c.member.id, cmd.operation_id) == before
    assert c.repo.authorize_claim(claim).revision == 0
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_journal').fetchone()[0] == journal_count
    c.repo.record_remote_result(claim, state='reconciling')
    assert c.repo.authorize_claim(claim).revision == 0


def test_v1_upgrade_preserves_receipts_and_installs_replace_protection(case):
    c = case
    cmd = command(c)
    before = c.repo.accept_command(c.member.id, cmd)
    with c.db.connection() as conn:
        # Recreate actual v1: later tables are fixture-only additions,
        # and did not exist in v1. A version-only downgrade is not a v1 database.
        conn.execute('DROP TRIGGER response_abort_guard')
        conn.execute('DROP TRIGGER abort_response_guard')
        conn.execute('DROP TRIGGER notification_projection_insert')
        conn.execute('DROP TRIGGER notification_projection_due_update')
        for table in ('notification_receipts', 'notification_attempts', 'notification_state',
                      'notification_task_refs', 'notification_intents', 'notification_due_state',
                      'notification_due_generations', 'notification_plan_runs', 'notification_planner_state'):
            conn.execute(f'DROP TABLE {table}')
        for table in ('team_due_resolution_consumptions', 'team_due_resolution_previews',
                      'team_sync_state', 'team_sync_observations', 'team_sync_results', 'team_sync_runs',
                      'voice_notice_state', 'voice_notices', 'voice_confirmations', 'voice_proposals',
                      'voice_proposal_batches', 'voice_contexts', 'voice_transcripts', 'voice_request_aborts', 'voice_responses',
                      'voice_requests', 'voice_job_state', 'voice_jobs',
                      'bot_button_confirmations', 'bot_context_consumptions', 'bot_reply_state',
                      'bot_replies', 'bot_contexts', 'bot_proposals', 'bot_buttons',
                      'bot_event_state', 'bot_events',
                      'team_auth_journal', 'team_auth_rates', 'team_auth_invitations',
                      'team_auth_codes', 'team_auth_sessions', 'team_auth_replays'):
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
            conn.execute(f'DROP TABLE {table}')
        for table in ('team_commands', 'team_messages', 'team_journal'):
            conn.execute(f'DROP TRIGGER IF EXISTS {table}_immutable_insert')
        conn.execute('DELETE FROM team_schema WHERE version>1')
    upgraded = c.api.TeamRepository(c.api.TeamDatabase(c.path), clock=lambda: c.time[0])
    assert upgraded.get_receipt(c.member.id, cmd.operation_id) == before
    with upgraded.db.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 9
        row = tuple(conn.execute('SELECT * FROM team_commands').fetchone())
        with pytest.raises(sqlite3.IntegrityError, match='Immutable team evidence'):
            conn.execute('INSERT OR REPLACE INTO team_commands VALUES(?,?,?,?,?,?,?,?)', row)
