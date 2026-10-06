"""Fresh, free canonical reads followed by one fenced, private MAX message.

The intent is a durable schedule, never an outbound body. ``before_send`` is
the repository's atomic mark_sending boundary; after it succeeds this method
owns exactly one POST and returns evidence, never an automatic retry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from secretary.application.reminders import aware, next_morning, quiet
from secretary.domain.bot import BotSend, SendReceipt
from secretary.domain.cloud_budget import APPROVED_MONTHLY_MICRO, BudgetSnapshot, MOSCOW, budget_period
from secretary.domain.notifications import (NotificationAuthorization, NotificationDeferred,
    NotificationError, NotificationIntent)
from secretary.domain.team import TaskSnapshot, TeamMember
from secretary.infrastructure.vikunja import TaskReadContext


class NotificationSender:
    def __init__(self, team, canonical_clients, max_client, *, clock=None, budget_reader=None):
        self.team, self.canonical_clients, self.max_client = team, dict(canonical_clients), max_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.budget_reader = budget_reader

    def _time_gate(self, intent, cancelled):
        now = aware(self.clock())
        if cancelled():
            raise NotificationDeferred('notification_not_ready', now + timedelta(seconds=30))
        if quiet(now):
            raise NotificationDeferred('notification_quiet_hours', next_morning(now))
        if now < intent.scheduled_at:
            raise NotificationDeferred('notification_not_ready', intent.scheduled_at)
        if (not intent.rule.startswith('budget_')
                and intent.scheduled_at.astimezone(MOSCOW).date() != now.astimezone(MOSCOW).date()):
            raise NotificationError('notification_stale')
        return now

    def _authorization(self, intent, authorize):
        try:
            value = authorize()
            if not isinstance(value, NotificationAuthorization):
                raise ValueError
            value = NotificationAuthorization.model_validate(value.model_dump())
            member = self.team.get_member(intent.recipient_id)
            if not isinstance(member, TeamMember):
                raise ValueError
            member = TeamMember.model_validate(member.model_dump())
            if (value.claim.intent != intent or value.claim.id != intent.notification_id
                    or value.claim.state != 'preparing' or member != value.recipient
                    or not member.enabled or member.revision != intent.recipient_revision
                    or not set(intent.project_ids).issubset(member.project_ids)):
                raise ValueError
            if not intent.rule.startswith('budget_'):
                if not value.tasks:
                    raise NotificationError('notification_empty')
                if intent.rule != 'daily_digest' and len(value.tasks) != 1:
                    raise NotificationError('notification_stale')
            return value
        except NotificationError:
            raise
        except Exception:
            raise NotificationError('notification_forbidden') from None

    def _budget(self, intent, now):
        if intent.budget_period != budget_period(now):
            raise NotificationError('notification_stale')
        if self.budget_reader is None:
            raise NotificationDeferred('notification_not_ready', now + timedelta(seconds=60))
        try:
            value = self.budget_reader()
        except Exception:
            raise NotificationDeferred('notification_not_ready', now + timedelta(seconds=60)) from None
        if not isinstance(value, BudgetSnapshot):
            raise NotificationDeferred('notification_not_ready', now + timedelta(seconds=60))
        amounts = (value.approved_limit_micro, value.effective_limit_micro,
                   value.confirmed_micro, value.reserved_micro)
        if (any(type(x) is not int or x < 0 for x in amounts)
                or not 0 < value.effective_limit_micro <= value.approved_limit_micro <= APPROVED_MONTHLY_MICRO
                or value.period != intent.budget_period or value.period != budget_period(now)
                or (value.confirmed_micro + value.reserved_micro) * 100 < value.effective_limit_micro * intent.budget_threshold):
            # Released reservations can cross the same threshold again later.
            # Preserve its immutable slot until a current proof permits sending.
            raise NotificationDeferred('notification_not_ready', now + timedelta(seconds=60))

    @staticmethod
    def _tasks_key(tasks):
        return tuple(sorted((task.task_id, task.model_dump_json()) for task in tasks))

    def _fresh_tasks(self, authorization, cancelled):
        fresh = []
        for task in authorization.tasks:
            self._time_gate(authorization.claim.intent, cancelled)
            try:
                client = self.canonical_clients[task.project_id]
                mapped = client.members.get_by_vikunja_id(authorization.recipient.vikunja_user_id)
                member = authorization.recipient
                # A stale provider directory must not silently reinterpret an assignee.
                if (client.binding.project_id != task.project_id or mapped is None
                        or (mapped.id, mapped.max_user_id, mapped.vikunja_user_id, mapped.revision,
                            mapped.enabled, mapped.project_ids) !=
                           (member.id, member.max_user_id, member.vikunja_user_id, member.revision,
                            member.enabled, member.project_ids)):
                    raise NotificationError('notification_forbidden')
                observed = client.get_task(task.task_id,
                    context=TaskReadContext(revision=task.revision, baseline=task), conditional=False)
                if not isinstance(observed, TaskSnapshot) or observed != task:
                    raise ValueError
                fresh.append(observed)
            except NotificationError:
                raise
            except Exception:
                raise NotificationDeferred('notification_sync_stale', aware(self.clock()) + timedelta(seconds=30)) from None
        return tuple(fresh)

    @staticmethod
    def _render(intent, tasks, now):
        if intent.rule.startswith('budget_'):
            return (f'Бюджет Polza за {intent.budget_period}: достигнут порог {intent.budget_threshold}% '
                    'с учётом подтверждённых расходов и резервов.')
        if intent.rule != 'daily_digest':
            task = tasks[0]
            if not task.due_confirmed or task.due_at is None:
                raise NotificationError('notification_stale')
            if ((intent.rule == 'overdue' and task.due_at >= now)
                    or (intent.rule != 'overdue' and task.due_at <= now)):
                raise NotificationError('notification_stale')
        header = 'Сводка задач' if intent.rule == 'daily_digest' else (
            'Задача просрочена' if intent.rule == 'overdue' else 'Напоминание о сроке задачи')
        if intent.part_count > 1:
            header += f' — часть {intent.part_index + 1}/{intent.part_count}'
        lines = []
        for task in tasks:
            date = (task.due_at.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M') + ' МСК'
                    if task.due_confirmed and task.due_at is not None else 'срок не указан')
            suffix = f' [#{task.task_id}] · {date}'
            # Bound the complete row, retaining exact decimal ID and confirmed date.
            title = ' '.join(task.title.split())
            room = 250 - len(suffix)
            title = title if len(title) <= room else title[:room - 1] + '…'
            lines.append(title + suffix)
        text = header + '\n' + '\n'.join(lines)
        if len(text) > 3500 or not lines:
            raise NotificationError('notification_corrupt')
        return text

    def dispatch(self, intent, *, authorize, before_send, cancelled=None) -> SendReceipt:
        if not isinstance(intent, NotificationIntent) or not callable(authorize) or not callable(before_send):
            raise NotificationError('notification_configuration_invalid')
        try:
            intent = NotificationIntent.model_validate(intent.model_dump())
        except Exception:
            raise NotificationError('notification_corrupt') from None
        cancelled = cancelled or (lambda: False)
        now = self._time_gate(intent, cancelled)
        initial = self._authorization(intent, authorize)
        if intent.rule.startswith('budget_'):
            self._budget(intent, now)
            tasks = ()
        else:
            tasks = self._fresh_tasks(initial, cancelled)
        now = self._time_gate(intent, cancelled)
        current = self._authorization(intent, authorize)
        if (current.recipient != initial.recipient
                or self._tasks_key(current.tasks) != self._tasks_key(tasks)):
            raise NotificationDeferred('notification_sync_stale', now + timedelta(seconds=30))
        if intent.rule.startswith('budget_'):
            self._budget(intent, now)
        text = self._render(intent, tasks, now)
        send = BotSend(operation_id=intent.notification_id, user_id=current.recipient.max_user_id, text=text)
        self._time_gate(intent, cancelled)
        proof = tuple((task.task_id, task.remote_fingerprint) for task in tasks)
        before_send(send, proof)
        # The durable commitment above owns this POST even if shutdown now begins.
        try:
            receipt = self.max_client.send_message(send)
            if not isinstance(receipt, SendReceipt) or receipt.operation_id != send.operation_id:
                raise ValueError
            return receipt
        except Exception:
            return SendReceipt(send.operation_id, 'uncertain', error_code='notification_send_uncertain')
