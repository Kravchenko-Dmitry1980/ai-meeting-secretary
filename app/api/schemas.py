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
    speaker: str
    timestamp: str
    text: str


class SummaryResponse(BaseModel):
    summary: str


class SegmentItemResponse(BaseModel):
    speaker: str
    start: float
    end: float
    text: str


class TaskItemResponse(BaseModel):
    id: str
    title: str
    assignee: str | None = None
    due_date: str | None = None
    priority: str
    source_quote: str
    confidence: float | None = None
