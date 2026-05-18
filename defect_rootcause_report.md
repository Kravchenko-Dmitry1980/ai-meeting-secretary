# Defect Root Cause Report

## Scope

Проанализированы дефекты:
1. English summary leakage
2. Useless task extraction
3. Dirty transcript text
4. Timeline 0s bug

Слои анализа: prompt / backend / frontend / diarization / stt.

---

## 1) English Summary Leakage

### Наблюдение
- В продовом флоу summary иногда приходит на английском.

### Root cause (primary)
- **Prompt/LLM layer:** модель `gpt-4o-mini` + сложный контекст с `SPEAKER_*` и mixed lexical domain может давать language drift даже при RU-инструкции.
- **Language guard limitation:** текущий RU guard в `summary_service.py` использует эвристику доли кириллицы (`>= 0.6`) и 1 retry. Это снижает риск, но не дает 100% гарантию.

### Root cause (contributing)
- **Backend runtime drift:** есть признаки, что в окружении периодически работает не последняя сборка (поведение summary в реальных прогонах ранее не соответствовало последним prompt-ограничениям).

### Layer verdict
- Primary: **prompt + runtime deployment state**
- Secondary: **backend guard robustness**

---

## 2) Useless Task Extraction

### Наблюдение
- Задачи иногда пустые или малоценные.

### Root cause (primary)
- **Prompt quality:** extraction prompt уже улучшен, но:
  - нет few-shot примеров,
  - нет строгих правил actionable quality (глагол действия, критерий завершения),
  - нет дедупликации на уровне промпта/постобработки.
- **Input quality:** в extractor подается speaker-aware transcript с техническими метками (`SPEAKER_XX [start-end]: ...`), что ухудшает семантический сигнал для задач.

### Root cause (contributing)
- **Diarization/STT noise:** ошибки сегментации и speaker assignment искажают ответственность/контекст.
- **No fallback strategy:** при пустом extraction нет вторичного pass (например, soft heuristic extractor).

### Layer verdict
- Primary: **prompt + backend post-processing gaps**
- Secondary: **diarization/stt quality**

---

## 3) Dirty Transcript Text

### Наблюдение
- Текст транскрипта воспринимается "грязным": технические метки, шум, неравномерный стиль.

### Root cause (primary)
- **Backend representation choice:** в пайплайне transcript хранится как raw STT `full_text` (join сегментов), а для summary/tasks используется speaker-aware текст с техническим форматом.
- **No transcript cleanup stage:** отсутствует отдельный normalization pass (punctuation cleanup, filler removal, whitespace normalization, sentence boundaries).

### Root cause (contributing)
- **STT model constraints:** `faster-whisper` tiny/int8 на CPU повышает риск артефактов и менее качественной пунктуации.
- **Frontend fallback parser:** при старом контракте строковый transcript разбивается эвристикой, что может добавлять "грязь" в UI.

### Layer verdict
- Primary: **stt + backend text normalization gap**
- Secondary: **frontend fallback parsing mode**

---

## 4) Timeline 0s Bug

### Наблюдение
- На таймлайне спикеров встречаются интервалы с `0s`.

### Root cause (primary)
- **Frontend mapping fallback:** в `meetingApi.ts` для segments используется:
  - `start: Number(record.start ?? 0) || 0`
  - `end: Number(record.end ?? 0) || 0`
  Если backend прислал `start_sec/end_sec` (старый/legacy shape) или пустые поля — фронт принудительно ставит 0.

### Root cause (contributing)
- **Contract transition mismatch:** часть окружений/сборок могла возвращать разные shapes (`start/end` vs `start_sec/end_sec`), и fallback логика фронта не учитывает оба поля для segments.
- **Diarization fallback path:** при проблемной диаризации интервалы могут быть менее точными, но не должны быть массово 0 без маппинг-проблемы.

### Layer verdict
- Primary: **frontend contract mapping**
- Secondary: **backend contract consistency across environments**

---

## Cross-Cutting Systemic Causes

1. **Contract drift между сборками** (backend/frontend runtime не всегда синхронизированы с последними изменениями).
2. **Недостаток observability** по качеству LLM outputs (RU compliance, parse quality, task usefulness).
3. **Отсутствие quality gates** на данных pipeline (transcript cleanliness, segment validity, task actionability).

---

## Root Cause Matrix (short)

- English leakage -> Prompt/LLM + runtime drift
- Weak tasks -> Prompt design + no fallback + noisy transcript input
- Dirty transcript -> STT quality + no cleanup stage
- Timeline 0s -> Frontend segment mapping fallback + contract mismatch
