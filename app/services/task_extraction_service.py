import json
from dataclasses import dataclass

from openai import OpenAI

from app.infrastructure.config.settings import settings


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
                    "Extract action items from meeting transcript. "
                    "Return JSON object with key 'tasks' as array. "
                    "Each task must contain: description, "
                    "assignee_speaker_label, due_date, priority, "
                    "source_quote, confidence."
                ),
            },
            {
                "role": "user",
                "content": transcript_text,
            },
        ],
    )
    content = completion.choices[0].message.content
    if content is None:
        return []

    payload = json.loads(content)
    tasks_data = payload.get("tasks", [])
    tasks: list[ExtractedTask] = []
    for item in tasks_data:
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        priority = str(item.get("priority", "medium")).strip().lower()
        if priority not in {"low", "medium", "high"}:
            priority = "medium"
        confidence_raw = item.get("confidence")
        confidence = (
            float(confidence_raw) if confidence_raw is not None else None
        )
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
