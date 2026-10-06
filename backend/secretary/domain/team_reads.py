"""Safe public read models. Local observation time never claims remote authorship."""
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_serializer

from secretary.domain.team import DecimalID, Digest, ExecutionStatus, Revision, TaskChange, TeamDTO, Text, UUIDString


HistoryCursor = Annotated[str, Field(strict=True, min_length=1, max_length=512, pattern=r'^[A-Za-z0-9_-]+$')]
Rubles = Annotated[str, Field(strict=True, pattern=r'^[0-9]+\.[0-9]{6}$')]
ReadCode = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r'^[a-z][a-z0-9_]*$')]


class MemberView(TeamDTO):
    id: UUIDString
    display_name: Text
    revision: Revision


class MemberPage(TeamDTO):
    items: tuple[MemberView, ...]
    next_cursor: UUIDString | None = None


class TaskHistoryEntry(TeamDTO):
    id: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    kind: Literal['command', 'external_change']
    event: ReadCode
    recorded_at: AwareDatetime
    operation_id: UUIDString | None = None
    actor_id: UUIDString | None = None
    # Current directory label; never represented as the historical name.
    actor_display_name: Text | None = None
    action: ReadCode | None = None
    values: TaskChange | None = None
    execution_state: ExecutionStatus | None = None
    verified_at: AwareDatetime | None = None
    error_code: ReadCode | None = None
    changed_fields: tuple[ReadCode, ...] = ()
    before_fingerprint: Digest | None = None
    after_fingerprint: Digest | None = None
    remote_occurred_at: None = None

    @field_serializer('values')
    def submitted_values(self, value):
        # Missing fields are not intended changes; explicit due_at=None is.
        return value.model_dump(mode='json', exclude_unset=True) if value is not None else None


class TaskHistoryPage(TeamDTO):
    items: tuple[TaskHistoryEntry, ...]
    next_cursor: HistoryCursor | None = None


class SyncStatus(TeamDTO):
    state: Literal['not_configured', 'never_synced', 'syncing', 'ready', 'degraded']
    last_attempt_at: AwareDatetime | None = None
    last_successful_sync_at: AwareDatetime | None = None
    last_command_verified_at: AwareDatetime | None = None
    error_code: ReadCode | None = None
    issue_count: Annotated[int, Field(strict=True, ge=0)] = 0


class CloudStatus(TeamDTO):
    state: Literal['not_configured', 'ready', 'paused', 'unavailable']
    paused_reason: ReadCode | None = None
    spend_rub: Rubles | None = None
    reserved_rub: Rubles | None = None
    remaining_rub: Rubles | None = None
    effective_budget_rub: Rubles | None = None


class TeamStatusView(TeamDTO):
    project_id: DecimalID
    sync: SyncStatus
    cloud: CloudStatus
