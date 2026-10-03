"""Immutable paid input and assignment review contracts.

Speech and roster strings are data, never instructions or identity authority.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from secretary.domain.assignment_evidence import Basis, EvidenceDTO, ResolvedAssignment, SemanticFlag
from secretary.domain.speakers import Identifier, Revision


SUMMARY_CONTEXT_CONTRACT = "secretary-summary-context-v2"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class FrozenSummarySettings(EvidenceDTO):
    """Public summary route and provider routing switch; no credentials or live caps."""

    summary_model: str = Field(repr=False)
    summary_input_rub_per_million: float | None = Field(default=None, ge=0)
    summary_output_rub_per_million: float | None = Field(default=None, ge=0)
    summary_batch_chars: int = Field(ge=1, strict=True)
    summary_max_output_tokens: int = Field(ge=1, strict=True)
    local_cost_limits_enabled: StrictBool = True


class FrozenSummaryParticipant(EvidenceDTO):
    participant_id: Identifier
    display_name: str = Field(repr=False)
    aliases: tuple[str, ...] = Field(default=(), repr=False)
    enabled: StrictBool = True
    person_profile_id: Identifier | None = None


class FrozenSummarySource(EvidenceDTO):
    id: Identifier
    text: str = Field(repr=False)
    text_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_ms: Revision | None = None
    end_ms: Revision | None = None
    timing_precision: Literal["word", "segment", "chunk", "unknown"] = "unknown"
    channel: Literal["mixed", "microphone", "system", "import"]
    speaker_observation_id: Identifier
    participant_id: Identifier | None = None
    identity_status: Literal["proposed", "confirmed", "unknown", "conflict"] = "unknown"
    identity_method: Literal["voice_embedding", "provider_reference", "manual", "unknown"] = "unknown"
    identity_revision: Revision = 0
    identity_run_id: Identifier | None = None
    stale: StrictBool = False
    overlap: StrictBool = False
    ambiguous_author: StrictBool = False
    possible_author_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def source_integrity(self):
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_hash:
            raise ValueError("summary_source_hash_mismatch")
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("summary_source_time_invalid")
        if self.timing_precision == "unknown" and (self.start_ms is not None or self.end_ms is not None):
            raise ValueError("summary_unknown_timing_invalid")
        return self


class FrozenSummaryContext(EvidenceDTO):
    contract: Literal["secretary-summary-context-v2"] = SUMMARY_CONTEXT_CONTRACT
    meeting_id: Identifier
    transcript_version: Revision
    attribution_revision: Revision
    attribution_run_id: Identifier | None = None
    roster_revision: Revision
    settings: FrozenSummarySettings
    participants: tuple[FrozenSummaryParticipant, ...]
    sources: tuple[FrozenSummarySource, ...]
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def scoped_integrity(self):
        participants = [p.participant_id for p in self.participants]
        sources = [s.id for s in self.sources]
        if len(set(participants)) != len(participants) or len(set(sources)) != len(sources):
            raise ValueError("summary_context_duplicate_identifier")
        if participants != sorted(participants):
            raise ValueError("summary_context_noncanonical_roster")
        if any(tuple(sorted(set(p.aliases))) != p.aliases for p in self.participants):
            raise ValueError("summary_context_noncanonical_aliases")
        if any(s.identity_revision > self.attribution_revision for s in self.sources):
            raise ValueError("summary_context_future_identity")
        if any(s.participant_id is not None and s.participant_id not in participants for s in self.sources):
            raise ValueError("summary_context_foreign_participant")
        if any(set(s.possible_author_ids) - set(participants) for s in self.sources):
            raise ValueError("summary_context_foreign_author")
        if content_hash(self.model_dump(mode="json", exclude={"context_hash"})) != self.context_hash:
            raise ValueError("summary_context_hash_mismatch")
        return self


class TaskAssignmentChange(EvidenceDTO):
    action_id: Identifier
    decision: Literal["confirm_proposal", "set_manual", "clear"]
    participant_id: Identifier | None = None


class AssignmentProposal(EvidenceDTO):
    """Model semantic draft only; participant/status/manual fields are forbidden."""

    basis: Basis = "unknown"
    named_owner_text: str | None = Field(default=None, repr=False)
    commitment_segment_id: Identifier | None = None
    semantic_flag: SemanticFlag = "ambiguous"


class ReviewTaskAssignments(EvidenceDTO):
    transcript_version: Revision
    summary_version: Revision
    attribution_revision: Revision
    roster_revision: Revision
    expected_revision: Revision
    operation_id: Identifier
    changes: tuple[TaskAssignmentChange, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_changes(self):
        ids = [change.action_id for change in self.changes]
        if len(set(ids)) != len(ids):
            raise ValueError("assignment_duplicate_changes")
        return self


class AssignmentSnapshot(EvidenceDTO):
    meeting_id: Identifier
    transcript_version: Revision
    summary_version: Revision
    revision: Revision
    attribution_revision: Revision
    roster_revision: Revision
    captured_context_hash: str | None = None
    current_context_hash: str
    items: tuple[ResolvedAssignment, ...]

    @model_validator(mode="after")
    def scoped_items(self):
        ids = []
        for item in self.items:
            scope = item.action.scope
            if (scope.meeting_id, scope.transcript_version, scope.summary_version) != (
                    self.meeting_id, self.transcript_version, self.summary_version):
                raise ValueError("assignment_snapshot_scope_mismatch")
            if item.revision != self.revision:
                raise ValueError("assignment_snapshot_revision_mismatch")
            ids.append(item.action.action_id)
        if len(set(ids)) != len(ids):
            raise ValueError("assignment_snapshot_duplicate_action")
        return self


class AssignmentReviewResult(EvidenceDTO):
    """Original immutable receipt remains separate from the current view on replay."""

    operation_id: Identifier
    receipt: AssignmentSnapshot
    current: AssignmentSnapshot
    replayed: StrictBool = False
    receipt_stale: StrictBool = False
