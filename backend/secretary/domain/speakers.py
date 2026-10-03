"""Identity overlays. Raw transcript/provider labels remain immutable evidence."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

Revision = Annotated[int, Field(strict=True, ge=0)]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=250, pattern=r"\S")]
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Method = Literal["voice_embedding", "provider_reference", "manual", "unknown"]
AttributionStatus = Literal["proposed", "confirmed", "unknown", "conflict"]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @field_validator("*", mode="before")
    @classmethod
    def utf8_encodable(cls, value):
        """JSON can decode lone surrogates that UTF-8/SQLite cannot store."""
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError:
                    raise ValueError("String must be valid UTF-8") from None
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
        return value


class PersonProfile(DTO):
    id: str
    display_name: Name
    aliases: list[Name]
    enabled: bool
    revision: Revision
    created_at: str
    updated_at: str


class VoiceEnrollment(DTO):
    """Public metadata only; private files/embeddings never enter this DTO."""
    id: str
    person_profile_id: str
    material_version: Revision
    revision: Revision
    consent_confirmed: bool
    status: Literal["pending", "awaiting_review", "ready", "revoked", "failed", "interrupted", "cancelled", "cleanup_pending"]
    model_id: str | None = None
    model_revision: str | None = None
    created_at: str
    revoked_at: str | None = None


class MeetingParticipant(DTO):
    id: str
    meeting_id: str
    person_profile_id: str | None
    display_name: Name
    aliases: list[Name]
    enabled: bool
    profile_revision: Revision | None
    created_at: str
    updated_at: str


class AttributionRun(DTO):
    id: str
    meeting_id: str
    transcript_version: Revision
    expected_revision: Revision
    roster_revision: Revision
    revision: Revision
    kind: Literal["manual", "automatic"]
    status: Literal["pending", "published", "stale", "failed"]
    profile_snapshot: list[dict]
    roster_snapshot: list[MeetingParticipant]
    audio_hash: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    algorithm_version: str | None = None
    created_at: str


class SpeakerReviewCandidate(DTO):
    participant_id: Identifier
    material_revision: Revision
    raw_score: float = Field(ge=-1,le=1)


class SpeakerAttribution(DTO):
    segment_id: str
    meeting_id: str
    transcript_version: Revision
    participant_id: str | None
    method: Method
    raw_score: float | None = None
    status: AttributionStatus
    reason_codes: list[str]
    run_id: str | None
    revision: Revision
    # Immutable source associations, never an identity inferred from STT confidence.
    chunk_id: str
    speaker_id: str | None
    provider_label: str | None
    channel: str
    ordinal: int
    start_ms: int | None
    end_ms: int | None
    group_id: str | None = None
    review_candidates: list[SpeakerReviewCandidate] = Field(default_factory=list,max_length=5)


class TaskAssignment(DTO):
    meeting_id: str
    summary_version: Revision
    action_id: str
    revision: Revision
    participant_id: str | None
    basis: Literal["named_person", "self_commitment", "manual", "unknown"]
    source_segment_ids: list[str]
    evidence_quote: str
    attribution_revision: Revision
    roster_revision: Revision
    status: Literal["proposed", "confirmed", "needs_review"]
    named_owner_text: str | None = None


class ParticipantRosterSnapshot(DTO):
    meeting_id: str
    roster_revision: Revision
    participants: list[MeetingParticipant]


class AttributionSnapshot(ParticipantRosterSnapshot):
    transcript_version: Revision
    revision: Revision
    items: list[SpeakerAttribution]
    automatic_overlay_stale: bool = False
    reason_codes: list[str] = Field(default_factory=list)


class CreateProfile(DTO):
    display_name: Name
    aliases: list[Name] = Field(default_factory=list, max_length=100)
    enabled: StrictBool = True
    operation_id: Identifier


class PatchProfile(DTO):
    display_name: Name | None = None
    aliases: list[Name] | None = Field(default=None, max_length=100)
    enabled: StrictBool | None = None
    expected_revision: Revision
    operation_id: Identifier

    @model_validator(mode="after")
    def changed(self):
        edits = self.model_fields_set - {"expected_revision", "operation_id"}
        if not edits or any(getattr(self, key) is None for key in edits):
            raise ValueError("A non-null profile edit is required")
        return self


class AddParticipant(DTO):
    person_profile_id: Identifier | None = None
    display_name: Name | None = None
    aliases: list[Name] | None = Field(default=None, max_length=100)
    enabled: StrictBool = True
    expected_roster_revision: Revision
    operation_id: Identifier

    @model_validator(mode="after")
    def identity(self):
        if self.person_profile_id is None and self.display_name is None:
            raise ValueError("Guest display_name required")
        if self.person_profile_id is not None and (self.display_name is not None or self.aliases is not None):
            raise ValueError("Profile participants use an immutable profile snapshot")
        return self


class PatchParticipant(DTO):
    display_name: Name | None = None
    aliases: list[Name] | None = Field(default=None, max_length=100)
    enabled: StrictBool | None = None
    apply_profile: StrictBool = False
    expected_roster_revision: Revision
    operation_id: Identifier

    @model_validator(mode="after")
    def changed(self):
        edits = self.model_fields_set - {"expected_roster_revision", "operation_id", "apply_profile"}
        if (not edits and not self.apply_profile) or any(getattr(self, key) is None for key in edits):
            raise ValueError("A non-null participant edit or apply_profile is required")
        if self.apply_profile and edits & {"display_name", "aliases"}:
            raise ValueError("apply_profile cannot be combined with name/aliases")
        return self


class AttributionChange(DTO):
    segment_id: Identifier
    participant_id: Identifier | None


class ReviewAttribution(DTO):
    transcript_version: Revision
    expected_revision: Revision
    operation_id: Identifier
    changes: list[AttributionChange] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_segments(self):
        ids = [change.segment_id for change in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate segment IDs")
        return self


class SpeakerError(Exception):
    status_code = 422


class SpeakerNotFound(SpeakerError):
    status_code = 404


class SpeakerConflict(SpeakerError):
    status_code = 409
