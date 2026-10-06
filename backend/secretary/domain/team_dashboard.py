"""Owner-only local operational evidence; delivery never proves human reading."""
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from secretary.domain.team import DecimalID, TeamDTO, UUIDString
from secretary.domain.team_reads import ReadCode, TeamStatusView


DashboardCursor = Annotated[str, Field(strict=True, min_length=1, max_length=512, pattern=r'^[A-Za-z0-9_-]+$')]
Count = Annotated[int, Field(strict=True, ge=0)]
ObservationState = Literal['not_configured', 'observed', 'issues', 'unavailable']


class OwnerCommandItem(TeamDTO):
    operation_id: UUIDString
    task_id: DecimalID | None = None
    action: Literal['create', 'set_state', 'assign', 'set_due', 'resolve_due', 'classify', 'comment', 'propose_due', 'rename', 'link']
    state: Literal['queued', 'running', 'reconciling', 'uncertain']
    accepted_at: AwareDatetime
    last_state_at: AwareDatetime | None = None
    lease_until: AwareDatetime | None = None
    error_code: ReadCode | None = None


class OwnerCommandPage(TeamDTO):
    items: Annotated[tuple[OwnerCommandItem, ...], Field(max_length=100)]
    next_cursor: DashboardCursor | None = None


class DashboardDeliverySummary(TeamDTO):
    state: ObservationState
    sources: tuple[Literal['bot', 'voice'], ...] = ()
    pending_count: Count | None = None
    sending_count: Count | None = None
    retryable_count: Count | None = None
    uncertain_count: Count | None = None
    rejected_count: Count | None = None
    max_api_accepted_count: Count | None = None
    last_max_api_accepted_at: AwareDatetime | None = None
    human_read_confirmed: None = None

    @model_validator(mode='after')
    def honest_observation(self):
        counts = (self.pending_count, self.sending_count, self.retryable_count,
                  self.uncertain_count, self.rejected_count, self.max_api_accepted_count)
        if len(set(self.sources)) != len(self.sources):
            raise ValueError('dashboard_observation_invalid')
        if self.state in {'not_configured', 'unavailable'}:
            if any(value is not None for value in counts) or self.last_max_api_accepted_at is not None:
                raise ValueError('dashboard_observation_invalid')
        elif not self.sources or any(value is None for value in counts):
            raise ValueError('dashboard_observation_invalid')
        elif (self.state == 'issues') != bool(self.retryable_count or self.uncertain_count or self.rejected_count):
            raise ValueError('dashboard_observation_invalid')
        if self.last_max_api_accepted_at is not None and not self.max_api_accepted_count:
            raise ValueError('dashboard_observation_invalid')
        if self.state == 'not_configured' and self.sources:
            raise ValueError('dashboard_observation_invalid')
        return self


class DashboardWebhookStatus(TeamDTO):
    state: Literal['not_configured', 'unknown', 'unavailable', 'observed', 'issues']
    last_authenticated_accept_at: AwareDatetime | None = None
    configured_subscription: bool = False
    subscription_state: Literal['present', 'missing', 'unavailable', 'verified', 'rejected', 'uncertain'] | None = None
    last_subscription_observed_at: AwareDatetime | None = None
    last_trusted_callback_at: AwareDatetime | None = None
    phone_delivery: Literal['unknown'] = 'unknown'

    @model_validator(mode='after')
    def honest_observation(self):
        if self.state in {'not_configured', 'unavailable'} and any(value is not None for value in
            (self.last_authenticated_accept_at, self.last_subscription_observed_at, self.last_trusted_callback_at)):
            raise ValueError('dashboard_observation_invalid')
        if self.state in {'observed', 'issues'} and self.last_subscription_observed_at is None and self.last_trusted_callback_at is None:
            raise ValueError('dashboard_observation_invalid')
        if self.subscription_state is not None and not self.configured_subscription:
            raise ValueError('dashboard_observation_invalid')
        return self


class DashboardNotificationSummary(TeamDTO):
    state: ObservationState
    pending_count: Count | None = None
    sending_count: Count | None = None
    uncertain_count: Count | None = None
    failed_count: Count | None = None
    cancelled_count: Count | None = None
    last_max_api_accepted_at: AwareDatetime | None = None
    human_read_confirmed: None = None

    @model_validator(mode='after')
    def honest_observation(self):
        counts = (self.pending_count, self.sending_count, self.uncertain_count, self.failed_count, self.cancelled_count)
        if self.state in {'not_configured', 'unavailable'}:
            if any(value is not None for value in counts) or self.last_max_api_accepted_at is not None:
                raise ValueError('dashboard_observation_invalid')
        elif any(value is None for value in counts):
            raise ValueError('dashboard_observation_invalid')
        elif (self.state == 'issues') != bool(self.uncertain_count or self.failed_count):
            raise ValueError('dashboard_observation_invalid')
        return self


class OwnerDashboardView(TeamDTO):
    project_id: DecimalID
    status: TeamStatusView
    commands: OwnerCommandPage
    deliveries: DashboardDeliverySummary
    webhook: DashboardWebhookStatus
    notifications: DashboardNotificationSummary
