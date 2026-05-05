import logging
from time import perf_counter

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.routes import get_current_user
from app.infrastructure.config.settings import settings
from app.infrastructure.db.models import User
from app.infrastructure.db.session import get_db_session
from app.schemas.memory import MemoryExtractRequest
from app.schemas.memory import MemoryExtractResponse
from app.schemas.memory import MemoryReportResponse
from app.schemas.memory import MemoryIssuesResponse
from app.schemas.memory import MemoryTodayFocusResponse
from app.schemas.memory import MemoryNudgeResponse
from app.schemas.memory import MemoryValidationRulesResponse
from app.schemas.memory import MemoryCompleteResponse
from app.schemas.memory import MemoryApproveResponse
from app.schemas.memory import MemoryTaskResponse
from app.schemas.memory import MemoryTasksResponse
from app.services.memory_service import extract_tasks
from app.services.memory_service import get_active_tasks
from app.services.memory_service import get_memory_issues
from app.services.memory_service import get_memory_report
from app.services.memory_service import get_nudge_message
from app.services.memory_service import get_task_issue_reason
from app.services.memory_service import get_today_focus
from app.services.memory_service import mark_task_done
from app.services.memory_service import approve_task
from app.services.memory_service import AUTO_VALIDATE_THRESHOLD
from app.services.memory_service import MIN_CONFIDENCE_FOR_CONTEXT
from app.services.memory_service import now_utc

logger = logging.getLogger(__name__)
_SLOW_ENDPOINT_THRESHOLD_MS = 300

router = APIRouter()


def _resolve_user_id(
    request_user_id: str | None,
    current_user: User | None,
) -> str:
    if request_user_id and request_user_id.strip():
        return request_user_id.strip()
    if current_user is not None:
        if getattr(current_user, "id", None):
            return str(current_user.id)
        if getattr(current_user, "email", None):
            return str(current_user.email)
    if settings.app_disable_auth:
        return "default_user"
    return "default_user"


@router.post("/memory/extract", response_model=MemoryExtractResponse)
def memory_extract(
    payload: MemoryExtractRequest,
    current_user: User = Depends(get_current_user),
) -> MemoryExtractResponse:
    user_id = _resolve_user_id(payload.user_id, current_user)
    _ = user_id
    tasks = extract_tasks(payload.text)
    return MemoryExtractResponse(tasks=tasks)


@router.get("/memory/tasks", response_model=MemoryTasksResponse)
def memory_tasks(
    user_id: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MemoryTasksResponse:
    resolved_user_id = _resolve_user_id(user_id, current_user)
    tasks = get_active_tasks(user_id=resolved_user_id, db=db)
    return MemoryTasksResponse(
        tasks=[MemoryTaskResponse.model_validate(task) for task in tasks]
    )


@router.post("/memory/tasks/{task_id}/done", response_model=MemoryCompleteResponse)
def memory_task_done(
    task_id: str,
    user_id: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MemoryCompleteResponse:
    resolved_user_id = _resolve_user_id(user_id, current_user)
    task = mark_task_done(user_id=resolved_user_id, task_id=task_id, db=db)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return MemoryCompleteResponse(
        id=task.id,
        status=task.status,
        completed_at=task.completed_at,
    )


@router.post("/memory/tasks/{task_id}/approve", response_model=MemoryApproveResponse)
def memory_task_approve(
    task_id: str,
    user_id: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MemoryApproveResponse:
    resolved_user_id = _resolve_user_id(user_id, current_user)
    task = approve_task(user_id=resolved_user_id, task_id=task_id, db=db)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return MemoryApproveResponse(
        id=task.id,
        validated=task.validated,
        validated_at=task.validated_at,
    )


@router.get("/memory/report", response_model=MemoryReportResponse)
def memory_report(
    user_id: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MemoryReportResponse:
    """Memory report endpoint. `deadline_at` naive datetime is treated as UTC."""
    started_at = perf_counter()
    try:
        resolved_user_id = _resolve_user_id(user_id, current_user)
        payload = get_memory_report(user_id=resolved_user_id, db=db)
        return MemoryReportResponse.model_validate(payload)
    finally:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.info("memory_endpoint_timing endpoint=/memory/report duration_ms=%s", elapsed_ms)
        if elapsed_ms > _SLOW_ENDPOINT_THRESHOLD_MS:
            logger.warning(
                "SLOW_ENDPOINT endpoint=/memory/report duration_ms=%s threshold_ms=%s",
                elapsed_ms,
                _SLOW_ENDPOINT_THRESHOLD_MS,
            )


@router.get("/memory/issues", response_model=MemoryIssuesResponse, response_model_exclude_none=True)
def memory_issues(
    user_id: str | None = None,
    debug: str | bool = False,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MemoryIssuesResponse:
    started_at = perf_counter()
    try:
        resolved_user_id = _resolve_user_id(user_id, current_user)
        payload = get_memory_issues(user_id=resolved_user_id, db=db)
        now = now_utc()
        debug_value = str(debug).strip().lower()
        is_compact = debug_value == "compact"
        is_debug = debug_value in {"1", "true", "yes", "compact"}

        def _serialize_tasks(items: list[object]) -> list[dict]:
            serialized: list[dict] = []
            for task in items:
                reason = get_task_issue_reason(task, now)
                if is_compact:
                    task_payload = {"id": str(getattr(task, "id", "")), "reason": reason}
                else:
                    task_payload = MemoryTaskResponse.model_validate(task).model_dump(
                        exclude_none=True
                    )
                    if is_debug:
                        task_payload["reason"] = reason
                serialized.append(task_payload)
            return serialized

        return MemoryIssuesResponse(
            low_confidence_tasks=_serialize_tasks(payload["low_confidence_tasks"]),
            unvalidated_tasks=_serialize_tasks(payload["unvalidated_tasks"]),
            overdue_tasks=_serialize_tasks(payload["overdue_tasks"]),
            low_confidence_total=int(payload.get("low_confidence_total", 0)),
            unvalidated_total=int(payload.get("unvalidated_total", 0)),
            overdue_total=int(payload.get("overdue_total", 0)),
            needs_attention=int(payload["needs_attention"]),
        )
    finally:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.info("memory_endpoint_timing endpoint=/memory/issues duration_ms=%s", elapsed_ms)
        if elapsed_ms > _SLOW_ENDPOINT_THRESHOLD_MS:
            logger.warning(
                "SLOW_ENDPOINT endpoint=/memory/issues duration_ms=%s threshold_ms=%s",
                elapsed_ms,
                _SLOW_ENDPOINT_THRESHOLD_MS,
            )


@router.get("/memory/validation-rules", response_model=MemoryValidationRulesResponse)
def memory_validation_rules() -> MemoryValidationRulesResponse:
    return MemoryValidationRulesResponse(
        auto_validate_threshold=AUTO_VALIDATE_THRESHOLD,
        context_threshold=MIN_CONFIDENCE_FOR_CONTEXT,
        rules=[
            "tasks with confidence >= 0.75 are auto-validated",
            "only validated tasks are used in context",
            "tasks below 0.55 are ignored",
        ],
    )


@router.get("/memory/today-focus", response_model=MemoryTodayFocusResponse)
def memory_today_focus(
    user_id: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MemoryTodayFocusResponse:
    """Today focus endpoint. `deadline_at` naive datetime is treated as UTC."""
    started_at = perf_counter()
    try:
        resolved_user_id = _resolve_user_id(user_id, current_user)
        payload = get_today_focus(user_id=resolved_user_id, db=db)
        return MemoryTodayFocusResponse(
            today_tasks=[MemoryTaskResponse.model_validate(task) for task in payload["today_tasks"]],
            overdue=[MemoryTaskResponse.model_validate(task) for task in payload["overdue"]],
            urgent=[MemoryTaskResponse.model_validate(task) for task in payload["urgent"]],
            today_tasks_total=int(payload.get("today_tasks_total", 0)),
            overdue_total=int(payload.get("overdue_total", 0)),
            urgent_total=int(payload.get("urgent_total", 0)),
            next_action=payload.get("next_action"),
        )
    finally:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.info(
            "memory_endpoint_timing endpoint=/memory/today-focus duration_ms=%s",
            elapsed_ms,
        )
        if elapsed_ms > _SLOW_ENDPOINT_THRESHOLD_MS:
            logger.warning(
                "SLOW_ENDPOINT endpoint=/memory/today-focus duration_ms=%s threshold_ms=%s",
                elapsed_ms,
                _SLOW_ENDPOINT_THRESHOLD_MS,
            )


@router.get("/memory/nudge", response_model=MemoryNudgeResponse)
def memory_nudge(
    user_id: str | None = None,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MemoryNudgeResponse:
    started_at = perf_counter()
    try:
        resolved_user_id = _resolve_user_id(user_id, current_user)
        return MemoryNudgeResponse(message=get_nudge_message(user_id=resolved_user_id, db=db))
    finally:
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        logger.info("memory_endpoint_timing endpoint=/memory/nudge duration_ms=%s", elapsed_ms)
        if elapsed_ms > _SLOW_ENDPOINT_THRESHOLD_MS:
            logger.warning(
                "SLOW_ENDPOINT endpoint=/memory/nudge duration_ms=%s threshold_ms=%s",
                elapsed_ms,
                _SLOW_ENDPOINT_THRESHOLD_MS,
            )
