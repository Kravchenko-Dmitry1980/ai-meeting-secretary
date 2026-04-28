# Priority Fix Plan

## Goal

Устранить 4 ключевых дефекта без ломки продукта:
- English summary leakage
- weak task extraction
- dirty transcript text
- timeline 0s bug

---

## Priority P0 (Immediate, low-risk)

### P0.1 Runtime consistency gate
- Зафиксировать, что API/worker/frontend работают на одной актуальной сборке.
- Добавить короткий smoke-check на старте (version/commit stamp в health/логах).
- Outcome: устранение contract drift как источника “фантомных” дефектов.

### P0.2 Timeline mapping hardening
- Во фронтенд-маппинге segments поддержать оба ключа:
  - `start/end`
  - `start_sec/end_sec`
- Добавить guard: сегменты с `end <= start` исключать/логировать.
- Outcome: убрать 0s-баг в UI.

### P0.3 Summary RU leakage guard hardening
- Усилить guard после retry:
  - если RU не прошел, выполнять deterministic fallback summary в RU шаблоне.
- Outcome: практическая гарантия RU на выходе.

---

## Priority P1 (High business impact)

### P1.1 Task extraction usefulness upgrade
- В prompt добавить:
  - критерии actionable задач (глагол + результат),
  - дедупликацию,
  - запрет “общих” задач без действия.
- Добавить post-filter:
  - отбрасывать пустые/неоперационные формулировки,
  - confidence threshold + reason logging.
- Outcome: рост полезности задач для бизнеса.

### P1.2 Transcript cleanup stage
- В backend добавить normalization step перед LLM:
  - trim noise/fillers,
  - normalize punctuation,
  - compact whitespace,
  - optional sentence segmentation.
- Outcome: cleaner transcript и лучшее качество summary/tasks.

---

## Priority P2 (Quality + Cost optimization)

### P2.1 LLM observability
- Метрики:
  - RU compliance rate,
  - JSON parse pass rate,
  - avg tasks per meeting,
  - empty task rate.
- Outcome: прозрачность деградаций.

### P2.2 Long transcript strategy
- Chunk + merge pipeline для summary/tasks.
- Outcome: стабильность и снижение token waste.

---

## Per-Defect Fix Mapping

1. English summary leakage
- P0.1, P0.3, P2.1

2. Useless task extraction
- P1.1, P1.2, P2.1

3. Dirty transcript text
- P1.2, P2.2

4. Timeline 0s bug
- P0.2, P0.1

---

## Verification Plan

### Smoke (каждый релиз)
1. Upload test audio
2. Wait status=done
3. Validate:
   - summary in Russian
   - tasks non-empty/usable
   - transcript readable
   - timeline has valid non-zero intervals

### Acceptance thresholds
- RU summary compliance: >= 99%
- Empty task rate: < 15% (на реальных business calls)
- Invalid segment rate (`end <= start`): < 1%
- Frontend timeline 0s incidents: 0

---

## Recommended Execution Order

1. P0.1 (runtime consistency)
2. P0.2 (timeline fix)
3. P0.3 (summary guard hardening)
4. P1.1 (task quality)
5. P1.2 (transcript cleanup)
6. P2.* (observability + optimization)
