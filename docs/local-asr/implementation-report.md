# Нейросекретарь: выполненная model-free фаза

**Обновление 27.10.2026 — подготовка этапа 2 завершена, inference ожидает RAM.**
NVIDIA RTX 4060 включена, CUDA подтверждена из PyTorch. Отдельные CPU RNNT/CTC
и GPU Nemotron окружения установлены с hash-locked зависимостями; все три веса
проверены локально. Полный suite: 431 PASS; финальные focused проверки:
39 bootstrap + 63 postprocess PASS (наборы пересекаются). Реальный прогон ещё
не выполнялся: свободная RAM ниже 6656 MiB; пользователь сейчас не может
освободить больше. Production остаётся выключенным. Точный текущий статус,
evidence и готовая команда продолжения: [model-runtime-stage2.md](model-runtime-stage2.md).

**Обновление 27.10.2026 — этап подключения исполнителя завершён.**
API → очередь Worker → native controller/stages → SQLite → текст/экспорт/плеер
проверены сквозным синтетическим запуском. Итоги и анализ голосов принимают
готовую локальную расшифровку. **409 local-ASR + 201 regression tests PASS**,
без ошибок и пропусков; TypeScript typecheck PASS. Модели не запускались,
live приложение и сервер не переключались. Текущая граница готовности,
квитанции и следующий этап: [portable-executor-stage1.md](portable-executor-stage1.md).
Остальной документ сохраняет историю предыдущих фаз.

26 октября 2026. Рабочая область: текущий Secretary checkout.

**Результат:** доступная локальная реализация из CURSOR_SECRETARY_IMPLEMENTATION_PROMPT
v2 выполнена и проверена. Подготовлены адаптеры, immutable storage, ручной UI,
job/resource contracts, restore compatibility и документы следующей квалификации.
Это **не включённый локальный ASR и не завершённая SERVER integration**.

**Дополнение 26.10.2026: архив со стационарного компьютера получен.** Проверены
3378 файлов передачи и 65 файлов canonical release. Добавлен адаптер фактических
JSON-артефактов пакета и повторная проверка source integrity. После этих изменений
общий local-ASR suite — **237 PASS**. Текущий результат,
пределы и доказательства: [source-handoff-intake.md](source-handoff-intake.md).
Нижеприведённые результаты первой фазы сохраняются как история; model runtime
остаётся выключенным и не квалифицированным.

**Следующий этап 26.10.2026: полный package test gate пройден.** В отдельной
среде установлены 19 hash-locked пакетов, native process ownership адаптирован
через Windows Job Objects. Исходные **198 Python tests + 6 PowerShell scripts**
прошли без ошибок и пропусков. Оригинальный пакет сохранён; изменяется только
производная тестовая копия. Полный текущий local-ASR regression suite: **281 PASS**.
Отчёт и повторный запуск:
[full-package-test-runtime.md](full-package-test-runtime.md).

## Статусы

| Область | Подтверждённый результат |
| --- | --- |
| Source Secretary adapters | Реализованы; model-free HTTP/SQLite/UI проверки PASS |
| Canonical inference package | Source integrity 3378/3378, canonical source gate65/65. Полный frozen test gate на адаптированной копии: 198 Python + 6 PowerShell PASS; model runtime не квалифицирован |
| RNNT provenance correction | Original manifest и desktop weight-hash receipt получены; ref сохранён без изменений, локальная повторная проверка весов на target ещё требуется |
| Среды | Отдельная тестовая среда с 19 hash-locked пакетами собрана и проверена. Existing Secretary/ECAPA сохранены; новые RNNT/CTC/Nemotron environments и clean rebuild НЕ выполнены |
| Admission | На ноутбуке RAM snapshot ниже guard, VRAM unknown; целевой desktop НЕ проверен; разрешения на inference нет |
| Model inference / quality | НЕ выполнялись; WER/CER/DER новых результатов не измерены |
| SERVER deployment | НЕ выполнялся; текущий live desktop недоступен, карта основана на проверенном доступном source |
| Расходы | Новых paid requests не выполнялось. Стоимость локальных вычислений не оценивалась |
| Рабочее приложение | Не перезапускалось, live DB/конфигурация/voice bank не мигрировались, provider не переключался |
| Git | Исходные пользовательские изменения сохранены; staging/commit/push/merge не выполнялись |

## Что изменено

- Domain v2 DTO с отдельным sidecar: words, raw turns, source mapping, warnings,
  diagnostics, model/loader/processor/custom-code hashes и точная device identity.
- Immutable SQLite manifests, analysis revisions и idempotent import с проверкой
  настоящего server-owned input hash и исходного текста. Отдельный JSON export
  сохраняет words/overlap/unknown и ручные решения.
- Новый локальный API наследует Host/Origin/CSRF; scope задаёт backend. Browser
  не получает model service token и не передаёт execution plan/permission/path.
- Ручной интерфейс во вкладке участников: несколько аудиопримеров, точный список
  реплик, исключения, явное подтверждение и отдельные overrides без machine proposed.
  Прежние source confirmations и ручное «без имени» сохраняют приоритет.
- Saved-text alignment seam валидирует токены и окна; при несовместимости сохраняет
  прежний текст/времена и явную диагностику, не запускает RNNT повторно.
- Асинхронный lifecycle прикреплён к существующим jobs. Замороженный plan,
  attempt/stage IDs, lease/fence, bounded recovery, checkpoint identity и atomic
  apply проверены на синтетическом исполнении. Нет второго broker/worker loop.
- Общий lock имеет physical GPU identity. CPU-ASR с GPU diarizer занимает тот же
  device. Admission проверяется перед стадией. Runtime error сохраняет durable
  quarantine; restart приложения не разблокирует устройство автоматически.
- Restore поддерживает новые таблицы и старые схемы: артефакты/ручная история
  сохраняются, старые незавершённые local jobs становятся restore_blocked/no-replay.

В прежних файлах — узкое API/startup/worker/cancel/UI wiring и restore compatibility.
`ProcessingJob.stage` дополнен `local_asr`; OpenAPI и TypeScript types пересозданы.
После генерации обнаружен конфликт прежнего `Required<...>` с optional
`assignee_profile_id`; исправлен только типовой adapter, runtime payload не менялся.
Предсуществовавшая схема 11 добавлена в закрытый restore catalogue после отдельного
воспроизведения несовместимости; реальные старые schema9/10 fixtures сохранены.

## Матрица десяти обязательных проверок

| Требование prompt §10 | Доказательство / предел |
| --- | --- |
| 1. DTO → storage → read → UI/export, words/overlap/unknown | contract/repository/API tests; реальный browser + SQLite export |
| 2. seconds/ms, counts/ordinals, mixed, empty alignment | contract/alignment/media tests, strict rejection nonfinite/bool/ranges; миллисекунды в UI |
| 3. Idempotent import, неизменность оплаченного текста/route/summary/tasks/manual | repository/API tests с **непустыми** paid receipts, manual assignee и manual unassigned; browser unchanged fingerprints |
| 4. Единственный claim, stale owner не публикует | lifecycle contention/fence tests; actual persisted manifest проверяется against plan внутри transaction |
| 5. Cancel/timeout и previous owner | lifecycle/resource/owned-process fixtures; expired lease не доказательство exit. Native Job Object containment ещё NOT_QUALIFIED |
| 6. Другой input/model/code/profile/device checkpoint | lifecycle negative matrix, CPU/CUDA/precision/profile mismatch; incompatible publication откатывает import+receipt |
| 7. Unknown paid POST без replay, receipt before parse/once | Existing Polza/worker baseline и affected regressions; HTTPX default retries=0; новый путь не вызывает provider |
| 8. Admission refusal без ослабления/foreign kill/cloud fallback | resource negative tests; RAM6656/commit9216 reserves, CUDA freeVRAM10240/allocator8192, RSS4608/job commit8192 caps preserved |
| 9. Exact shown cluster set, exclusions/manual priority | repository/API tests и реальный browser POST→refresh→export |
| 10. Token/scope | Host/Origin/CSRF, wrong meeting/owner/run negatives, no service-token DTO, server-only profile factory |

Автоматические имена нового кластера не включены без подходящей калибровки. ECAPA
сохранён; шесть model files совпали с существующим manifest. Veche для identity
не использовался. Политики Polza, лимиты расходов, модели итогов/задач/команд и
платный retry semantics не изменены этой работой.

## Выполненные проверки

| Проверка | Результат | Evidence |
| --- | --- | --- |
| Все новые `tests/test_local_asr*.py` через offline guard | **171 PASS**, 45.49 s | `.runtime/team-rollout/offline-d4987a752586420cb2ebc6e9dfe218e5/junit.xml` |
| Затронутые backend/API/worker/OpenAPI/cancel/STT regressions | **200 PASS**, 167.47 s | `.runtime/team-rollout/offline-22513dace30f4cb78c7d1052e8725503/junit.xml` |
| Affected backup/restore compatibility | **64 PASS**, 14.20 s | `.runtime/team-rollout/offline-005b301c698f4545a9748b2503d33b76/junit.xml` |
| Frontend suite | **341 PASS**, 0 failed/skipped | `npm run test:processing` |
| TypeScript / ESLint | PASS | `npm run typecheck`, `npm run lint` |
| Production frontend build | PASS, isolated output | `.runtime/local-asr-build-20261026-final`, основной `frontend/dist` не заменялся |
| Browser | **17 assertions PASS + 10 evidence checks PASS** | [frontend-validation.md](frontend-validation.md) |
| Изменения tracked source | `git diff --check` exit 0 | Нет whitespace errors; предупреждения LF→CRLF |
| Dependency/provenance verifier | Expected **BLOCKED/exit2** | `local_preparation_hashes_match=true`, source bundle missing; не подменяет runtime PASS |

Числа разных suites частично пересекаются; это не сумма уникальных тестов.
Полный огромный backend/team suite и отсутствующий frozen source-package gate
не запускались, весь репозиторий зелёным не объявляется. Существующее предупреждение
Starlette/httpx deprecated сохраняется; зависимости ради него не обновлялись.

Основные команды из корня:

```powershell
.\.venv\Scripts\python.exe -B scripts/run_local_asr_tests.py
.\.venv\Scripts\python.exe -B scripts/run_local_asr_tests.py tests/test_openapi_export.py tests/test_job_contract.py tests/test_speaker_api.py tests/test_backend.py tests/test_api_regressions.py tests/test_stt_cap_resume.py tests/test_cancel_completion_regression.py tests/test_cancel_review_regression.py -q
.\.venv\Scripts\python.exe -B scripts/run_local_asr_tests.py tests/test_local_asr_restore.py tests/test_local_backup_cli.py tests/test_restore_quarantine_preparation_validation.py -q
.\.venv\Scripts\python.exe -B scripts/export_openapi.py
```

Из `frontend`: `npm run api:types`, `npm run typecheck`, `npm run lint`,
`npm run test:processing`,
`node node_modules/vite/bin/vite.js build --outDir ../.runtime/local-asr-build-20261026-final`.
Для повторной сборки выбирать **новый** isolated output, не заменять рабочий dist.
Offline launcher не удаляет прошлые test artifacts и запрещает network/live-data access.

## Независимое ревью и ограничения

Исправлены выявленные дефекты: partial raw-turn coverage; безопасность открытия media
через descriptor identity/reparse/hardlink checks; response DTO нового stage;
привязка реального persisted run к плану; исходные RSS/commit caps; восстановление
расширенной БД. Повторное независимое чтение двух lifecycle/resource замечаний
подтвердило их устранение.

Browser использовал реальный компонент/API/SQLite и 5 секунд synthetic silence.
Это проверка workflow, не качество голосов. Нативное media-preview может остановиться
на 0.1–0.23 s позднее границы; sample-accurate WAV clipping и человеческое прослушивание
не проверены. Общий device lock и sampled RSS не заменяют квалифицированные native
Job Object/container ограничения; job commit enforcement пока NOT_ENFORCED.

Scoped Obsidian recall был недоступен из-за отсутствия MCP roots; глобальных notes
для этой области не было. Решения сверялись по текущим исходникам и документам.
Новые записи в глобальную память не делались.

## Следующее конкретное действие

Получить на целевом desktop canonical source/evidence bundle и его текущие
manifest/locks/custom code, проверить RNNT weights hash, затем выполнить source gate
и clean rebuild по [плану переноса и квалификации](transfer-and-qualification.md).
Исправление RNNT pin — только после этого подтверждения. Точные CPU runner flags
без исходного bundle не выдуманы.

После отдельного возобновления inference и PASS admission: historical CPU RNNT/CTC
+ фактический Nemotron device, short smoke, frozen exposed regression/cross-domain,
long recovery/cancel; CUDA — отдельный профиль. Готовую встречу не транскрибировать
повторно. SERVER cutover требует отдельного поручения и integration evidence.

Другие материалы: [ADR](adr-shared-core.md), [контракты](contracts.md),
[lifecycle](lifecycle.md), [runtime audit](runtime-audit.md),
[clean rebuild](clean-rebuild.md), [restore](restore-compatibility.md),
[rollback](rollback.md), [baseline](baseline.md).

## Идентичность входных документов

| Документ пользователя | SHA-256 |
| --- | --- |
| CURSOR_SECRETARY_IMPLEMENTATION_PROMPT.md | `01a3c779d9e5c6512ae87f93268f7ffc1493ab7fd29c19428382d30fbbaf85eb` |
| NEUROSECRETARY_ASR_DIARIZATION_HANDOFF_2026-10-26 (1).md | `36db3d2cdd236f7fefe8b5f2a3c3590d5613ef272e0df9755a2ba631e3311348` |
| SECRETARY_STRATEGY_2026-10-26.md | `42cbc4cc80bf6ee50c315f27041ff683edcb8f4b3723a2f6261702df875b04f2` |
