import json
import logging
from dataclasses import dataclass
import math

from openai import OpenAI

from app.infrastructure.config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class ExtractedTask:
    description: str
    assignee_speaker_label: str | None
    due_date: str | None
    priority: str
    source_quote: str
    confidence: float | None


def extract_tasks_from_transcript(transcript_text: str) -> list[ExtractedTask]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=settings.openai_api_key)
    completion = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты аналитик задач по итогам встреч. "
                    "Извлеки ЯВНЫЕ и НЕЯВНЫЕ задачи из расшифровки. "
                    "Используй только факты из расшифровки. "
                    "Если данных нет — укажи это явно. "
                    "Верни только JSON-объект формата: "
                    "{\"tasks\": [{description, assignee_speaker_label, due_date, priority, source_quote, confidence}]}. "
                    "description: коротко и по-русски, в деловом стиле. "
                    "assignee_speaker_label: SPEAKER_XX если можно определить, иначе null. "
                    "due_date: YYYY-MM-DD если есть явный срок, иначе null. "
                    "priority: low|medium|high (default medium). "
                    "source_quote: точная цитата или близкая формулировка из расшифровки. "
                    "confidence: 0..1. "
                    "Если задач нет, верни tasks: []."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Расшифровка встречи:\n"
                    f"{transcript_text}\n\n"
                    "Извлеки список задач в требуемом JSON-формате."
                ),
            },
        ],
    )
    content = completion.choices[0].message.content
    if content is None:
        logger.warning("OpenAI returned empty content for task extraction")
        return []

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Failed to parse task extraction JSON")
        return []
    if not isinstance(payload, dict):
        logger.warning("Task extraction payload is not an object")
        return []

    tasks_data = payload.get("tasks", [])
    if not isinstance(tasks_data, list):
        logger.warning("Task extraction payload has non-list 'tasks' field")
        return []
    tasks: list[ExtractedTask] = []
    for item in tasks_data:
        if not isinstance(item, dict):
            logger.warning("Task extraction item has invalid type")
            continue
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        priority = str(item.get("priority", "medium")).strip().lower()
        if priority not in {"low", "medium", "high"}:
            priority = "medium"
        confidence_raw = item.get("confidence")
        confidence: float | None = None
        if confidence_raw is not None:
            try:
                parsed_confidence = float(confidence_raw)
                if math.isfinite(parsed_confidence):
                    confidence = max(0.0, min(1.0, parsed_confidence))
                else:
                    logger.warning("Task confidence is not finite")
            except (TypeError, ValueError):
                logger.warning("Task confidence has invalid value")
        tasks.append(
            ExtractedTask(
                description=description,
                assignee_speaker_label=item.get("assignee_speaker_label"),
                due_date=item.get("due_date"),
                priority=priority,
                source_quote=str(item.get("source_quote", "")).strip(),
                confidence=confidence,
            )
        )
    return tasks
