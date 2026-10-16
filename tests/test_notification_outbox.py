"""T10 notification evidence, generation and delivery against isolated Team SQLite."""
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from importlib import import_module, util
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlite3

from secretary.application.reminders import ReminderScheduler
from secretary.domain.bot import BotSend, SendReceipt
from secretary.domain.notifications import NotificationDeferred, NotificationError
from secretary.domain.team import TaskCommand, TaskChange, TaskSnapshot, TeamForbidden, TeamMember
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_repository import TeamRepository
from secretary.infrastructure.team_sync_repository import TeamSyncRepository, SyncObservation


def uid():
    return str(uuid4())


def test_notification_component_exists():
    assert util.find_spec('secretary.infrastructure.notification_repository'), 'T10 durable notification storage missing'


@pytest.fixture
def case(tmp_path):
    assert util.find_spec('secretary.infrastructure.notification_repository'), 'T10 durable notification storage missing'
    module = import_module('secretary.infrastructure.notification_repository')
    now = [datetime(2026, 10, 5, 7, tzinfo=timezone.utc)]
    db = TeamDatabase(tmp_path / 'team.sqlite3')
    team = TeamRepository(db, clock=lambda: now[0])
    owner = TeamMember(id=uid(), display_name='Owner', role='owner', max_user_id='9007199254740993',
                       vikunja_user_id='21', project_ids=('7',))
    member = TeamMember(id=uid(), display_name='Member', max_user_id='9007199254740995',
                        vikunja_user_id='22', project_ids=('7',))
    for item in (owner, member):
        team.upsert_member(item, expected_revision=None)
    task = TaskSnapshot(task_id='9007199254740997', project_id='7', revision=0,
        remote_fingerprint='a' * 64, title='Task', assignee_id=member.id,
        important=False, urgent=False, classification_confirmed=True,
        due_at=now[0] + timedelta(hours=24), due_confirmed=True)
    team.save_projection(task, expected_revision=None)
    repository = module.NotificationRepository(team, clock=lambda: now[0])
    return SimpleNamespace(module=module, db=db, team=team, repo=repository,
        owner=owner, member=member, task=task, now=now)


def update(c, **changes):
    current = c.team.get_projection(c.owner.id, c.task.task_id)
    changed = current.model_copy(update={'revision': current.revision + 1, **changes})
    c.team.save_projection(changed, expected_revision=current.revision)
    return changed


def test_due_generation_ignores_unrelated_task_revision(case):
    c = case
    assert c.repo.due_revision(c.task.task_id) == 0
    update(c, title='Renamed', bucket='doing', remote_fingerprint='b' * 64)
    assert c.repo.due_revision(c.task.task_id) == 0


def test_due_generation_detects_away_then_back_between_plans(case):
    c = case
    update(c, due_at=c.task.due_at + timedelta(days=1))
    update(c, due_at=c.task.due_at)
    assert c.repo.due_revision(c.task.task_id) == 2
    with c.db.connection() as conn:
        generations = conn.execute('SELECT due_revision FROM notification_due_generations WHERE task_id=? ORDER BY due_revision',
                                   (c.task.task_id,)).fetchall()
    assert tuple(row[0] for row in generations) == (0, 1, 2)


def test_simultaneous_due_and_assignee_change_is_one_generation(case):
    c = case
    update(c, due_at=c.task.due_at + timedelta(days=1), assignee_id=c.owner.id)
    assert c.repo.due_revision(c.task.task_id) == 1


def test_clear_due_and_confirmation_changes_are_recorded(case):
    c = case
    update(c, due_at=None, due_confirmed=True)
    assert c.repo.due_revision(c.task.task_id) == 1
    update(c, due_confirmed=False)
    assert c.repo.due_revision(c.task.task_id) == 2


def source(c, *, resumed=False):
    sync = TeamSyncRepository(c.team, clock=lambda: c.now[0])
    claim = sync.begin('7')
    sync.commit(claim, tuple(SyncObservation(t.task_id, t.remote_fingerprint) for t in claim.projections),
                claim.projections, tuple(t.task_id for t in claim.projections))
    return c.repo.planning_snapshot(('7',), now=c.now[0], resumed=resumed)


def queued(c, rule='due_24h', *, resumed=False):
    if rule != 'daily_digest' and source(c).last_planned_at is None:
        c.now[0] -= timedelta(seconds=30)
        initial = source(c)
        c.repo.commit_plan(initial, (), planned_at=c.now[0])
        c.now[0] += timedelta(seconds=30)
    snapshot = source(c, resumed=resumed)
    intents = tuple(x for x in ReminderScheduler().plan(c.now[0], snapshot) if x.rule == rule)
    assert len(intents) == 1
    c.repo.commit_plan(snapshot, intents, planned_at=c.now[0])
    return snapshot, intents[0]


def sending(c, claim=None, text='Fresh verified task'):
    claim = claim or c.repo.claim('worker')
    authorized = c.repo.authorize(claim)
    body = BotSend(operation_id=claim.id, user_id=authorized.recipient.max_user_id, text=text)
    started = c.repo.mark_sending(claim, body,
        observed_fingerprints=tuple((task.task_id, task.remote_fingerprint) for task in authorized.tasks))
    return started, body


def test_plan_replay_keeps_one_immutable_intent_and_watermark(case):
    c = case
    snapshot, intent = queued(c)
    assert c.repo.commit_plan(snapshot, (intent,), planned_at=c.now[0]) == (intent,)
    assert c.repo.claim('first').id == intent.notification_id
    assert c.repo.claim('second') is None
    assert c.repo.planning_snapshot(('7',), now=c.now[0]).last_planned_at == c.now[0]


def test_plan_cas_rolls_back_when_task_or_member_changes(case):
    c = case
    snapshot = source(c)
    intents = tuple(ReminderScheduler().plan(c.now[0], snapshot))
    update(c, title='Changed while planning')
    with pytest.raises(NotificationError):
        c.repo.commit_plan(snapshot, intents, planned_at=c.now[0])
    assert c.repo.claim('worker') is None


def test_due_change_cancels_leased_old_single_notification(case):
    c = case
    _, intent = queued(c)
    claim = c.repo.claim('worker')
    update(c, due_at=c.task.due_at + timedelta(days=1))
    assert c.repo.read(intent.notification_id)['state'] == 'cancelled'
    with pytest.raises(NotificationError):
        c.repo.authorize(claim)
    assert c.repo.claim('other') is None


def test_prepare_expiry_can_resume_but_sending_expiry_is_uncertain(case):
    c = case
    _, intent = queued(c)
    first = c.repo.claim('first', lease_seconds=5)
    c.now[0] += timedelta(seconds=5)
    c.repo.recover_expired()
    second = c.repo.claim('second')
    assert second.id == first.id and second.fence > first.fence
    with pytest.raises(NotificationError):
        c.repo.authorize(first)
    started, _ = sending(c, second)
    c.now[0] = started.lease_until
    c.repo.recover_expired()
    assert c.repo.read(intent.notification_id)['state'] == 'uncertain'
    assert c.repo.claim('third') is None
    with pytest.raises(NotificationError):
        c.repo.finish(started, SendReceipt(started.id, 'sent', message_id='late'))
    assert c.repo.read(intent.notification_id)['state'] == 'uncertain'


def test_actual_send_body_is_frozen_only_before_post_and_renew_old_claim_works(case):
    c = case
    _, intent = queued(c)
    claim = c.repo.claim('worker')
    assert c.repo.read(intent.notification_id)['attempt'] == 0
    started, body = sending(c, claim, text='Actual literal body <script>')
    assert started.state == 'sending' and started.attempt == 1
    assert c.repo.renew(claim).state == 'sending'
    with c.db.connection() as conn:
        row = conn.execute('SELECT send_payload FROM notification_attempts WHERE notification_id=?', (started.id,)).fetchone()
        assert 'Actual literal body <script>' in row[0]
    c.repo.finish(started, SendReceipt(started.id, 'sent', message_id='provider-id'))
    result = c.repo.read(started.id)
    assert result['state'] == 'sent'
    assert 'read' not in result['receipt']


def test_mark_sending_rechecks_remote_proof_and_recipient_before_any_attempt(case):
    c = case
    _, intent = queued(c)
    claim = c.repo.claim('worker')
    body = BotSend(operation_id=claim.id, user_id=c.member.max_user_id, text='Fresh')
    with pytest.raises(NotificationError):
        c.repo.mark_sending(claim, body, observed_fingerprints=((c.task.task_id, 'f' * 64),))
    c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'max_user_id': '88'}), expected_revision=0)
    with pytest.raises(NotificationError):
        c.repo.mark_sending(claim, body, observed_fingerprints=((c.task.task_id, c.task.remote_fingerprint),))
    assert c.repo.read(intent.notification_id)['attempt'] == 0


def test_known_429_retries_are_bounded_and_each_actual_body_is_immutable(case):
    c = case
    _, intent = queued(c)
    for attempt in range(1, 4):
        started, _ = sending(c, text=f'Fresh attempt {attempt}')
        assert started.attempt == attempt
        c.repo.finish(started, SendReceipt(started.id, 'retryable', error_code='max_rate_limited', retry_after=10))
        assert c.repo.claim('too-early') is None
        c.now[0] += timedelta(seconds=10)
    assert c.repo.read(intent.notification_id)['state'] == 'failed'
    assert c.repo.claim('fourth') is None
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM notification_attempts').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM notification_receipts').fetchone()[0] == 3


def test_non_429_retryable_is_uncertain_never_auto_repeated(case):
    c = case
    _, intent = queued(c)
    started, _ = sending(c)
    c.repo.finish(started, SendReceipt(started.id, 'retryable', error_code='unknown_outcome'))
    assert c.repo.read(intent.notification_id)['state'] == 'uncertain'
    assert c.repo.claim('again') is None


def test_closed_digest_refs_are_dropped_without_discarding_current_other_tasks(case):
    c = case
    other = c.task.model_copy(update={'task_id': '99', 'title': 'Other'})
    c.team.save_projection(other, expected_revision=None)
    _, intent = queued(c, 'daily_digest')
    update(c, bucket='done')
    authorization = c.repo.authorize(c.repo.claim('worker'))
    assert [task.task_id for task in authorization.tasks] == ['99']
    assert c.repo.read(intent.notification_id)['intent'].task_refs == intent.task_refs


def test_restart_cancels_old_individual_backlog_and_keeps_one_current_digest(case):
    c = case
    _, original = queued(c)
    c.now[0] += timedelta(minutes=5)
    snapshot, digest = queued(c, 'daily_digest', resumed=True)
    assert c.repo.read(original.notification_id)['state'] == 'cancelled'
    assert c.repo.commit_plan(snapshot, (digest,), planned_at=c.now[0]) == (digest,)
    assert c.repo.claim('worker').id == digest.notification_id
    assert c.repo.claim('other') is None


def test_all_evidence_tables_reject_replace_update_and_delete(case):
    c = case
    queued(c)
    started, _ = sending(c)
    c.repo.finish(started, SendReceipt(started.id, 'sent', message_id='123'))
    tables = ('notification_due_generations', 'notification_intents', 'notification_task_refs',
              'notification_plan_runs', 'notification_attempts', 'notification_receipts')
    with c.db.connection() as conn:
        for table in tables:
            row = conn.execute(f'SELECT * FROM {table} LIMIT 1').fetchone()
            assert row
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'INSERT OR REPLACE INTO {table} VALUES({",".join("?" for _ in row)})', tuple(row))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'UPDATE {table} SET rowid=rowid')
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f'DELETE FROM {table}')


def test_morning_digest_coalesces_deferred_individual_with_continuous_watermark(case):
    c = case
    _, old = queued(c)
    claim = c.repo.claim('evening')
    morning = c.now[0].replace(hour=6) + timedelta(days=1)
    c.repo.defer(claim, available_at=morning, error_code='notification_quiet_hours')
    # Frequent planning overnight means the morning watermark gap is only 30s.
    c.now[0] = morning - timedelta(seconds=30)
    warm = source(c)
    c.repo.commit_plan(warm, (), planned_at=c.now[0])
    c.now[0] = morning
    _, digest = queued(c, 'daily_digest')
    assert c.repo.read(old.notification_id)['state'] == 'cancelled'
    assert c.repo.claim('morning').id == digest.notification_id
    assert c.repo.claim('other') is None


def test_morning_cancels_leased_preparing_but_never_erases_sending(case):
    c = case
    _, old = queued(c)
    started, _ = sending(c)
    c.now[0] += timedelta(seconds=30)
    queued(c, 'daily_digest', resumed=True)
    assert c.repo.read(old.notification_id)['state'] == 'sending'
    c.now[0] = started.lease_until
    c.repo.recover_expired()
    assert c.repo.read(old.notification_id)['state'] == 'uncertain'


def test_quiet_prepost_guard_does_not_consume_attempt(case):
    c = case
    queued(c)
    c.now[0] = c.now[0].replace(hour=18)  # 21:00 Moscow
    claim = c.repo.claim('worker')
    with pytest.raises(NotificationDeferred) as error:
        sending(c, claim)
    assert error.value.code == 'notification_quiet_hours'
    assert error.value.available_at.hour == 6
    assert c.repo.read(claim.id)['attempt'] == 0


def test_same_snapshot_concurrent_planners_commit_only_one_watermark(case):
    from concurrent.futures import ThreadPoolExecutor
    c = case
    snapshot = source(c)
    intents = ReminderScheduler().plan(c.now[0], snapshot)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = tuple(workers.map(lambda _: c.repo.commit_plan(snapshot, intents, planned_at=c.now[0]), range(2)))
    assert results[0] == results[1]
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM notification_plan_runs').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM notification_intents').fetchone()[0] == len(intents)


def test_same_logical_digest_reuses_original_after_title_and_verification_change(case):
    c = case
    _, original = queued(c, 'daily_digest')
    update(c, title='New title', remote_fingerprint='c' * 64)
    c.now[0] += timedelta(seconds=30)
    fresh = source(c, resumed=True)
    again = ReminderScheduler().plan(c.now[0], fresh)
    assert again[0].notification_id == original.notification_id
    assert again[0].canonical_verified_at != original.canonical_verified_at
    assert c.repo.commit_plan(fresh, again, planned_at=c.now[0]) == (original,)


def test_summary_uses_native_acceptance_time_and_exact_safe_shape(case):
    c = case
    queued(c)
    started, _ = sending(c)
    native = '2026-10-05T06:59:40Z'
    c.repo.finish(started, SendReceipt(started.id, 'sent', message_id='provider', accepted_at=native))
    summary = c.repo.summary(c.owner.id, '7', expected_actor_revision=0)
    assert summary == {'state':'observed','pending_count':0,'sending_count':0,'uncertain_count':0,
        'failed_count':0,'cancelled_count':0,'last_max_api_accepted_at':datetime.fromisoformat(native),
        'human_read_confirmed':None}
    with pytest.raises(TeamForbidden):
        c.repo.summary(c.member.id, '7')


def test_summary_orders_native_instants_with_mixed_fractional_formats(case):
    c = case
    for stamp in ('2026-10-05T07:00:00Z', '2026-10-05T07:00:00.234Z'):
        queued(c)
        started, _ = sending(c)
        c.repo.finish(started, SendReceipt(started.id,'sent',message_id=stamp,accepted_at=stamp))
        assert c.repo.read(started.id)['receipt']['accepted_at'] == stamp
        c.now[0] += timedelta(seconds=1)
        update(c, due_at=c.now[0]+timedelta(hours=24))
    assert c.repo.summary(c.owner.id,'7')['last_max_api_accepted_at'] == datetime.fromisoformat('2026-10-05T07:00:00.234Z')


def test_summary_does_not_attribute_task_message_to_unrelated_recipient_project(case):
    c = case
    for member in (c.owner,c.member):
        c.team.upsert_member(member.model_copy(update={'project_ids':('7','8'),'revision':1}), expected_revision=0)
    snapshot = source(c)
    intent = ReminderScheduler().plan(c.now[0], snapshot)[0]
    # Broad scope is legitimate planner metadata, but task evidence is only 7.
    sync = TeamSyncRepository(c.team, clock=lambda:c.now[0])
    run = sync.begin('8')
    sync.commit(run, (), (), ())
    snapshot = c.repo.planning_snapshot(('7','8'))
    intent = intent.model_copy(update={'project_ids':('7','8')})
    c.repo.commit_plan(snapshot,(intent,),planned_at=c.now[0])
    assert c.repo.summary(c.owner.id,'7')['pending_count'] == 1
    assert c.repo.summary(c.owner.id,'8')['pending_count'] == 0


def downgrade_to_six(db):
    with db.connection() as conn:
        conn.execute('DROP TRIGGER response_abort_guard')
        conn.execute('DROP TRIGGER abort_response_guard')
        conn.execute('DROP TABLE voice_request_aborts')
        conn.execute('DROP TABLE team_due_resolution_consumptions')
        conn.execute('DROP TABLE team_due_resolution_previews')
        conn.execute('DROP TRIGGER notification_projection_insert')
        conn.execute('DROP TRIGGER notification_projection_due_update')
        for table in ('notification_receipts','notification_attempts','notification_state','notification_task_refs',
            'notification_intents','notification_due_state','notification_due_generations','notification_plan_runs',
            'notification_planner_state'):
            conn.execute(f'DROP TABLE {table}')
        conn.execute('DELETE FROM team_schema WHERE version>6')


def test_genuine_six_upgrade_preserves_command_receipt_and_backfills_generation(case):
    c = case
    command = TaskCommand(operation_id=uid(),action='comment',project_id='7',task_id=c.task.task_id,
        expected_revision=0,expected_fingerprint=c.task.remote_fingerprint,values=TaskChange(comment='Historical result'))
    accepted = c.team.accept_command(c.owner.id, command)
    downgrade_to_six(c.db)
    reopened = TeamDatabase(c.db.path)
    team = TeamRepository(reopened, clock=lambda:c.now[0])
    repo = c.module.NotificationRepository(team,clock=lambda:c.now[0])
    assert team.get_receipt(c.owner.id,command.operation_id) == accepted
    assert repo.due_revision(c.task.task_id) == 0
    with reopened.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 9
        assert conn.execute('SELECT COUNT(*) FROM notification_due_generations').fetchone()[0] == 1
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    TeamDatabase(c.db.path)
    assert repo.due_revision(c.task.task_id) == 0


def test_failed_schema_seven_upgrade_rolls_back_all_new_tables(case, monkeypatch):
    c = case
    downgrade_to_six(c.db)
    monkeypatch.setattr(c.module,'NOTIFICATION_MIGRATION',c.module.NOTIFICATION_MIGRATION + ('INVALID SQL',))
    with pytest.raises(sqlite3.OperationalError):
        TeamDatabase(c.db.path)
    with c.db.connection() as conn:
        assert conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] == 6
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'notification_%'").fetchall() == []


def test_stale_or_unverified_snapshot_cannot_plan(case):
    c = case
    with pytest.raises(NotificationDeferred):
        c.repo.planning_snapshot(('7',))
    source(c)
    c.now[0] += timedelta(seconds=91)
    with pytest.raises(NotificationDeferred):
        c.repo.planning_snapshot(('7',))
