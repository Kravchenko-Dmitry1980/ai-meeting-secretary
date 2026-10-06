"""Publication DTOs; validation of transcript evidence belongs to T5 service."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, Field, StrictBool, computed_field, model_validator

from secretary.domain.team import (
    AcceptanceReceipt, CommandReceipt, DecimalID, Digest, ExecutionState, Revision,
    TaskSnapshot, TeamDTO, Text, UUIDString, canonical, content_hash,
)

PublicationIntent = Literal['publish', 'link', 'propose_update', 'create_separate']
EvidenceFingerprint = Annotated[str, Field(strict=True, pattern=r'^assignment-evidence-v1:[0-9a-f]{64}$')]


class PublicationScope(TeamDTO):
    meeting_id: Text
    transcript_version: Revision
    summary_version: Revision
    destination_project_id: DecimalID


class PublicationWatermarks(TeamDTO):
    assignment_revision: Revision
    roster_revision: Revision
    attribution_revision: Revision
    context_hash: Digest


class PublicationTask(TeamDTO):
    action_id: Text
    title: Text
    assignee_id: UUIDString
    source_segment_ids: tuple[Text, ...]
    evidence_quote: Text
    due_at: AwareDatetime | None = None
    due_phrase: str | None = None
    due_confirmed: StrictBool = False
    due_timezone: Literal['Europe/Moscow'] = 'Europe/Moscow'
    publication_id: UUIDString | None = None
    member_revision: Revision = 0
    participant_id: Text | None = None
    source_fingerprint: EvidenceFingerprint | None = None
    intent: PublicationIntent = 'publish'
    target_publication_id: UUIDString | None = None
    target_task_id: DecimalID | None = None
    expected_task_revision: Revision | None = None
    expected_task_fingerprint: Digest | None = None
    target_snapshot: TaskSnapshot | None = None
    separate_id: UUIDString | None = None

    @model_validator(mode='after')
    def evidence_and_date(self):
        if not self.source_segment_ids or len(set(self.source_segment_ids)) != len(self.source_segment_ids):
            raise ValueError('publication_requires_unique_source_segments')
        if self.due_at is not None and not self.due_confirmed:
            raise ValueError('publication_due_requires_confirmation')
        targets = (self.target_publication_id, self.target_task_id, self.expected_task_revision, self.expected_task_fingerprint)
        if self.intent in ('link', 'propose_update') and any(value is None for value in targets):
            raise ValueError('publication_target_required')
        if self.intent in ('publish', 'create_separate') and any(value is not None for value in targets):
            raise ValueError('creation_has_no_target')
        if self.intent == 'create_separate' and self.separate_id is None:
            raise ValueError('separate_publication_requires_explicit_id')
        if self.intent != 'create_separate' and self.separate_id is not None:
            raise ValueError('separate_id_requires_explicit_decision')
        if self.target_snapshot is not None:
            if (self.target_snapshot.task_id, self.target_snapshot.revision, self.target_snapshot.remote_fingerprint) != (
                    self.target_task_id, self.expected_task_revision, self.expected_task_fingerprint):
                raise ValueError('publication_target_snapshot_mismatch')
        return self


class PublicationPayload(TeamDTO):
    preview_id: UUIDString
    scope: PublicationScope
    watermarks: PublicationWatermarks
    items: tuple[PublicationTask, ...] = Field(min_length=1, max_length=100)
    actor_id: UUIDString | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode='after')
    def selected_actions(self):
        if not self.items or len({item.action_id for item in self.items}) != len(self.items):
            raise ValueError('publication_requires_unique_selected_actions')
        return self


class PublishPreview(PublicationPayload):
    @computed_field
    @property
    def preview_hash(self) -> str:
        return content_hash(self.model_dump(mode='json', exclude={'preview_hash'}))


class PublishCommand(PublicationPayload):
    operation_id: UUIDString
    preview_hash: Digest


class PublicationSelection(TeamDTO):
    """Owner choices; source text/evidence and current mappings are server-owned."""
    action_id: Text
    assignee_id: UUIDString | None = None
    due_resolution: Literal['unresolved', 'date', 'none'] = 'unresolved'
    due_at: AwareDatetime | None = None
    intent: PublicationIntent = 'publish'
    target_publication_id: UUIDString | None = None
    separate_id: UUIDString | None = None

    @model_validator(mode='after')
    def choices(self):
        if (self.due_resolution == 'date') != (self.due_at is not None):
            raise ValueError('due_resolution_requires_exact_date')
        if self.intent in ('link', 'propose_update') and self.target_publication_id is None:
            raise ValueError('publication_target_required')
        if self.intent in ('publish', 'create_separate') and self.target_publication_id is not None:
            raise ValueError('creation_has_no_target')
        if (self.intent == 'create_separate') != (self.separate_id is not None):
            raise ValueError('separate_publication_requires_explicit_id')
        return self


class PreviewPublications(TeamDTO):
    scope: PublicationScope
    selections: tuple[PublicationSelection, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def selected_once(self):
        if len({item.action_id for item in self.selections}) != len(self.selections):
            raise ValueError('publication_requires_unique_selected_actions')
        return self


class SupersedesCandidate(TeamDTO):
    publication_id: UUIDString
    title: Text
    task_id: DecimalID | None = None
    verified: StrictBool = False


class PublicationCandidate(TeamDTO):
    action_id: Text
    title: str
    participant_id: Text | None = None
    assignee_id: UUIDString | None = None
    assignee_name: Text | None = None
    due_phrase: str | None = None
    evidence_quote: str
    source_segment_ids: tuple[Text, ...]
    eligible: StrictBool
    reason_codes: tuple[str, ...] = ()
    possible_supersedes: tuple[SupersedesCandidate, ...] = ()
    existing_publication_id: UUIDString | None = None


class PublicationPreviewResult(TeamDTO):
    candidates: tuple[PublicationCandidate, ...]
    preview: PublishPreview | None = None


class PublicationMember(TeamDTO):
    id: UUIDString
    display_name: Text
    revision: Revision


class PublicationContext(TeamDTO):
    scope: PublicationScope
    watermarks: PublicationWatermarks
    project_name: Text
    members: tuple[PublicationMember, ...]
    candidates: tuple[PublicationCandidate, ...]


class PublicationItemReceipt(TeamDTO):
    publication_id: UUIDString
    delivery_operation_id: UUIDString
    action_id: Text
    intent: PublicationIntent
    execution_state: ExecutionState
    gateway_receipt: CommandReceipt | None = None
    source_stale: StrictBool = False
    correction_required: StrictBool = False

    @model_validator(mode='after')
    def verified_application(self):
        gateway = self.gateway_receipt
        if gateway and gateway.acceptance_receipt.operation_id != self.delivery_operation_id:
            raise ValueError('publication_receipt_identity_mismatch')
        if self.execution_state.state == 'applied':
            if (not gateway or gateway.execution_state.state != 'applied'
                    or gateway.acceptance_receipt.decision != 'accepted'
                    or self.execution_state.task_id is None
                    or gateway.execution_state.task_id != self.execution_state.task_id
                    or gateway.execution_state.verified_at is None or gateway.current is None
                    or gateway.current.task_id != self.execution_state.task_id):
                raise ValueError('publication_requires_verified_gateway_receipt')
        return self


class DeliveryReceipt(TeamDTO):
    operation_id: UUIDString
    scope: PublicationScope
    acceptance_receipt: AcceptanceReceipt
    items: tuple[PublicationItemReceipt, ...]

    @model_validator(mode='after')
    def receipt_scope(self):
        if self.acceptance_receipt.operation_id != self.operation_id:
            raise ValueError('publication_batch_identity_mismatch')
        if len({item.action_id for item in self.items}) != len(self.items):
            raise ValueError('publication_requires_unique_selected_actions')
        for item in self.items:
            if item.gateway_receipt and item.gateway_receipt.current and item.gateway_receipt.current.project_id != self.scope.destination_project_id:
                raise ValueError('publication_receipt_scope_mismatch')
        return self


def publication_uuid(scope: PublicationScope, action_id: str, separate_id: str | None = None) -> str:
    """Assignment revisions cannot change normal creation identity."""
    return str(uuid5(NAMESPACE_URL, canonical(['secretary-publication-v1', scope.model_dump(mode='json'), action_id, separate_id])))


def delivery_uuid(scope: PublicationScope, item: PublicationTask) -> str:
    publication_id = publication_uuid(scope, item.action_id, item.separate_id)
    if item.intent != 'propose_update':
        return publication_id
    # A repeated identical proposal after a new status read must not append the
    # same comment again. Expected task revision/fingerprint are guards, not ID.
    value = item.model_dump(mode='json', exclude={'publication_id', 'expected_task_revision', 'expected_task_fingerprint', 'target_snapshot'})
    return str(uuid5(NAMESPACE_URL, canonical(['secretary-proposal-v1', publication_id, value])))


class GatewayPublication(TeamDTO):
    actor_id: UUIDString
    scope: PublicationScope
    item: PublicationTask

    @model_validator(mode='after')
    def identity(self):
        if self.item.publication_id != publication_uuid(self.scope, self.item.action_id, self.item.separate_id):
            raise ValueError('publication_identity_mismatch')
        if not self.item.due_confirmed or self.item.source_fingerprint is None:
            raise ValueError('publication_requires_confirmed_source_and_date')
        if self.item.target_snapshot and self.item.target_snapshot.project_id != self.scope.destination_project_id:
            raise ValueError('publication_target_scope_mismatch')
        return self
