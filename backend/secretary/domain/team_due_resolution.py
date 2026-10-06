"""Explicit owner decisions for a deadline observed outside the Team gateway."""
from __future__ import annotations

from pydantic import AwareDatetime, model_validator

from secretary.domain.team import DecimalID, Digest, Revision, TeamDTO, Text, UUIDString


class DueResolutionCandidate(TeamDTO):
    observation_id: UUIDString
    project_id: DecimalID
    task_id: DecimalID
    baseline_revision: Revision
    baseline_fingerprint: Digest
    observed_fingerprint: Digest
    baseline_due_at: AwareDatetime | None
    observed_due_at: AwareDatetime | None
    title: Text


class DueResolutionRequest(TeamDTO):
    observation_id: UUIDString
    expected_revision: Revision
    expected_fingerprint: Digest
    # No default: a deliberate null is the explicit "Без срока" decision.
    due_at: AwareDatetime | None
    reason: Text


class DueResolutionPreview(TeamDTO):
    preview_id: UUIDString
    candidate: DueResolutionCandidate
    due_at: AwareDatetime | None
    reason: Text
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode='after')
    def lifetime(self):
        if self.expires_at <= self.created_at:
            raise ValueError('due_resolution_preview_lifetime_invalid')
        return self


class DueResolutionConfirmation(TeamDTO):
    operation_id: UUIDString


class StoredDueResolution(TeamDTO):
    """Private evidence; the before-image never enters the public response."""
    preview: DueResolutionPreview
    actor_id: UUIDString
    actor_revision: Revision
    before_image: dict
