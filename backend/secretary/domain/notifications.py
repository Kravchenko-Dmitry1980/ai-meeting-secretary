"""T10 immutable plans; intent text is a preview, never an outbound MAX body."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from secretary.domain.bot import BotError
from secretary.domain.cloud_budget import BudgetSnapshot
from secretary.domain.team import DecimalID, Revision, TaskSnapshot, TeamDTO, TeamMember, Text, UUIDString

NotificationRule = Literal['daily_digest', 'due_24h', 'due_1h', 'overdue',
                           'budget_50', 'budget_80', 'budget_90']


class NotificationError(BotError):
    """Sanitized error at the planner/outbox/sender boundary."""


class NotificationDeferred(NotificationError):
    def __init__(self, code: str, available_at: datetime | None = None):
        super().__init__(code)
        if available_at is not None and (available_at.tzinfo is None or available_at.utcoffset() is None):
            raise NotificationError('notification_time_invalid')
        self.available_at = available_at


class ReminderTask(TeamDTO):
    snapshot: TaskSnapshot
    due_revision: Revision


class ReminderSnapshot(TeamDTO):
    tasks: Annotated[tuple[ReminderTask, ...], Field(max_length=10000)]
    members: Annotated[tuple[TeamMember, ...], Field(max_length=10)]
    verified_at: AwareDatetime
    project_ids: Annotated[tuple[DecimalID, ...], Field(min_length=1, max_length=10)]
    last_planned_at: AwareDatetime | None = None
    resumed: StrictBool = False
    budget: BudgetSnapshot | None = None

    @model_validator(mode='after')
    def scope(self):
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError('notification_scope_invalid')
        ids = [item.snapshot.task_id for item in self.tasks]
        if len(set(ids)) != len(ids) or len({m.id for m in self.members}) != len(self.members):
            raise ValueError('notification_duplicate_identity')
        if any(item.snapshot.project_id not in self.project_ids for item in self.tasks):
            raise ValueError('notification_scope_invalid')
        return self


class NotificationTaskRef(TeamDTO):
    project_id: DecimalID
    task_id: DecimalID
    due_revision: Revision


class NotificationIntent(TeamDTO):
    notification_id: UUIDString
    dedup_key: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    recipient_id: UUIDString
    recipient_revision: Revision
    rule: NotificationRule
    scheduled_at: AwareDatetime
    task_refs: Annotated[tuple[NotificationTaskRef, ...], Field(max_length=10)]
    project_ids: Annotated[tuple[DecimalID, ...], Field(min_length=1, max_length=10)]
    canonical_verified_at: AwareDatetime
    text: Annotated[str, Field(strict=True, min_length=1, max_length=3500)]
    group_id: UUIDString
    part_index: Revision = 0
    part_count: Annotated[int, Field(strict=True, ge=1)] = 1
    budget_period: Annotated[str, Field(strict=True, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')] | None = None
    budget_threshold: Literal[50, 80, 90] | None = None

    @model_validator(mode='after')
    def contract(self):
        if self.part_index >= self.part_count or len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError('notification_part_invalid')
        if len({ref.task_id for ref in self.task_refs}) != len(self.task_refs):
            raise ValueError('notification_duplicate_identity')
        if any(ref.project_id not in self.project_ids for ref in self.task_refs):
            raise ValueError('notification_scope_invalid')
        if self.rule.startswith('budget_'):
            if self.task_refs or self.budget_period is None or self.budget_threshold != int(self.rule.split('_')[1]):
                raise ValueError('notification_budget_invalid')
        elif self.budget_period is not None or self.budget_threshold is not None:
            raise ValueError('notification_budget_invalid')
        elif not self.task_refs or (self.rule != 'daily_digest' and len(self.task_refs) != 1):
            raise ValueError('notification_task_required')
        if '\x00' in self.text or not self.text.strip():
            raise ValueError('notification_text_invalid')
        return self


class NotificationClaim(TeamDTO):
    id: UUIDString
    intent: NotificationIntent
    worker_id: Text
    fence: Revision
    lease_until: AwareDatetime
    attempt: Revision = 0
    state: Literal['preparing', 'sending'] = 'preparing'

    @model_validator(mode='after')
    def identity(self):
        if self.id != self.intent.notification_id:
            raise ValueError('notification_claim_invalid')
        return self


class NotificationAuthorization(TeamDTO):
    claim: NotificationClaim
    recipient: TeamMember
    tasks: tuple[TaskSnapshot, ...]

    @model_validator(mode='after')
    def scope(self):
        intent = self.claim.intent
        if (self.recipient.id != intent.recipient_id or not self.recipient.enabled
                or self.recipient.revision != intent.recipient_revision):
            raise ValueError('notification_recipient_changed')
        refs = {(ref.project_id, ref.task_id) for ref in intent.task_refs}
        if any((task.project_id, task.task_id) not in refs or task.assignee_id != self.recipient.id
               or task.bucket in {'done', 'cancelled'} for task in self.tasks):
            raise ValueError('notification_task_changed')
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError('notification_duplicate_identity')
        if intent.rule.startswith('budget_') and (self.tasks or self.recipient.role != 'owner'):
            raise ValueError('notification_budget_owner_required')
        return self
