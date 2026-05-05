from datetime import datetime

from pydantic import BaseModel
from pydantic import ConfigDict


class MemoryExtractRequest(BaseModel):
    text: str
    user_id: str | None = None


class MemoryExtractResponse(BaseModel):
    tasks: list[str]


class MemoryTaskResponse(BaseModel):
    id: str
    user_id: str
    content: str
    status: str
    created_at: datetime
    source: str | None = None
    meeting_id: str | None = None
    confidence: float = 0.5
    deadline_at: datetime | None = None
    priority: str = "medium"
    completed_at: datetime | None = None
    updated_at: datetime | None = None
    validated: bool = False
    validated_at: datetime | None = None
    validation_source: str | None = None

    model_config = ConfigDict(from_attributes=True)


class MemoryTasksResponse(BaseModel):
    tasks: list[MemoryTaskResponse]


class MemoryExtractedTask(BaseModel):
    content: str
    confidence: float = 0.5
    priority: str = "medium"
    deadline_at: datetime | None = None


class MemoryCompleteResponse(BaseModel):
    id: str
    status: str
    completed_at: datetime | None = None


class MemoryApproveResponse(BaseModel):
    id: str
    validated: bool
    validated_at: datetime | None = None


class MemoryValidationRuleInfo(BaseModel):
    auto_threshold: float


class MemoryProblemItem(BaseModel):
    type: str
    count: int


class MemoryReportResponse(BaseModel):
    total: int
    pending: int
    done: int
    overdue: int
    high_priority: int
    urgent: int
    upcoming_deadlines: int
    low_confidence_pending: int
    validated_pending: int
    unvalidated_pending: int
    action_required: bool
    main_problem: str | None = None
    hidden_tasks: int
    health: str
    attention_score: int
    problems: list[MemoryProblemItem]
    generated_at: datetime


class MemoryIssuesResponse(BaseModel):
    low_confidence_tasks: list[dict]
    unvalidated_tasks: list[dict]
    overdue_tasks: list[dict]
    low_confidence_total: int = 0
    unvalidated_total: int = 0
    overdue_total: int = 0
    needs_attention: int


class MemoryValidationRulesResponse(BaseModel):
    auto_validate_threshold: float
    context_threshold: float
    rules: list[str]


class MemoryTodayFocusResponse(BaseModel):
    today_tasks: list[MemoryTaskResponse]
    overdue: list[MemoryTaskResponse]
    urgent: list[MemoryTaskResponse]
    today_tasks_total: int = 0
    overdue_total: int = 0
    urgent_total: int = 0
    next_action: str | None = None


class MemoryNudgeResponse(BaseModel):
    message: str
