from __future__ import annotations

from typing import Any, Callable, Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, model_validator
from secretary.domain.summary_context import AssignmentProposal
from secretary.domain.assignment_evidence import QuoteAnchor, CarryoverProvenance


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meeting(Entity):
    id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    transcript_version: int = 0
    duration_ms: int | None = None
    error: str | None = None
    processing_mode: Literal['ordinary','voice_identification'] = 'ordinary'


class AudioChunk(Entity):
    id: str
    meeting_id: str
    sequence: int
    channel: Literal["mixed", "microphone", "system", "import"]
    path: str
    offset_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    status: str
    sha256: str


class TranscriptSegment(Entity):
    id: str
    meeting_id: str
    chunk_id: str
    transcript_version: int
    ordinal: int
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    timing_precision: Literal["word", "segment", "chunk", "unknown"] = "unknown"
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    speaker_id: str | None = None
    channel: Literal["mixed", "microphone", "system", "import"]

    @model_validator(mode="after")
    def check_time(self):
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("end_ms precedes start_ms")
        if self.timing_precision == "unknown" and (self.start_ms is not None or self.end_ms is not None):
            raise ValueError("Unknown timing must use null offsets")
        return self


class Speaker(Entity):
    id: str
    meeting_id: str
    chunk_id: str
    provider_label: str
    display_name: str | None = None


class EvidenceItem(Entity):
    text: str
    source_segment_ids: list[str]
    evidence_quote: str = ''


class ActionItem(EvidenceItem):
    id: str
    owner: str | None = None
    due_date: str | None = None
    assignment_proposal: AssignmentProposal | None = None
    participant_id: str | None = None
    assignment_status: Literal['proposed','confirmed','needs_review'] = 'needs_review'
    assignment_basis: Literal['named_person','self_commitment','manual','unknown'] = 'unknown'
    assignment_revision: int = Field(default=0,ge=0,strict=True)
    assignment_reason_codes: list[str] = Field(default_factory=list)
    assignment_anchor: QuoteAnchor | None = None
    assignment_provenance: CarryoverProvenance | None = None


class Summary(Entity):
    meeting_id: str
    transcript_version: int
    summary_version: int
    overview: str
    readable_transcript: str
    decisions: list[EvidenceItem]
    action_items: list[ActionItem]
    open_questions: list[EvidenceItem]
    excluded_items_count: int = Field(default=0, ge=0, strict=True)
    status: str = "succeeded"


class ProcessingJob(Entity):
    id: str
    meeting_id: str
    version: int = 0
    chunk_id: str | None = None
    stage: Literal["prepare", "transcribe", "identify_speakers", "summarize"]
    intent_key: str | None = None
    run_id: str | None = None
    local_outcome: str | None = None
    status: Literal["queued", "running", "waiting_config", "paused_budget", "succeeded", "failed", "cancelled", "uncertain"]
    attempts: int
    error: str | None = None
    created_at: str
    updated_at: str


class UsageRecord(Entity):
    id: str
    meeting_id: str
    job_id: str
    kind: str
    estimated_rub: float | None = None
    confirmed_rub: float | None = None
    status: Literal["reserved", "confirmed", "unknown", "released"]
    provider_request_id: str | None = None


class AudioCapture(Protocol):
    def list_devices(self) -> dict: ...
    def start(self, meeting_id: str, microphone_id: str | None, system_id: str | None,
              on_chunk: Callable[[dict], None], on_error: Callable[[str], None]) -> dict: ...
    def stop(self, meeting_id: str) -> dict: ...
    def state(self, meeting_id: str) -> dict: ...
    def close(self) -> None: ...


class TranscriptionProvider(Protocol):
    async def transcribe(self, path: str, *, offset_ms: int = 0, channel: str = "import") -> dict: ...


class SummaryProvider(Protocol):
    async def summarize(self, segments: list[dict], *, context: Any | None = None) -> dict: ...


class ModelCatalog(Protocol):
    async def models(self) -> dict: ...


class MeetingRepository(Protocol):
    def create_meeting(self, title: str) -> dict: ...
    def list_meetings(self) -> list[dict]: ...
    def meeting(self, meeting_id: str) -> dict: ...


class JobRepository(Protocol):
    def enqueue(self, meeting_id: str, stage: str, *, chunk_id: str | None = None, version: int = 0) -> dict: ...
    def claim(self) -> dict | None: ...

