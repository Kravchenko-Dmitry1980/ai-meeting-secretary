import logging
import json

from openai import OpenAI

from app.infrastructure.config.settings import settings

logger = logging.getLogger(__name__)


SummaryPayload = dict[str, str | list[str]]


def _safe_summary_payload() -> SummaryPayload:
    return {
        "summary": "Недостаточно данных в расшифровке для формирования итогов.",
        "key_decisions": [],
        "risks": [],
        "next_steps": [],
    }


def _normalize_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_summary_payload(raw_content: str | None) -> SummaryPayload:
    if not raw_content:
        logger.warning("OpenAI returned empty summary content")
        return _safe_summary_payload()
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError:
        logger.warning("Failed to parse summary JSON payload")
        return _safe_summary_payload()
    if not isinstance(payload, dict):
        logger.warning("Summary payload is not an object")
        return _safe_summary_payload()

    summary_text = str(payload.get("summary", "")).strip()
    return {
        "summary": summary_text or _safe_summary_payload()["summary"],
        "key_decisions": _normalize_list(payload.get("key_decisions")),
        "risks": _normalize_list(payload.get("risks")),
        "next_steps": _normalize_list(payload.get("next_steps")),
    }


def _is_russian_text(value: str) -> bool:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return True
    cyrillic_count = sum(1 for char in letters if "а" <= char.lower() <= "я" or char.lower() == "ё")
    return (cyrillic_count / len(letters)) >= 0.6


def _is_payload_russian(payload: SummaryPayload) -> bool:
    fragments: list[str] = [str(payload.get("summary", ""))]
    for key in ("key_decisions", "risks", "next_steps"):
        value = payload.get(key, [])
        if isinstance(value, list):
            fragments.extend(str(item) for item in value)
    return _is_russian_text(" ".join(fragments))


def _render_summary(payload: SummaryPayload) -> str:
    summary_text = str(payload.get("summary", "")).strip() or _safe_summary_payload()["summary"]
    key_decisions = payload.get("key_decisions", [])
    risks = payload.get("risks", [])
    next_steps = payload.get("next_steps", [])

    def _section(title: str, values: object) -> str:
        if not isinstance(values, list) or not values:
            return f"{title}\n- Нет данных"
        return f"{title}\n" + "\n".join(f"- {str(item)}" for item in values)

    return "\n\n".join(
        [
            f"Краткое резюме\n{summary_text}",
            _section("Ключевые решения", key_decisions),
            _section("Риски и блокеры", risks),
            _section("Следующие шаги", next_steps),
        ]
    )


def _request_summary_payload(client: OpenAI, transcript_text: str) -> SummaryPayload:
    completion = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты корпоративный ассистент по протоколам встреч. "
                    "Всегда отвечай строго на русском языке. "
                    "Используй только факты из расшифровки. "
                    "Если данных нет — укажи это явно. "
                    "Верни только JSON-объект строго по схеме: "
                    "{\"summary\":\"...\",\"key_decisions\":[],\"risks\":[],\"next_steps\":[]}. "
                    "Все поля обязательны."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Подготовь структурированный итог встречи по расшифровке:\n\n"
                    f"{transcript_text}"
                ),
            },
        ],
    )
    return _parse_summary_payload(completion.choices[0].message.content)


def summarize_transcript_text(transcript_text: str) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = OpenAI(api_key=settings.openai_api_key)
    payload = _request_summary_payload(client, transcript_text)

    if not _is_payload_russian(payload):
        logger.warning("Summary is not Russian, running one repair retry")
        repair_completion = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Преобразуй входной JSON строго на русский язык. "
                        "Используй только исходные факты. "
                        "Верни только JSON по схеме: "
                        "{\"summary\":\"...\",\"key_decisions\":[],\"risks\":[],\"next_steps\":[]}."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        repaired = _parse_summary_payload(repair_completion.choices[0].message.content)
        if _is_payload_russian(repaired):
            payload = repaired
        else:
            logger.warning("Summary repair retry failed, using best-effort payload")

    return _render_summary(payload).strip()
