# Prompt Audit Report

## Prompt Inventory

1. `app/services/summary_service.py` -> `summarize_transcript_text()`
   - **Purpose:** Генерация итогового summary встречи.
   - **Type:** system + user prompt, free-form text output.

2. `app/services/task_extraction_service.py` -> `extract_tasks_from_transcript()`
   - **Purpose:** Извлечение задач из расшифровки.
   - **Type:** system + user prompt, JSON extraction prompt (`response_format=json_object`).

3. `app/workers/`, `app/utils/`, top-level `services/`, `utils/`
   - **Result:** prompt templates/hidden builders/inline prompt strings не найдены.

## Critical Problems

1. **Summary prompt не гарантирует машинно-валидируемую структуру**
   - Сейчас output свободный текст с разделами.
   - Нет JSON schema или строгого формата, поэтому фронт/аналитика не могут надежно парсить key decisions/risks/actions.

2. **Нет явной анти-галлюцинационной политики**
   - Ни в summary, ни в tasks нет жесткого правила "не выдумывать факты вне transcript".
   - Бизнес-риск: ложные решения/задачи.

3. **Слабая failure resilience для JSON extraction**
   - В `task_extraction_service.py` нет `try/except` на `json.loads`, валидации типа `tasks_data`, контроля NaN/inf.
   - Возможны падения пайплайна при нестабильном model output.

4. **Локализация summary не защищена от drift**
   - Есть инструкция "строго на русском", но нет пост-валидации языка.
   - Уже наблюдались English outputs в продовом потоке.

## Medium Problems

1. **Недостаточная детерминированность summary**
   - `temperature=0.1` лучше, чем 0.2, но для протоколов встреч обычно нужен `0.0`.

2. **Слишком общий user prompt в summary**
   - Передается весь transcript как есть, без ограничений длины/фокуса.
   - При длинных встречах растут токены и шум.

3. **Task extraction не требует дедупликации и нормализации задач**
   - Нет явного правила объединять дубликаты/переформулировки.

4. **Нет few-shot примеров**
   - Для сложной экстракции неявных задач это снижает стабильность и качество.

5. **Не хватает бизнес-ограничений**
   - Нет требований по actionable quality (глагол действия, проверяемость результата, приоритетная логика).

## Quick Wins

1. Для summary перейти на строгий JSON output:
   - `{"summary":"...","key_decisions":[...],"risks":[...],"next_steps":[...]}`

2. Добавить в оба system prompt правило:
   - "Используй только факты из расшифровки. Если данных нет, возвращай null/пустой список."

3. В `task_extraction_service.py` добавить безопасный parse:
   - `try/except JSONDecodeError`, fallback `[]`, числовая санитаризация confidence.

4. Снизить `temperature` summary до `0.0`.

5. Добавить язык-валидатор после генерации summary:
   - если текст не RU (эвристика/библиотека), выполнить повторный strict regeneration.

## Prompts That Need Rewrite First

1. `app/services/task_extraction_service.py::extract_tasks_from_transcript`
   - Наиболее критичен для downstream ценности продукта (tasks -> actionability).
   - Требует schema-hardening + fallback + anti-hallucination.

2. `app/services/summary_service.py::summarize_transcript_text`
   - Требует структурного output и гарантий русского языка.

## Recommended Unified Prompt Standard

Единый стандарт для всех LLM-вызовов:

1. **Role**
   - Четкая роль и бизнес-контекст (meeting intelligence assistant).

2. **Hard Constraints**
   - Только факты из transcript.
   - Строго русский язык.
   - Запрещено домысливание.

3. **Output Contract**
   - Только JSON по фиксированной schema.
   - Явные null/[] при отсутствии данных.

4. **Quality Rules**
   - Дедупликация, краткость, деловой стиль.
   - Для задач: action verb + owner inference rules + date normalization.

5. **Recovery**
   - Retry on invalid JSON / invalid language / empty critical fields.

6. **Cost Guardrails**
   - Ограничение длины входа (chunking/salient extract).
   - Лимит output токенов.

## Token Waste Areas

1. Полный transcript передается целиком в summary/task extraction без сжатия.
2. Дублируется контекст speaker-меток, но не выполняется pre-filter salient segments.
3. Нет stage-wise summarization (chunk -> partial summaries -> final merge).
4. Нет dynamic prompt size control по длительности встречи.

## Russian Localization Gaps

1. Инструкции на русском есть, но нет пост-валидации языка.
2. Нет fallback-механизма "перегенерировать на русском" при EN output.
3. В summary отсутствует явный запрет англицизмов/англоязычных заголовков.

## Final Score (0-10)

**6.2 / 10**

Код уже в рабочем состоянии и использует правильные базовые паттерны (role messages, JSON format для extraction), но production-уровню мешают:
- отсутствие строгой schema/валидации для summary,
- слабая защита от hallucination и language drift,
- недостаточная resilience на невалидный JSON,
- высокая стоимость на длинных транскриптах.
