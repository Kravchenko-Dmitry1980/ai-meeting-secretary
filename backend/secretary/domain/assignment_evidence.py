"""Immutable, offline assignment evidence contracts (no persistence or provider state)."""
from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from secretary.domain.speakers import DTO, Identifier, Revision

Basis = Literal["named_person", "self_commitment", "unknown"]
SemanticFlag = Literal[
    "explicit_commitment", "directive", "reported_speech", "question",
    "hypothesis", "negation", "ambiguous",
]


class EvidenceDTO(DTO):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class AssignmentScope(EvidenceDTO):
    meeting_id: Identifier
    transcript_version: Revision
    summary_version: Revision


class DraftAction(EvidenceDTO):
    scope: AssignmentScope
    action_id: Identifier
    text: str = Field(repr=False)
    basis: Basis = "unknown"
    named_owner_text: str | None = Field(default=None, repr=False)
    commitment_segment_id: str | None = None
    semantic_flag: SemanticFlag = "ambiguous"
    evidence_quote: str = Field(default="", repr=False)
    source_segment_ids: tuple[str, ...] = ()


class SourceSegment(EvidenceDTO):
    meeting_id: Identifier
    transcript_version: Revision
    segment_id: Identifier
    text: str = Field(repr=False)
    start_ms: Revision | None = None
    end_ms: Revision | None = None


class RosterParticipant(EvidenceDTO):
    meeting_id: Identifier
    participant_id: Identifier
    display_name: str = Field(repr=False)
    aliases: tuple[str, ...] = Field(default=(), repr=False)
    enabled: bool = True
    person_profile_id: str | None = None


class IdentityEvidence(EvidenceDTO):
    meeting_id: Identifier
    transcript_version: Revision
    segment_id: Identifier
    participant_id: str | None = None
    status: Literal["proposed", "confirmed", "unknown", "conflict"] = "unknown"
    revision: Revision = 0
    method: Literal["voice_embedding", "provider_reference", "manual", "unknown"] = "unknown"
    run_id: str | None = None
    # Supplied by the identity boundary. Never inspect enrollments or infer revoke.
    stale: bool = False
    overlap: bool = False
    ambiguous_author: bool = False
    possible_author_ids: tuple[str, ...] = ()


class AssignmentContext(EvidenceDTO):
    scope: AssignmentScope
    attribution_revision: Revision
    roster_revision: Revision
    sources: tuple[SourceSegment, ...]
    participants: tuple[RosterParticipant, ...]
    identities: tuple[IdentityEvidence, ...] = ()


class QuoteAnchor(EvidenceDTO):
    segment_id: Identifier
    start_char: Revision
    end_char: Revision
    source_text_hash: str


class CarryoverProvenance(EvidenceDTO):
    summary_version: Revision
    action_id: Identifier
    assignment_revision: Revision
    basis: Literal["named_person", "self_commitment", "manual", "unknown"]
    status: Literal["proposed", "confirmed", "needs_review"]


class ResolvedAssignment(EvidenceDTO):
    action: DraftAction
    participant_id: str | None
    basis: Literal["named_person", "self_commitment", "manual", "unknown"]
    status: Literal["proposed", "confirmed", "needs_review"]
    reason_codes: tuple[str, ...]
    confirm_eligible: bool
    anchor: QuoteAnchor | None
    fingerprint: str | None
    dependency_signature: str
    attribution_revision: Revision
    roster_revision: Revision
    revision: Revision = 0
    previous_provenance: CarryoverProvenance | None = None


class AssignmentReview(EvidenceDTO):
    """A human command, separate from the model draft. CAS/persistence is Task5b."""
    scope: AssignmentScope
    action_id: Identifier
    decision: Literal["confirm_proposal", "set_manual", "clear"]
    participant_id: str | None = None
    expected_attribution_revision: Revision
    expected_roster_revision: Revision


class AssignmentEvidenceError(ValueError):
    """Allowlisted reason only; never include quote/transcript/name in errors."""

