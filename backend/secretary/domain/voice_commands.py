"""Short-command contracts. Model suggestions cannot grant scope or execute a task."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, field_validator, model_validator

from secretary.domain.bot import BotError, BotEvent
from secretary.domain.team import (Bucket, DecimalID, Digest, Revision, TaskCommand,
    TeamDTO, Text, UUIDString)


IntentAction = Literal['create', 'set_state', 'assign', 'set_due', 'classify', 'comment', 'propose_due', 'rename']
UnresolvedField = Literal['action', 'project', 'task', 'member', 'due', 'due_time', 'classification', 'result', 'evidence']
VoiceText = Annotated[str, Field(strict=True, min_length=1, max_length=16000)]


class VoiceError(BotError):
    """Stable code-only failures; never provider output, media URLs or transcripts."""

    def __init__(self, code: str, *, uncertain=False):
        self.uncertain = uncertain is True
        super().__init__(code)


class RawIntent(TeamDTO):
    action: IntentAction
    project_id: DecimalID | None
    task_id: DecimalID | None
    member_id: UUIDString | None
    person_mention: Text | None
    title: Text | None
    text: Text | None
    bucket: Bucket | None
    important: StrictBool | None
    urgent: StrictBool | None
    due_phrase: Text | None
    proposed_due_at: AwareDatetime | None
    evidence_quote: Text
    utterance_kind: Literal['command', 'negated', 'reported', 'question']
    unresolved_fields: tuple[UnresolvedField, ...]

    @field_validator('unresolved_fields')
    @classmethod
    def unique_unresolved(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('voice_duplicate_unresolved_field')
        return value


class RawIntentReply(TeamDTO):
    proposals: Annotated[tuple[RawIntent, ...], Field(max_length=5)]


class IntentProposal(TeamDTO):
    id: UUIDString
    command_id: UUIDString
    actor_id: UUIDString
    actor_revision: Revision
    project_ids: tuple[DecimalID, ...]
    revision: Revision = 0
    action: IntentAction
    project_id: DecimalID | None = None
    task_id: DecimalID | None = None
    expected_revision: Revision | None = None
    expected_fingerprint: Digest | None = None
    expected_assignee_revision: Revision | None = None
    evidence_quote: Text
    unresolved_fields: tuple[UnresolvedField, ...] = ()
    proposed_due_at: AwareDatetime | None = None
    due_phrase: Text | None = None
    expires_at: AwareDatetime
    source_hash: Digest
    operation_id: UUIDString
    command: TaskCommand | None = None

    @model_validator(mode='after')
    def frozen_command(self):
        if len(self.project_ids) != len(set(self.project_ids)):
            raise ValueError('voice_duplicate_project')
        if len(self.unresolved_fields) != len(set(self.unresolved_fields)):
            raise ValueError('voice_duplicate_unresolved_field')
        if self.project_id is not None and self.project_id not in self.project_ids:
            raise ValueError('voice_foreign_project')
        if self.command is not None:
            c = self.command
            if (self.unresolved_fields or c.operation_id != self.operation_id or c.action != self.action
                    or c.project_id != self.project_id or c.task_id != self.task_id
                    or c.expected_revision != self.expected_revision
                    or c.expected_fingerprint != self.expected_fingerprint
                    or c.expected_assignee_revision != self.expected_assignee_revision):
                raise ValueError('voice_command_binding_invalid')
        return self


@dataclass(frozen=True)
class VoiceClaim:
    job_id: str
    event: BotEvent
    worker_id: str
    fence: int
    lease_until: datetime
    stage: Literal['queued', 'stt', 'intent', 'repair', 'complete'] = 'queued'
