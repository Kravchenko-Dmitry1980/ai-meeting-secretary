"""T10 pure scheduling: synthetic people/tasks, no provider or owner state."""
from __future__ import annotations

import importlib
import importlib.util
from datetime import datetime, timedelta, timezone

import pytest

from secretary.domain.cloud_budget import APPROVED_MONTHLY_MICRO, BudgetSnapshot
from secretary.domain.team import TaskSnapshot, TeamMember

OWNER = '10000000-0000-4000-8000-000000000001'
MEMBER = '10000000-0000-4000-8000-000000000002'
MOSCOW = timezone(timedelta(hours=3))


def modules():
    names = ('secretary.domain.notifications', 'secretary.application.reminders')
    for name in names:
        assert importlib.util.find_spec(name) is not None, f'T10 implementation absent: {name}'
    return tuple(importlib.import_module(name) for name in names)


def at(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def member(identifier=MEMBER, **changes):
    return TeamMember(**{'id':identifier, 'display_name':'Синтетический участник', 'role':'member',
        'max_user_id':'11' if identifier == MEMBER else '12', 'vikunja_user_id':'21',
        'project_ids':('7',), **changes})


def task(identifier='50', **changes):
    return TaskSnapshot(**{'task_id':identifier, 'project_id':'7', 'revision':4,
        'remote_fingerprint':'a' * 64, 'title':'Проверить синтетический отчёт', 'assignee_id':MEMBER,
        'bucket':'accepted', 'due_confirmed':True, **changes})


def snapshot(now, tasks=(), *, members=None, last=None, resumed=False, verified=None, budget=None):
    domain, _ = modules()
    return domain.ReminderSnapshot(tasks=tuple(domain.ReminderTask(snapshot=t, due_revision=g)
        for t, g in tasks), members=tuple(members if members is not None else (member(),)),
        verified_at=verified or now, project_ids=('7',), last_planned_at=last,
        resumed=resumed, budget=budget)


def plan(now, tasks=(), **kwargs):
    _, application = modules()
    return application.ReminderScheduler().plan(now, snapshot(now, tasks, **kwargs))


@pytest.mark.parametrize(('rule', 'hours'), [('due_24h', 24), ('due_1h', 1), ('overdue', 0)])
def test_due_rules_cross_once_with_stable_binding_key(rule, hours):
    now = at('2026-10-04T15:00:00+03:00')
    due = now + timedelta(hours=hours) if hours else now-timedelta(seconds=1)
    values = ((task(due_at=due), 2),)
    result = plan(now, values, last=now-timedelta(seconds=30))
    candidates = [i for i in result if i.rule == rule]
    assert len(candidates) == 1
    assert candidates[0].task_refs[0].due_revision == 2
    assert candidates[0].recipient_id == MEMBER
    assert candidates[0].scheduled_at == (now if hours else due)
    repeated = plan(now, values, last=now-timedelta(seconds=30))
    assert [i.notification_id for i in repeated] == [i.notification_id for i in result]


def test_restart_sends_one_current_digest_not_backlog():
    now = at('2026-11-01T11:00:00+03:00')
    values = tuple((task(str(n), due_at=now-timedelta(days=n)), 1) for n in range(1, 8))
    result = plan(now, values, last=now-timedelta(days=5), resumed=True)
    assert len(result) == 1
    assert result[0].rule == 'daily_digest'
    assert result[0].scheduled_at == at('2026-11-01T09:00:00+03:00')
    assert len(result[0].task_refs) == 7
    assert '2026-10-27' not in result[0].dedup_key


def test_large_gap_coalesces_without_explicit_restart_flag():
    now = at('2026-10-04T15:00:00+03:00')
    result = plan(now, ((task(due_at=now-timedelta(seconds=20)), 1),), last=now-timedelta(hours=2))
    assert [i.rule for i in result] == ['daily_digest']


def test_first_tick_without_durable_cursor_is_current_digest_only():
    now = at('2026-10-04T15:00:00+03:00')
    result = plan(now, ((task(due_at=now-timedelta(seconds=1)), 1),))
    assert [i.rule for i in result] == ['daily_digest']


def test_exact_deadline_not_overdue_next_tick_at_equal_cursor_sends_once():
    due = at('2026-10-04T15:00:00+03:00')
    rows = ((task(due_at=due), 1),)
    exact = plan(due, rows, last=due-timedelta(seconds=30))
    after = plan(due+timedelta(seconds=30), rows, last=due)
    assert 'overdue' not in [i.rule for i in exact]
    assert [i.rule for i in after].count('overdue') == 1
    assert next(i for i in after if i.rule == 'overdue').scheduled_at == due


@pytest.mark.parametrize(('value', 'scheduled'), [
    ('2026-10-31T20:59:59+03:00', '2026-10-31T09:00:00+03:00'),
    ('2026-10-31T21:00:00+03:00', None),
    ('2026-11-01T08:59:59+03:00', None),
    ('2026-11-01T09:00:00+03:00', '2026-11-01T09:00:00+03:00'),
])
def test_quiet_hours_digest_schedule_moscow_and_month_boundary(value, scheduled):
    now = at(value)
    result = plan(now, ((task(due_at=now), 1),), last=now-timedelta(seconds=30), resumed=True)
    if scheduled is None:
        assert result == []
        return
    assert [i.rule for i in result] == ['daily_digest']
    assert result[0].scheduled_at == at(scheduled)


def test_quiet_due_events_merge_with_daily_digest_instead_of_extra_packets():
    now = at('2026-10-04T22:00:00+03:00')
    result = plan(now, ((task('50', due_at=now+timedelta(hours=1)), 1),
                        (task('51', due_at=now), 2)), last=now-timedelta(seconds=30))
    assert result == []
    morning = at('2026-10-05T09:00:00+03:00')
    fresh = plan(morning, ((task('50', due_at=now+timedelta(hours=1)), 1),
                          (task('51', due_at=now), 2), (task('52'), 1)), last=morning-timedelta(seconds=30))
    assert len(fresh) == 1 and fresh[0].rule == 'daily_digest'
    assert fresh[0].scheduled_at == morning
    assert {ref.task_id for ref in fresh[0].task_refs} == {'50','51','52'}


def test_normal_day_ticks_do_not_repeat_daily_planning_or_freeze_tomorrow_refs():
    now = at('2026-10-04T15:00:00+03:00')
    assert not plan(now, ((task(), 1),), last=now-timedelta(seconds=30))
    night = at('2026-10-04T21:00:00+03:00')
    assert not plan(night, ((task(), 1),), resumed=True)


def test_no_due_is_digest_only_and_closed_unassigned_revoked_out_of_scope_are_absent():
    now = at('2026-10-04T15:00:00+03:00')
    rows = ((task('50'), 1), (task('51', due_at=now, bucket='done'), 1),
            (task('52', due_at=now, bucket='cancelled'), 1),
            (task('53', due_at=now, assignee_id=None), 1))
    result = plan(now, rows)
    assert [i.rule for i in result] == ['daily_digest']
    assert [r.task_id for r in result[0].task_refs] == ['50']
    assert not plan(now, rows, members=(member(enabled=False),))
    assert not plan(now, rows, members=(member(project_ids=('8',)),))


def test_due_generation_changes_key_but_general_task_revision_and_title_do_not():
    now = at('2026-10-04T15:00:00+03:00')
    original = task(due_at=now-timedelta(seconds=1))
    changed = original.model_copy(update={'revision':8, 'title':'Новое название', 'bucket':'doing'})
    get = lambda t, g: next(i for i in plan(now, ((t, g),), last=now-timedelta(seconds=30)) if i.rule == 'overdue')
    first, renamed, new_generation = get(original, 3), get(changed, 3), get(changed, 4)
    assert first.dedup_key == renamed.dedup_key
    assert first.notification_id == renamed.notification_id
    assert first.dedup_key != new_generation.dedup_key
    assert original.title not in first.text and changed.title not in renamed.text


def test_missing_polza_key_does_not_stop_reminders(monkeypatch):
    monkeypatch.delenv('POLZA_API_KEY', raising=False)
    now = at('2026-10-04T15:00:00+03:00')
    result = plan(now, ((task(due_at=now-timedelta(seconds=1)), 1),), last=now-timedelta(seconds=30))
    assert {i.rule for i in result} == {'overdue'}


def test_task_staleness_defers_wrong_notification():
    domain, application = modules()
    now = at('2026-10-04T15:00:00+03:00')
    stale = snapshot(now, ((task(due_at=now), 1),), verified=now-timedelta(seconds=91))
    with pytest.raises(domain.NotificationDeferred, match='notification_snapshot_stale'):
        application.ReminderScheduler().plan(now, stale)


def test_digest_pages_are_bounded_and_stable_no_mutable_title_in_plan_payload():
    now = at('2026-10-04T09:00:00+03:00')
    rows = tuple((task(str(n), title='Я'*4000), 1) for n in range(1, 24))
    result = plan(now, rows, resumed=True)
    assert len(result) == 3
    assert {i.part_count for i in result} == {3}
    assert {i.part_index for i in result} == {0, 1, 2}
    assert len({i.group_id for i in result}) == 1
    assert all(len(i.task_refs) <= 10 and len(i.text) <= 3500 for i in result)
    assert all('Я' not in i.text for i in result)


def test_budget_owner_thresholds_use_effective_cap_confirmed_plus_reserved():
    now = at('2026-10-04T15:00:00+03:00')
    owners = (member(OWNER, role='owner'), member())
    budget = BudgetSnapshot('2026-10', APPROVED_MONTHLY_MICRO, APPROVED_MONTHLY_MICRO,
        1_400_000_000, 1_300_000_000, 300_000_000, None)
    result = plan(now, (), members=owners, budget=budget)
    assert [i.rule for i in result] == ['budget_50', 'budget_80', 'budget_90']
    assert {i.recipient_id for i in result} == {OWNER}
    assert {i.budget_period for i in result} == {'2026-10'}
    assert [i.budget_threshold for i in result] == [50, 80, 90]
    assert not plan(now, (), members=owners)


def test_budget_alerts_have_period_keys_not_volatile_observation_time():
    first = at('2026-10-04T15:00:00+03:00')
    budget = BudgetSnapshot('2026-10', APPROVED_MONTHLY_MICRO, 1_000_000_000,
        500_000_000, 0, 500_000_000, None)
    owners = (member(OWNER, role='owner'),)
    left = plan(first, (), members=owners, budget=budget)
    right = plan(first+timedelta(days=1), (), members=owners, budget=budget)
    assert len(left) == len(right) == 1
    assert left[0].notification_id == right[0].notification_id
    assert left[0].dedup_key == right[0].dedup_key


def test_approved_external_cost_deduction_keeps_effective_budget_alerts():
    now = at('2026-10-04T15:00:00+03:00')
    budget = BudgetSnapshot('2026-10', 2_900_000_000, 1_000_000_000,
        500_000_000, 0, 500_000_000, None)
    result = plan(now, (), members=(member(OWNER, role='owner'),), budget=budget)
    assert [i.rule for i in result] == ['budget_50']


@pytest.mark.parametrize(('used', 'expected'), [
    (499_999_999, []), (500_000_000, ['budget_50']),
    (799_999_999, ['budget_50']), (800_000_000, ['budget_50','budget_80']),
    (899_999_999, ['budget_50','budget_80']), (900_000_000, ['budget_50','budget_80','budget_90']),
])
def test_budget_thresholds_are_exact_integer_money_not_rounded_percent(used, expected):
    now = at('2026-10-04T15:00:00+03:00')
    budget = BudgetSnapshot('2026-10', APPROVED_MONTHLY_MICRO, 1_000_000_000,
        used, 0, max(0,1_000_000_000-used), None)
    result = plan(now, (), members=(member(OWNER, role='owner'),), budget=budget)
    assert [i.rule for i in result] == expected


@pytest.mark.parametrize(('value', 'period', 'expected'), [
    ('2026-11-01T00:59:59+03:00', '2026-10', True),
    ('2026-11-01T01:00:00+03:00', '2026-10', False),
    ('2026-11-01T01:00:00+03:00', '2026-11', True),
])
def test_budget_period_obeys_moscow_provider_one_am_reset(value, period, expected):
    now = at(value)
    budget = BudgetSnapshot(period, APPROVED_MONTHLY_MICRO, APPROVED_MONTHLY_MICRO,
        1_500_000_000, 0, 1_500_000_000, None)
    result = plan(now, (), members=(member(OWNER, role='owner'),), budget=budget)
    assert bool(result) is expected


def test_invalid_or_zero_budget_does_not_disable_free_task_reminder():
    now = at('2026-10-04T15:00:00+03:00')
    budget = BudgetSnapshot('2026-10', APPROVED_MONTHLY_MICRO, 0, 0, 0, 0, 'monthly_budget_exhausted')
    result = plan(now, ((task(due_at=now-timedelta(seconds=1)), 1),),
        last=now-timedelta(seconds=30), budget=budget)
    assert [i.rule for i in result] == ['overdue']


def test_due_recipient_change_and_generation_aba_never_reuse_old_event_identity():
    now = at('2026-10-04T15:00:00+03:00')
    current = task(due_at=now-timedelta(seconds=1))
    owners = (member(), member(OWNER, role='owner'))
    get = lambda t, g: next(i for i in plan(now, ((t,g),), members=owners,
        last=now-timedelta(seconds=30)) if i.rule == 'overdue')
    original = get(current,0)
    reassigned = get(current.model_copy(update={'assignee_id':OWNER}),1)
    returned = get(current,2)
    assert len({original.notification_id,reassigned.notification_id,returned.notification_id}) == 3
    assert reassigned.recipient_id == OWNER
    assert returned.recipient_id == original.recipient_id


def test_shared_domain_limits_and_unknown_error_are_safe():
    domain, _ = modules()
    now = at('2026-10-04T09:00:00+03:00')
    candidate = plan(now, ((task(),1),), resumed=True)[0]
    with pytest.raises(ValueError):
        domain.NotificationIntent.model_validate({**candidate.model_dump(), 'task_refs':candidate.task_refs*11})
    with pytest.raises(ValueError):
        domain.ReminderSnapshot(tasks=(),members=(),project_ids=('7','7'),verified_at=now)
    assert domain.NotificationError('secret https://invalid.example').code == 'bot_contract_invalid'


def test_injected_clock_and_naive_or_backward_time_fail_closed():
    domain, application = modules()
    now = at('2026-10-04T15:00:00+03:00')
    batch = snapshot(now, ((task(due_at=now-timedelta(seconds=1)), 1),), last=now-timedelta(seconds=30))
    assert application.ReminderScheduler(clock=lambda: now).plan(None, batch)
    with pytest.raises(domain.NotificationError):
        application.ReminderScheduler().plan(now.replace(tzinfo=None), batch)
    with pytest.raises(domain.NotificationError):
        application.ReminderScheduler().plan(now, batch.model_copy(update={'last_planned_at':now+timedelta(seconds=1)}))
