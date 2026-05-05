import json
import logging
import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from openai import OpenAI
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import select

from app.infrastructure.config.settings import settings
from app.models.memory import MemoryItem

logger = logging.getLogger(__name__)

_BULLET_PREFIX_RE = re.compile(r"^\s*[-*•\d\)\.(]+\s*")
_PUNCTUATION_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_TASK_MAX_LENGTH = 300
_ALLOWED_PRIORITIES = {"low", "medium", "high", "urgent"}
AUTO_VALIDATE_THRESHOLD = 0.75
MIN_CONFIDENCE_FOR_CONTEXT = 0.55
_PRIORITY_ORDER = {"urgent": 4, "high": 3, "medium": 2, "low": 1}


def now_utc() -> datetime:
    return datetime.now(UTC)


def apply_auto_validation(item: MemoryItem) -> None:
    if float(item.confidence or 0.0) >= AUTO_VALIDATE_THRESHOLD:
        item.validated = True
        item.validated_at = now_utc()
        item.validation_source = "auto"
    else:
        item.validated = False
        item.validated_at = None
        item.validation_source = None
def normalize_task(text: str) -> str:
    cleaned = _BULLET_PREFIX_RE.sub("", (text or "").strip())
    cleaned = cleaned.lower()
    cleaned = _PUNCTUATION_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned[:_TASK_MAX_LENGTH].strip()


def parse_tasks_from_llm_output(output: str) -> list[str]:
    task_items = parse_extracted_tasks_from_llm_output(output)
    return [str(item["content"]) for item in task_items]


def _parse_confidence(raw: object) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def _parse_priority(raw: object) -> str:
    value = str(raw or "medium").strip().lower()
    if value not in _ALLOWED_PRIORITIES:
        return "medium"
    return value


def _parse_deadline(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    value = str(raw).strip()
    if not value or value.lower() == "null":
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_deadline_input(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_extracted_tasks_from_llm_output(output: str) -> list[dict]:
    if not output:
        return []
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        logger.warning("memory_parse_failed_invalid_json")
        return []
    if not isinstance(payload, list):
        logger.warning("memory_parse_failed_non_list_payload")
        return []
    tasks: list[dict] = []
    for item in payload:
        if isinstance(item, str):
            normalized = normalize_task(item)
            if normalized:
                tasks.append(
                    {
                        "content": normalized,
                        "confidence": 0.5,
                        "priority": "medium",
                        "deadline_at": None,
                        "raw_payload": item,
                    }
                )
            continue
        if not isinstance(item, dict):
            continue
        content = normalize_task(str(item.get("content", "")))
        if not content:
            continue
        tasks.append(
            {
                "content": content,
                "confidence": _parse_confidence(item.get("confidence", 0.5)),
                "priority": _parse_priority(item.get("priority", "medium")),
                "deadline_at": _parse_deadline(item.get("deadline")),
                "raw_payload": json.dumps(item, ensure_ascii=False),
            }
        )
    return tasks


def extract_tasks(text: str) -> list[str]:
    task_items = extract_task_items(text)
    return [str(item["content"]) for item in task_items]


def extract_task_items(text: str) -> list[dict]:
    if not text.strip():
        return []
    if not settings.openai_api_key:
        logger.warning("memory_extract_skipped_openai_key_missing")
        return []

    current_date = datetime.now(UTC).date().isoformat()
    prompt = (
        "You are an assistant that extracts actionable tasks from meeting text.\n\n"
        "Return ONLY a valid JSON array of objects.\n"
        "Do not include markdown.\n"
        "Do not include explanations.\n\n"
        "Extract only:\n"
        "- concrete tasks\n"
        "- obligations\n"
        "- follow-ups\n"
        "- agreed next actions\n\n"
        "Ignore:\n"
        "- general discussion\n"
        "- opinions\n"
        "- background\n"
        "- unclear ideas\n"
        "- emotional phrases\n\n"
        "For each task return:\n"
        "- content: short task in the same language as source text\n"
        "- confidence: number from 0 to 1\n"
        "- priority: low, medium, high, or urgent\n"
        "- deadline: ISO datetime if explicitly stated, otherwise null\n\n"
        "Rules:\n"
        "- Do not invent deadlines.\n"
        "- If deadline is relative, infer it only if current date is available in the service context.\n"
        "- If task is vague, set confidence below 0.6.\n"
        "- If there are no tasks, return [].\n\n"
        f"Current date: {current_date}\n\n"
        f"Text:\n{text}"
    )

    try:
        client = OpenAI(api_key=settings.openai_api_key)
        completion = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0,
            messages=[
                {"role": "system", "content": "Return only valid JSON array of objects."},
                {"role": "user", "content": prompt},
            ],
        )
        output = completion.choices[0].message.content or "[]"
        return parse_extracted_tasks_from_llm_output(output)
    except Exception:  # noqa: BLE001
        logger.exception("memory_extract_failed")
        return []


def save_tasks(
    user_id: str,
    tasks: list[str],
    db,
    source: str = "llm",
    meeting_id: str | None = None,
) -> list[MemoryItem]:
    task_items = [
        {
            "content": task,
            "confidence": 0.5,
            "priority": "medium",
            "deadline_at": None,
            "raw_payload": None,
        }
        for task in tasks
    ]
    return save_task_items(
        user_id=user_id,
        task_items=task_items,
        db=db,
        source=source,
        meeting_id=meeting_id,
    )


def save_task_items(
    user_id: str,
    task_items: list[dict],
    db,
    source: str = "llm",
    meeting_id: str | None = None,
) -> list[MemoryItem]:
    if not user_id.strip():
        return []

    saved: list[MemoryItem] = []
    for task_item in task_items:
        normalized = normalize_task(str(task_item.get("content", "")))
        if not normalized:
            continue
        confidence = _parse_confidence(task_item.get("confidence", 0.5))
        if confidence < 0.45:
            continue
        priority = _parse_priority(task_item.get("priority", "medium"))
        deadline_at = normalize_deadline_input(_parse_deadline(task_item.get("deadline_at")))
        raw_payload = task_item.get("raw_payload")
        duplicate = db.scalar(
            select(MemoryItem).where(
                MemoryItem.user_id == user_id,
                MemoryItem.content == normalized,
                MemoryItem.status == "pending",
            )
        )
        if duplicate is not None:
            changed = False
            if confidence > float(duplicate.confidence or 0.5):
                duplicate.confidence = confidence
                changed = True
            if not duplicate.deadline_at and deadline_at is not None:
                duplicate.deadline_at = deadline_at
                changed = True
            if (duplicate.priority or "medium") == "medium" and priority in {"high", "urgent"}:
                duplicate.priority = priority
                changed = True
            if raw_payload:
                duplicate.raw_extracted_payload = str(raw_payload)
                changed = True
            if changed:
                apply_auto_validation(duplicate)
                duplicate.updated_at = now_utc()
                saved.append(duplicate)
            continue
        item = MemoryItem(
            user_id=user_id,
            content=normalized,
            status="pending",
            source=source or "llm",
            meeting_id=meeting_id,
            confidence=confidence,
            deadline_at=deadline_at,
            priority=priority,
            raw_extracted_payload=str(raw_payload) if raw_payload else None,
        )
        apply_auto_validation(item)
        item.updated_at = now_utc()
        db.add(item)
        saved.append(item)

    if not saved:
        return []

    try:
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("memory_save_failed")
        return []

    for item in saved:
        db.refresh(item)
    return saved


def get_active_tasks(user_id: str, db, limit: int = 20) -> list[MemoryItem]:
    if not user_id.strip():
        return []
    safe_limit = max(1, min(limit, 100))
    priority_weight = case(
        (MemoryItem.priority == "urgent", 4),
        (MemoryItem.priority == "high", 3),
        (MemoryItem.priority == "medium", 2),
        else_=1,
    )
    rows = db.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.user_id == user_id,
            MemoryItem.status == "pending",
            MemoryItem.validated.is_(True),
        )
        .order_by(
            priority_weight.desc(),
            MemoryItem.deadline_at.asc().nulls_last(),
            MemoryItem.created_at.desc(),
        )
        .limit(safe_limit)
    ).all()
    current_time = now_utc()
    for row in rows:
        try:
            _ = is_overdue(row, current_time)
        except Exception:  # noqa: BLE001
            logger.exception("memory_overdue_check_failed item_id=%s", row.id)
    return list(rows)


def build_active_tasks_context(user_id: str, db, limit: int = 10) -> str:
    try:
        tasks = get_active_tasks(user_id=user_id, db=db, limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("memory_context_build_failed")
        return ""

    if not tasks:
        return ""
    now = now_utc()
    max_age_cutoff = now - timedelta(days=30)
    lines = ["Active unresolved tasks from previous meetings:"]
    for task in tasks:
        created_at = task.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if created_at < max_age_cutoff:
            continue
        if float(task.confidence or 0.5) < MIN_CONFIDENCE_FOR_CONTEXT:
            continue
        deadline_text = f" до {task.deadline_at.date().isoformat()}" if task.deadline_at else ""
        lines.append(f"- [{task.priority}]{deadline_text}: {task.content}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def mark_task_done(user_id: str, task_id: str, db) -> MemoryItem | None:
    item = db.scalar(select(MemoryItem).where(MemoryItem.id == task_id))
    if item is None:
        return None
    if item.user_id != user_id:
        return None
    now = now_utc()
    item.status = "done"
    item.completed_at = now
    item.updated_at = now
    db.commit()
    db.refresh(item)
    return item


def approve_task(user_id: str, task_id: str, db) -> MemoryItem | None:
    item = db.scalar(select(MemoryItem).where(MemoryItem.id == task_id))
    if item is None:
        return None
    if item.user_id != user_id:
        return None
    item.validated = True
    item.validated_at = now_utc()
    item.validation_source = "manual"
    item.updated_at = now_utc()
    db.commit()
    db.refresh(item)
    return item


def is_overdue(item: MemoryItem, now: datetime) -> bool:
    """Check overdue status. `deadline_at` naive datetime is treated as UTC."""
    deadline_at = item.deadline_at
    if deadline_at is None:
        return False
    if deadline_at.tzinfo is None:
        deadline_at = deadline_at.replace(tzinfo=UTC)
    return (
        item.status == "pending"
        and deadline_at < now
    )


def normalize_deadline(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _priority_weight(priority: str | None) -> int:
    return _PRIORITY_ORDER.get((priority or "low").lower(), 1)


def sort_tasks_for_topn(items: list[MemoryItem]) -> list[MemoryItem]:
    def _deadline_key(item: MemoryItem) -> tuple[int, datetime]:
        normalized = normalize_deadline(item.deadline_at)
        if normalized is None:
            return (1, datetime.max)
        return (0, normalized)

    return sorted(
        items,
        key=lambda item: (
            -_priority_weight(item.priority),
            _deadline_key(item),
            item.created_at,
        ),
    )


def get_task_issue_reason(item: MemoryItem, now: datetime) -> str | None:
    if item.status == "done":
        return None
    if is_overdue(item, now):
        return "overdue"
    if item.validated is False:
        return "not_validated"
    if float(item.confidence or 0.0) < 0.6:
        return "low_confidence"
    return None


def classify_task_issues(items: list[MemoryItem], now: datetime) -> dict[str, list[MemoryItem]]:
    issues: dict[str, list[MemoryItem]] = {
        "low_confidence": [],
        "not_validated": [],
        "overdue": [],
    }
    for item in items:
        reason = get_task_issue_reason(item, now)
        if reason == "low_confidence":
            issues["low_confidence"].append(item)
        if reason == "not_validated":
            issues["not_validated"].append(item)
        if reason == "overdue":
            issues["overdue"].append(item)
    return issues


def get_pending_tasks_for_user(user_id: str, db) -> list[MemoryItem]:
    if not user_id.strip():
        return []
    rows = db.scalars(
        select(MemoryItem).where(
            MemoryItem.user_id == user_id,
            MemoryItem.status == "pending",
        )
    ).all()
    return list(rows)


def _fetch_pending_tasks_batch(user_id: str, db, limit: int = 50) -> list[MemoryItem]:
    if not user_id.strip():
        return []
    safe_limit = max(1, min(limit, 200))
    priority_weight = case(
        (MemoryItem.priority == "urgent", 4),
        (MemoryItem.priority == "high", 3),
        (MemoryItem.priority == "medium", 2),
        (MemoryItem.priority == "low", 1),
        else_=1,
    )
    rows = db.scalars(
        select(MemoryItem)
        .where(
            MemoryItem.user_id == user_id,
            MemoryItem.status == "pending",
        )
        .order_by(
            priority_weight.desc(),
            MemoryItem.deadline_at.asc().nulls_last(),
            MemoryItem.created_at.desc(),
        )
        .limit(safe_limit)
    ).all()
    return list(rows)


def get_memory_issues(user_id: str, db) -> dict[str, list[MemoryItem] | int]:
    now = now_utc()
    items = _fetch_pending_tasks_batch(user_id=user_id, db=db, limit=50)
    low_confidence_sorted: list[MemoryItem] = []
    unvalidated_sorted: list[MemoryItem] = []
    overdue_sorted: list[MemoryItem] = []
    low_confidence_total = 0
    unvalidated_total = 0
    overdue_total = 0

    for item in items:
        reason = get_task_issue_reason(item, now)
        if reason == "low_confidence":
            low_confidence_total += 1
            low_confidence_sorted.append(item)
        elif reason == "not_validated":
            unvalidated_total += 1
            unvalidated_sorted.append(item)
        elif reason == "overdue":
            overdue_total += 1
            overdue_sorted.append(item)

    needs_attention = low_confidence_total + unvalidated_total + overdue_total
    return {
        "low_confidence_tasks": low_confidence_sorted[:10],
        "unvalidated_tasks": unvalidated_sorted[:10],
        "overdue_tasks": overdue_sorted[:10],
        "low_confidence_total": low_confidence_total,
        "unvalidated_total": unvalidated_total,
        "overdue_total": overdue_total,
        "needs_attention": needs_attention,
    }


def _get_report_aggregates(user_id: str, db, now: datetime) -> dict[str, int]:
    now_moment_for_db = now.replace(tzinfo=None)
    now_norm = normalize_deadline(now)
    if now_norm is None:
        now_norm = now
    now_for_db = now_norm.replace(tzinfo=None)
    next_week_for_db = now_for_db + timedelta(days=7)
    aggregates = db.execute(
        select(
            func.count(MemoryItem.id).label("total"),
            func.sum(case((MemoryItem.status == "pending", 1), else_=0)).label("pending"),
            func.sum(case((MemoryItem.status == "done", 1), else_=0)).label("done"),
            func.sum(
                case(
                    (
                        and_(
                            MemoryItem.status == "pending",
                            MemoryItem.deadline_at.is_not(None),
                            MemoryItem.deadline_at < now_moment_for_db,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("overdue"),
            func.sum(
                case(
                    (
                        and_(
                            MemoryItem.status == "pending",
                            MemoryItem.priority == "high",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("high_priority"),
            func.sum(
                case(
                    (
                        and_(
                            MemoryItem.status == "pending",
                            MemoryItem.priority == "urgent",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("urgent"),
            func.sum(
                case(
                    (
                        and_(
                            MemoryItem.status == "pending",
                            MemoryItem.deadline_at.is_not(None),
                            MemoryItem.deadline_at >= now_for_db,
                            MemoryItem.deadline_at <= next_week_for_db,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("upcoming_deadlines"),
            func.sum(
                case(
                    (
                        and_(
                            MemoryItem.status == "pending",
                            MemoryItem.confidence < 0.6,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("low_confidence_pending"),
            func.sum(
                case(
                    (
                        and_(
                            MemoryItem.status == "pending",
                            MemoryItem.validated.is_(True),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("validated_pending"),
            func.sum(
                case(
                    (
                        and_(
                            MemoryItem.status == "pending",
                            MemoryItem.validated.is_(False),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("unvalidated_pending"),
        ).where(MemoryItem.user_id == user_id)
    ).one()
    return {
        "total": int(aggregates.total or 0),
        "pending": int(aggregates.pending or 0),
        "done": int(aggregates.done or 0),
        "overdue": int(aggregates.overdue or 0),
        "high_priority": int(aggregates.high_priority or 0),
        "urgent": int(aggregates.urgent or 0),
        "upcoming_deadlines": int(aggregates.upcoming_deadlines or 0),
        "low_confidence_pending": int(aggregates.low_confidence_pending or 0),
        "validated_pending": int(aggregates.validated_pending or 0),
        "unvalidated_pending": int(aggregates.unvalidated_pending or 0),
    }


def _counts_from_aggregates(aggregates: dict[str, int]) -> dict[str, int]:
    return {
        "pending": int(aggregates["pending"]),
        "done": int(aggregates["done"]),
        "validated_pending": int(aggregates["validated_pending"]),
        "unvalidated_pending": int(aggregates["unvalidated_pending"]),
        "low_confidence_pending": int(aggregates["low_confidence_pending"]),
        "overdue": int(aggregates["overdue"]),
    }


def get_counts(user_id: str, db) -> dict[str, int]:
    now = now_utc()
    aggregates = _get_report_aggregates(user_id=user_id, db=db, now=now)
    return _counts_from_aggregates(aggregates)


def calculate_attention_score(report_dict: dict) -> int:
    score = 100
    overdue = int(report_dict.get("overdue", 0))
    penalty_overdue = min(overdue, 3) * 15
    score -= penalty_overdue
    score -= int(report_dict.get("unvalidated_pending", 0)) * 5
    score -= int(report_dict.get("low_confidence_pending", 0)) * 3
    return max(0, min(100, score))


def get_today_focus(user_id: str, db) -> dict[str, list[MemoryItem]]:
    now = now_utc()
    today = normalize_deadline(now)
    if today is None:
        return {"today_tasks": [], "overdue": [], "urgent": []}
    items = _fetch_pending_tasks_batch(user_id=user_id, db=db, limit=50)
    today_sorted: list[MemoryItem] = []
    overdue_sorted: list[MemoryItem] = []
    urgent_sorted: list[MemoryItem] = []
    for item in items:
        deadline_normalized = normalize_deadline(item.deadline_at)
        item_is_overdue = is_overdue(item, now)
        if item_is_overdue:
            overdue_sorted.append(item)
        if item_is_overdue or (deadline_normalized is not None and deadline_normalized == today):
            today_sorted.append(item)
        if item.priority in {"urgent", "high"}:
            urgent_sorted.append(item)

    today_tasks_total = len(today_sorted)
    overdue_total = len(overdue_sorted)
    urgent_total = len(urgent_sorted)
    next_action: str | None = None
    if overdue_sorted:
        next_action = f"Начните с: {overdue_sorted[0].content}"
    elif urgent_sorted:
        next_action = f"Начните с: {urgent_sorted[0].content}"
    elif today_sorted:
        next_action = f"Начните с: {today_sorted[0].content}"
    return {
        "today_tasks": today_sorted[:10],
        "overdue": overdue_sorted[:10],
        "urgent": urgent_sorted[:10],
        "today_tasks_total": today_tasks_total,
        "overdue_total": overdue_total,
        "urgent_total": urgent_total,
        "next_action": next_action,
    }


def get_nudge_message(user_id: str, db) -> str:
    now = now_utc()
    counts = get_counts(user_id=user_id, db=db)
    overdue = int(counts["overdue"])
    unvalidated_pending = int(counts["unvalidated_pending"])
    low_confidence_pending = int(counts["low_confidence_pending"])
    now_for_db = now.replace(tzinfo=None)
    priority_weight = case(
        (MemoryItem.priority == "urgent", 4),
        (MemoryItem.priority == "high", 3),
        (MemoryItem.priority == "medium", 2),
        (MemoryItem.priority == "low", 1),
        else_=1,
    )
    overdue_condition = and_(
        MemoryItem.deadline_at.is_not(None),
        MemoryItem.deadline_at < now_for_db,
    )
    not_validated_condition = and_(
        MemoryItem.validated.is_(False),
        ~overdue_condition,
    )
    low_confidence_condition = and_(
        MemoryItem.validated.is_(True),
        MemoryItem.confidence < 0.6,
        ~overdue_condition,
    )
    base_pending = [
        MemoryItem.user_id == user_id,
        MemoryItem.status == "pending",
    ]

    def _task_hint(items: list[MemoryItem]) -> str:
        if not items:
            return ""
        selected = items[:3]
        labels: list[str] = []
        for item in selected:
            raw_content = (item.content or "").strip()
            short_content = raw_content[:28].rstrip()
            if short_content:
                labels.append(short_content)
            else:
                labels.append(str(item.id)[:8])
        hints = ", ".join(labels)
        if not hints:
            return ""
        return f" Примеры: {hints}."

    if overdue > 0:
        overdue_tasks = db.scalars(
            select(MemoryItem)
            .where(*base_pending, overdue_condition)
            .order_by(
                priority_weight.desc(),
                MemoryItem.deadline_at.asc().nulls_last(),
                MemoryItem.created_at.desc(),
            )
            .limit(3)
        ).all()
        return (
            f"У вас {overdue} просроченных задач. Начните с самой критичной."
            f"{_task_hint(list(overdue_tasks))}"
        )
    if unvalidated_pending > 3:
        unvalidated_tasks = db.scalars(
            select(MemoryItem)
            .where(*base_pending, not_validated_condition)
            .order_by(
                priority_weight.desc(),
                MemoryItem.deadline_at.asc().nulls_last(),
                MemoryItem.created_at.desc(),
            )
            .limit(3)
        ).all()
        return (
            "Есть задачи без подтверждения. Проверьте и утвердите их."
            f"{_task_hint(list(unvalidated_tasks))}"
        )
    if low_confidence_pending > 3:
        low_confidence_tasks = db.scalars(
            select(MemoryItem)
            .where(*base_pending, low_confidence_condition)
            .order_by(
                priority_weight.desc(),
                MemoryItem.deadline_at.asc().nulls_last(),
                MemoryItem.created_at.desc(),
            )
            .limit(3)
        ).all()
        return (
            "Есть задачи с низкой уверенностью. Требуется уточнение."
            f"{_task_hint(list(low_confidence_tasks))}"
        )
    return "Система в норме. Продолжайте работу."


def get_memory_report(user_id: str, db) -> dict:
    now = now_utc()
    aggregates = _get_report_aggregates(user_id=user_id, db=db, now=now)
    counts = _counts_from_aggregates(aggregates)
    total = int(aggregates["total"])
    pending = int(counts["pending"])
    done = int(counts["done"])
    overdue = int(counts["overdue"])
    high_priority = int(aggregates["high_priority"])
    urgent = int(aggregates["urgent"])
    upcoming_deadlines = int(aggregates["upcoming_deadlines"])
    low_confidence_pending = int(counts["low_confidence_pending"])
    validated_pending = int(counts["validated_pending"])
    unvalidated_pending = int(counts["unvalidated_pending"])
    hidden_tasks = int(unvalidated_pending)
    action_required = bool(overdue > 0 or unvalidated_pending > 3)
    main_problem: str | None = None
    if overdue > 0:
        main_problem = f"{int(overdue)} overdue tasks"
    elif unvalidated_pending > 3:
        main_problem = "too many unvalidated tasks"
    elif low_confidence_pending > 3:
        main_problem = "low confidence tasks detected"
    health = "green"
    if overdue > 0:
        health = "red"
    elif unvalidated_pending > 3 or low_confidence_pending > 3:
        health = "yellow"

    problems: list[dict[str, int | str]] = []
    if overdue > 0:
        problems.append({"type": "overdue", "count": int(overdue)})
    if unvalidated_pending > 0:
        problems.append({"type": "unvalidated", "count": int(unvalidated_pending)})
    if low_confidence_pending > 0:
        problems.append({"type": "low_confidence", "count": int(low_confidence_pending)})

    report_payload = {
        "total": int(total),
        "pending": int(pending),
        "done": int(done),
        "overdue": int(overdue),
        "high_priority": int(high_priority),
        "urgent": int(urgent),
        "upcoming_deadlines": int(upcoming_deadlines),
        "low_confidence_pending": int(low_confidence_pending),
        "validated_pending": int(validated_pending),
        "unvalidated_pending": int(unvalidated_pending),
        "action_required": action_required,
        "main_problem": main_problem,
        "hidden_tasks": hidden_tasks,
        "health": health,
        "problems": problems,
        "generated_at": now_utc(),
    }
    report_payload["attention_score"] = calculate_attention_score(report_payload)
    return report_payload
