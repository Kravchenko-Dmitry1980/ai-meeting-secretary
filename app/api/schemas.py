from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    db: str
    redis: str


class UploadMeetingResponse(BaseModel):
    meeting_id: str
    processing_job_id: str
    status: str


class MeetingStatusResponse(BaseModel):
    meeting_id: str
    meeting_status: str
    job_status: str
    stage: str
    error: str | None = None


class TranscriptResponse(BaseModel):
    meeting_id: str
    transcript: str
    provider: str
    language: str


class SummaryResponse(BaseModel):
    meeting_id: str
    summary: str


class SegmentItemResponse(BaseModel):
    speaker: str
    start_sec: str
    end_sec: str
    text: str


class SegmentsResponse(BaseModel):
    meeting_id: str
    segments: list[SegmentItemResponse]


class TaskItemResponse(BaseModel):
    id: str
    description: str
    assignee_speaker_label: str | None = None
    due_date: str | None = None
    priority: str
    source_quote: str
    confidence: float | None = None


class TasksResponse(BaseModel):
    meeting_id: str
    tasks: list[TaskItemResponse]
