"""Team boundary DTOs. External numeric identifiers stay exact decimal strings."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BeforeValidator, ConfigDict, Field, StrictBool, field_validator, model_validator

from secretary.domain.speakers import DTO


def uuid_string(value):
    if not isinstance(value, str):
        raise ValueError('UUID must be a string')
    return str(UUID(value))


UUIDString = Annotated[str, BeforeValidator(uuid_string)]
DecimalID = Annotated[str, Field(strict=True, pattern=r'^[1-9][0-9]*$', max_length=128)]
Revision = Annotated[int, Field(strict=True, ge=0)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=4000, pattern=r'\S')]
Digest = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
Bucket = Literal['inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled']
ExecutionStatus = Literal['queued', 'running', 'reconciling', 'applied', 'conflict', 'uncertain', 'rejected']


class TeamConflict(ValueError):
    """Map to 409 at an HTTP boundary; do not expose stored payloads."""


class TeamForbidden(ValueError):
    """Map to 403/404 without including foreign task data."""


class TeamDTO(DTO):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)

    @field_validator('*')
    @classmethod
    def utc_dates(cls, value):
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        return value


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_hash(value) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


class TeamMember(TeamDTO):
    id: UUIDString
    display_name: Text
    role: Literal['owner', 'member'] = 'member'
    enabled: StrictBool = True
    max_user_id: DecimalID
    vikunja_user_id: DecimalID
    person_profile_id: str | None = None
    project_ids: tuple[DecimalID, ...] = ()
    revision: Revision = 0

    @field_validator('project_ids')
    @classmethod
    def unique_projects(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('duplicate_project')
        return tuple(sorted(value))


class TaskOrigin(TeamDTO):
    source_kind: Literal['manual', 'meeting', 'max'] = 'manual'
    publication_id: UUIDString | None = None
    meeting_id: Text | None = None
    transcript_version: Revision | None = None
    summary_version: Revision | None = None
    action_id: Text | None = None

    @model_validator(mode='after')
    def source_scope(self):
        values = (self.meeting_id, self.transcript_version, self.summary_version, self.action_id)
        if self.source_kind == 'meeting' and (any(v is None for v in values) or self.publication_id is None):
            raise ValueError('meeting_origin_requires_full_scope')
        if self.source_kind != 'meeting' and any(v is not None for v in values):
            raise ValueError('non_meeting_origin_has_meeting_scope')
        return self


class TaskSnapshot(TeamDTO):
    task_id: DecimalID
    project_id: DecimalID
    revision: Revision
    remote_fingerprint: Digest
    title: Text
    description: str = ''
    assignee_id: UUIDString | None = None
    bucket: Bucket = 'inbox'
    important: StrictBool | None = None
    urgent: StrictBool | None = None
    classification_confirmed: StrictBool = False
    due_at: AwareDatetime | None = None
    due_phrase: str | None = None
    due_timezone: Literal['Europe/Moscow'] = 'Europe/Moscow'
    due_confirmed: StrictBool = False
    origin: TaskOrigin | None = None

    @model_validator(mode='after')
    def confirmed_fields(self):
        if self.classification_confirmed and (self.important is None or self.urgent is None):
            raise ValueError('confirmed_classification_requires_both_axes')
        if self.due_at is not None and not self.due_confirmed:
            raise ValueError('stored_due_requires_confirmation')
        return self


class TaskChange(TeamDTO):
    title: Text | None = None
    description: str | None = None
    assignee_id: UUIDString | None = None
    bucket: Bucket | None = None
    important: StrictBool | None = None
    urgent: StrictBool | None = None
    classification_confirmed: StrictBool | None = None
    due_at: AwareDatetime | None = None
    due_phrase: str | None = None
    due_timezone: Literal['Europe/Moscow'] = 'Europe/Moscow'
    due_confirmed: StrictBool | None = None
    reason: Text | None = None
    result: Text | None = None
    comment: Text | None = None


class TaskCommand(TeamDTO):
    operation_id: UUIDString
    project_id: DecimalID
    task_id: DecimalID | None = None
    expected_revision: Revision | None = None
    expected_fingerprint: Digest | None = None
    expected_assignee_revision: Revision | None = None
    action: Literal['create', 'set_state', 'assign', 'set_due', 'resolve_due', 'classify', 'comment', 'propose_due', 'rename', 'link']
    values: TaskChange
    origin: TaskOrigin | None = None
    resolution_id: UUIDString | None = None

    @model_validator(mode='after')
    def command_contract(self):
        if (self.action == 'resolve_due') != (self.resolution_id is not None):
            raise ValueError('resolution_id_requires_resolution_action')
        if self.action == 'resolve_due' and self.origin is not None:
            raise ValueError('resolution_has_no_external_origin')
        if self.expected_assignee_revision is not None and self.action not in {'create', 'assign'}:
            raise ValueError('assignee_revision_requires_assignment')
        if self.action == 'create':
            if self.task_id is not None or self.expected_revision is not None or self.expected_fingerprint is not None:
                raise ValueError('create_has_no_existing_task_revision')
        elif self.task_id is None or self.expected_revision is None or self.expected_fingerprint is None:
            raise ValueError('mutation_requires_task_revision_and_fingerprint')
        due = {'due_at', 'due_phrase', 'due_timezone', 'due_confirmed'}
        axes = {'important', 'urgent', 'classification_confirmed'}
        allowed = {'create': {'title', 'description', 'assignee_id'} | due | axes,
                   'set_state': {'bucket', 'result'}, 'assign': {'assignee_id'},
                   'set_due': due | {'reason'}, 'resolve_due': {'due_at', 'due_confirmed', 'reason'},
                   'propose_due': due | {'reason'},
                    'classify': axes, 'comment': {'comment'}, 'rename': {'title'}, 'link': set()}
        if self.values.model_fields_set - allowed[self.action]:
            raise ValueError('fields_not_allowed_for_action')
        required = {'create': ('title', 'assignee_id'), 'set_state': ('bucket',),
                    'assign': ('assignee_id',), 'set_due': ('reason', 'due_confirmed'),
                    'resolve_due': ('reason', 'due_confirmed'),
                    'propose_due': ('reason', 'due_confirmed'),
                    'classify': ('important', 'urgent', 'classification_confirmed'),
                    'comment': ('comment',), 'rename': ('title',), 'link': ()}
        if any(getattr(self.values, key) is None for key in required[self.action]):
            raise ValueError('required_action_value_missing')
        if self.action in {'set_due', 'resolve_due', 'propose_due'} and 'due_at' not in self.values.model_fields_set:
            raise ValueError('due_at_must_be_explicit_even_to_clear')
        if ('due_at' in self.values.model_fields_set and self.values.due_confirmed is not True
                or self.action == 'classify' and self.values.classification_confirmed is not True):
            raise ValueError('change_requires_confirmation')
        if self.values.classification_confirmed and (self.values.important is None or self.values.urgent is None):
            raise ValueError('confirmed_classification_requires_both_axes')
        return self


class AcceptanceReceipt(TeamDTO):
    operation_id: UUIDString
    payload_hash: Digest
    decision: Literal['accepted', 'rejected']
    decided_at: AwareDatetime
    error_code: str | None = None


class ExecutionState(TeamDTO):
    state: ExecutionStatus
    revision: Revision = 0
    task_id: DecimalID | None = None
    verified_at: AwareDatetime | None = None
    error_code: str | None = None
    retry_after: int | None = Field(default=None, strict=True, ge=0)


class CommandReceipt(TeamDTO):
    acceptance_receipt: AcceptanceReceipt
    execution_state: ExecutionState
    current: TaskSnapshot | None = None


class ClaimedCommand(TeamDTO):
    command: TaskCommand
    actor_id: UUIDString
    worker_id: Text
    fence: Revision
    lease_until: AwareDatetime
    reconciliation: StrictBool = False


class DurableMessage(TeamDTO):
    id: UUIDString
    kind: Literal['inbox', 'outbox']
    dedup_key: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    actor_id: UUIDString
    project_id: DecimalID
    payload: dict


class MessageState(TeamDTO):
    message: DurableMessage
    state: Literal['pending', 'sending', 'sent', 'uncertain', 'failed', 'cancelled']
    fence: Revision = 0
    worker_id: str | None = None
    lease_until: AwareDatetime | None = None
    error_code: str | None = None
    reconciliation: StrictBool = False
