"""Pure current-state scheduling. No HTTP, database, provider or device access."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from secretary.domain.cloud_budget import APPROVED_MONTHLY_MICRO, MOSCOW, budget_period
from secretary.domain.notifications import (NotificationDeferred, NotificationError,
    NotificationIntent, NotificationTaskRef, ReminderSnapshot)
from secretary.domain.team import content_hash

UTC = timezone.utc
HEADERS = {'daily_digest': 'Сводка задач', 'due_24h': 'Напоминание о сроке задачи',
           'due_1h': 'Напоминание о сроке задачи', 'overdue': 'Задача просрочена'}


def aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise NotificationError('notification_time_invalid')
    return value.astimezone(UTC)


def quiet(value: datetime) -> bool:
    hour = aware(value).astimezone(MOSCOW).hour
    return hour >= 21 or hour < 9


def next_morning(value: datetime) -> datetime:
    local = aware(value).astimezone(MOSCOW)
    day = local.date() + (timedelta(days=1) if local.hour >= 21 else timedelta())
    return datetime.combine(day, time(9), MOSCOW).astimezone(UTC)


def identity(key: str) -> str:
    return str(uuid5(NAMESPACE_URL, 'secretary/notification/' + key))


class ReminderScheduler:
    def __init__(self, *, clock=None, max_staleness=timedelta(seconds=90),
                 max_gap=timedelta(seconds=90)):
        if not isinstance(max_staleness, timedelta) or max_staleness <= timedelta() or not isinstance(max_gap, timedelta) or max_gap <= timedelta():
            raise NotificationError('notification_configuration_invalid')
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_staleness = max_staleness
        self.max_gap = max_gap

    def plan(self, now: datetime | None, canonical_snapshots: ReminderSnapshot) -> list[NotificationIntent]:
        now = aware(self.clock() if now is None else now)
        if not isinstance(canonical_snapshots, ReminderSnapshot):
            raise NotificationError('notification_snapshot_invalid')
        source = canonical_snapshots
        age = now - aware(source.verified_at)
        if age < timedelta() or age > self.max_staleness:
            raise NotificationDeferred('notification_snapshot_stale')
        begin = aware(source.last_planned_at) if source.last_planned_at is not None else now - timedelta(seconds=30)
        if begin > now:
            raise NotificationError('notification_clock_regressed')
        coalesce = source.resumed or source.last_planned_at is None or now-begin > self.max_gap
        local = now.astimezone(MOSCOW)
        morning = datetime.combine(local.date(), time(9), MOSCOW).astimezone(UTC)
        daily_due = not quiet(now) and (coalesce or begin < morning <= now)
        members = {m.id:m for m in source.members if m.enabled}
        grouped = {}
        for item in sorted(source.tasks, key=lambda row: (len(row.snapshot.task_id), row.snapshot.task_id)):
            task = item.snapshot
            member = members.get(task.assignee_id)
            if task.bucket in {'done', 'cancelled'} or member is None or task.project_id not in member.project_ids:
                continue
            grouped.setdefault(member.id, []).append(item)
        result = []
        for recipient_id, rows in sorted(grouped.items()):
            member = members[recipient_id]
            scheduled = morning
            day = scheduled.astimezone(MOSCOW).date().isoformat()
            group_key = f'daily_digest:{recipient_id}:{day}'
            part_count = (len(rows) + 9) // 10
            for part in range(part_count if daily_due else 0):
                refs = tuple(NotificationTaskRef(project_id=r.snapshot.project_id,
                    task_id=r.snapshot.task_id, due_revision=r.due_revision) for r in rows[part*10:(part+1)*10])
                key = f'{group_key}:{part}'
                result.append(self._intent(source, member, key, 'daily_digest', scheduled, refs,
                    group_key=group_key, part_index=part, part_count=part_count))
            if coalesce or quiet(now):
                continue
            for item in rows:
                task = item.snapshot
                if not task.due_confirmed or task.due_at is None:
                    continue
                due = aware(task.due_at)
                for rule, delta in (('due_24h', timedelta(hours=24)), ('due_1h', timedelta(hours=1)), ('overdue', timedelta())):
                    event = due - delta
                    crossed = begin <= event < now if rule == 'overdue' else begin < event <= now
                    if not crossed or quiet(event) or event.astimezone(MOSCOW).time() == time(9):
                        continue
                    if rule != 'overdue' and due <= now:
                        continue
                    ref = NotificationTaskRef(project_id=task.project_id, task_id=task.task_id, due_revision=item.due_revision)
                    key = rule + ':' + content_hash({'project_id':task.project_id, 'task_id':task.task_id,
                        'due_revision':item.due_revision, 'recipient':recipient_id, 'rule':rule, 'scheduled':event.isoformat()})
                    result.append(self._intent(source, member, key, rule, event, (ref,)))
        result.extend(self._budget(source, now))
        return result

    @staticmethod
    def _intent(source, member, key, rule, scheduled, refs, *, group_key=None,
                part_index=0, part_count=1, budget_period_value=None, budget_threshold=None):
        return NotificationIntent(notification_id=identity(key), dedup_key=key,
            recipient_id=member.id, recipient_revision=member.revision, rule=rule,
            scheduled_at=scheduled, task_refs=refs,
            project_ids=tuple(sorted(set(source.project_ids) & set(member.project_ids))),
            canonical_verified_at=source.verified_at, text=HEADERS.get(rule, 'Предупреждение о месячном бюджете'),
            group_id=identity(group_key or key), part_index=part_index, part_count=part_count,
            budget_period=budget_period_value, budget_threshold=budget_threshold)

    def _budget(self, source, now):
        budget = source.budget
        if budget is None:
            return []
        amounts = (budget.approved_limit_micro, budget.effective_limit_micro, budget.confirmed_micro, budget.reserved_micro)
        if (any(type(v) is not int or v < 0 for v in amounts)
                or not 0 < budget.effective_limit_micro <= budget.approved_limit_micro <= APPROVED_MONTHLY_MICRO
                or budget.period != budget_period(now)):
            return []
        used = budget.confirmed_micro + budget.reserved_micro
        # A stable month anchor makes repeated observations the same logical alert.
        year, month = (int(part) for part in budget.period.split('-'))
        scheduled = datetime(year, month, 1, 1, tzinfo=MOSCOW).astimezone(UTC)
        result = []
        for member in sorted(source.members, key=lambda row: row.id):
            if not member.enabled or member.role != 'owner' or not set(member.project_ids) & set(source.project_ids):
                continue
            for threshold in (50, 80, 90):
                if used * 100 >= budget.effective_limit_micro * threshold:
                    key = f'budget:{member.id}:{budget.period}:{threshold}'
                    result.append(self._intent(source, member, key, f'budget_{threshold}', scheduled, (),
                        budget_period_value=budget.period, budget_threshold=threshold))
        return result
