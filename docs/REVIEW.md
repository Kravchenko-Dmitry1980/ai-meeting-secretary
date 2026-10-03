# Независимое ревью Secretary V1

Дата: 2026-10-01. Корень: `D:\AI\Projects\Active\Secretary`.

Ревью сопоставляет новую реализацию с исходным промптом, особенно с требованиями о сохранении аудио, восстановлении jobs, атомарном захвате, повторе нужного этапа, provenance, неизвестной стоимости и localhost security. Исходники backend проверялись READ ONLY. Ревьюер и его подзадачи не меняли код реализации, donor repositories или процессы; разрешённые результаты — этот документ и `docs/audit/OTHER_DONORS.md`.

## Вывод и границы

**FACT:** ошибки R01–R13 воспроизведены на синтетических данных, переданы владельцам реализации, исправлены и проверены повторно. На проверенном snapshot открытых подтверждённых P1/P2 из этого ревью нет.

**FACT:** локальные проверки API, хранения, worker, Polza adapter и capture contract прошли. Это не подтверждает качество STT/LLM, фактический расход/доступность конкретного аккаунта Polza или настоящую запись Windows microphone/WASAPI loopback.

**INTERPRETATION:** текущая реализация пригодна для следующего контролируемого пользовательского smoke test. Полную end-to-end приёмку исходного промпта нельзя объявлять по mock HTTP и синтетическому PCM.

## Найденные ошибки и проверка исправлений

Evidence пути ниже относительны указанному корню. Номера строк относятся к финальному snapshot исправления, первоначальные результаты repro приведены отдельно.

| ID / приоритет | Первоначальный дефект и результат repro | Исправление / evidence | Повторная проверка |
|---|---|---|---|
| R01 P1 | GET poll network failure у уже принятой async STT превращался в failed; ручной retry создавал новую job без provider ID. Repro: existing-123 сохранён, original failed, new_job_created=true, retry_provider_job_id=null | Retryable ошибка с сохранённым ID продолжает ту же job; известный async ID не теряется. `backend/secretary/application/worker.py`, `backend/secretary/infrastructure/polza.py` | Один accepted ID, polling resume без нового submit; соответствующая regression и собственный tempfile repro |
| R02 P1 | После первого успешного summary batch и 429 на втором весь этап автоматически повторялся. Real PolzaClient + MockTransport: идентичный оплаченный first batch отправлен 2 раза | SQLite `summary_checkpoints`, hash точного payload/model, проверка grounding и receipt при cache hit, explicit resume той же job. Автоматический full-summary rate retry остановлен. `backend/secretary/infrastructure/database.py`, `backend/secretary/infrastructure/polza.py`, `backend/secretary/application/worker.py` | После 200→429: failed, automatic_retry=false. После reopen DB и explicit retry: same_job=true, paid_first_batch_submissions=1, checkpoint_rows=4, total_requests=5, succeeded |
| R03 P1 | allow_unknown_price=true позволял неизвестной цене расходовать 0 из бюджета: 5 unknown requests разрешены при cap 1 ₽ | Необходима явная положительная per-request RUB reservation. Reservation учитывается в cap и остаётся отдельной от estimated/confirmed. `backend/secretary/infrastructure/database.py:reserve`, `backend/secretary/settings.py` | Нет разрешения без positive reservation; unknown reservations учитываются, следующий запрос сверх cap блокируется |
| R04 P1 | Summary v1 мог поставить ready после запуска transcript v2: version=2, status=ready, current summary=null, segments=0 | Атомарная проверка current transcript version перед summary commit; superseded queued summary отменяется, новая STT не пересекается с running summary. `backend/secretary/infrastructure/database.py:save_summary`, `backend/secretary/application/worker.py:enqueue_transcription` | Stale summary не публикуется и не помечает встречу ready |
| R05 P1 | Параллельные uploads использовали один temporary file. Windows repro: B получал успешный ответ, original содержал BBB + 61 A вместо трёх B | Atomic upload lease в SQLite и уникальный token.partial. `backend/secretary/infrastructure/database.py:reserve_upload`, `backend/secretary/api.py:upload` | Второй запрос 409, original содержит ровно 64 A; смешивания нет |
| R06 P1 | prepare_audio повторно использовал building/part-*.wav. Старый part-000001 добавлялся к текущему FFmpeg output: source 1000 ms → 2 chunks / 1100 ms | Отдельный building-UUID каждой попытки; chunk_seconds сохраняется в job payload. `backend/secretary/application/preparation.py`, `backend/secretary/application/worker.py:prepare` | Старый файл не включается, source 1000 ms → 1 chunk / 1000 ms |
| R07 P2 | Отмена completed in-flight STT оставляла meeting=transcribe при job=cancelled и сохранённом тексте | Сохраняются результат и receipt, затем терминальный aggregate status cancelled. `backend/secretary/application/worker.py:run_once`, `backend/secretary/infrastructure/database.py:cancel` | Job/meeting cancelled, готовый текст и audio retained |
| R08 P2 | Cancel running prepare + crash → queued/cancel_requested=1; claim никогда не выбирал job | Recover переводит отменённую local preparation в cancelled. `backend/secretary/infrastructure/database.py:recover` | Cancelled prepare не становится неполучаемой queued job |
| R09 P2 | Upload size проверялся после multipart spool: при cap 1 KiB parser успевал записать 2 MiB | ASGI receive/body cap до parser, включая chunked requests. `backend/secretary/api.py:BodyLimitMiddleware` | Content-Length oversized → 413 / parser bytes=0; chunked oversized → 413 / bounded spool |
| R10 P2 | После первой реализации body cap chunked oversized давал generic 400 вместо 413 | BodyTooLarge наследует HTTPException(413), сохраняет семантику FastAPI parser error. `backend/secretary/api.py:BodyTooLarge` | Оба способа upload возвращают 413 |
| R11 P2 | Поздняя capture failure оставляла recording=1; failed stop объявлял recorded | Capture state synchronisation и explicit failed handling. `backend/secretary/api.py:sync_recordings`, `backend/secretary/api.py:recording_stop` | FakeCapture failure → partial_error/recording=0 после sync и stop |
| R12 P2 | TXT глобально удалял '# ': C# и F# превращались в C/F, issue # 42 в issue 42 | TXT headings формируются отдельно; raw transcript content не очищается глобальными replace. `backend/secretary/application/exporting.py` | Hash terms, source IDs, исходный текст и отрицания сохраняются |
| R13 P2 | Async provider terminal failed нельзя было действительно повторить: explicit retry делал GET того же permanently failed ID. Calls POST,GET,GET → failed | provider_terminal marker отличает definitive failed от uncertainty; explicit retry может создать новую job, сохранив прошлый receipt. Routing/tariff snapshot защищает accepted job от смены текущей модели. `backend/secretary/application/worker.py` | POST,GET,POST,GET; новая job succeeds; оба receipts retained, 2 usage rows |

### Evidence index финального snapshot

| Файл / строки | Проверяемая граница |
|---|---|
| `backend/secretary/application/worker.py:60,137-143,180,193-194,212-213,240,261-305` | Route snapshot, durable checkpoints, terminal async failure, resume classification, isolated preparation settings, stage retry |
| `backend/secretary/infrastructure/database.py:36,150-178,194-239` | Checkpoints schema, atomic claim/cancel/recover, stale summary guard, partial capture warning, cost/upload reservations |
| `backend/secretary/infrastructure/polza.py:142-178,268-305,350-435` | Typed HTTP uncertainty, save/poll provider ID, exact payload hash, cache validation, per-call receipt/checkpoint |
| `backend/secretary/application/preparation.py:11-16,54-65` | Isolated attempt directory и bounded WAV outputs |
| `backend/secretary/api.py:27-61,158-191,230-263,287-334` | Body limit before multipart spool, startup-only recovery, Host/Origin+CSRF, upload lease, capture failure propagation |
| `backend/secretary/application/exporting.py:7-18` | Исходные segment IDs/timing и TXT-safe headings |
| `backend/secretary/settings.py:19-30,41` | Budget/unknown reservation bounds и editable non-secret configuration |
| `backend/secretary/infrastructure/audio.py:318,462-473` | Stream health независимо от queue.Empty; stop под Session.lock с сохранением terminal status |

Дополнительное независимое audio review другой ветви команды выявило две P2: inactive stream не проверялся при непрерывном backlog второго канала и stop мог перезаписать terminal status во время on_finish. В реализации добавлены проверки stream health на каждой iteration и status transition под Session.lock. Два synthetic regressions проверяют inactive system при продолжающемся mic PCM и concurrent stop при заблокированном on_finish. Они включены в финальный focused test run.

## Проверки и положительное evidence

**PASS, focused offline suite:** 100 passed, 1 warning, 6.89 s. Проверены 38 backend, 15 audio и 47 Polza cases, включая параметризованные сценарии. Единственное предупреждение — StarletteDeprecationWarning о связке TestClient/httpx; failures нет. Transient test outputs: `.runtime/review-final-tests`.

```powershell
$env:PYTHONPATH='backend'
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests\test_backend.py tests\test_polza.py tests\test_audio.py --basetemp .runtime\review-final-tests
```

Дополнительные собственные repro выполнялись в TemporaryDirectory и удаляли свои SQLite/WAV после завершения. Polza HTTP всегда заменялся httpx.MockTransport; API keys были явно synthetic. Настоящие облачные запросы, личное аудио и устройства не открывались.

| Требование | Проверенное evidence |
|---|---|
| Atomic claim / launch dedup | BEGIN IMMEDIATE + active-job unique index; независимый stress: 20 jobs / 32 concurrent attempts → 20 уникальных claims, queued=0 |
| Cap включает in-flight | Независимый stress: 8 concurrent reservations по 3 ₽ при cap 10 ₽ → 3 разрешены, total=9 ₽ |
| Schema export / recovery isolation | API factory/OpenAPI export не переводит существующие running jobs в uncertain; recovery выполняется при запуске рабочего lifespan, regression covers this side effect |
| Restart / uncertainty | Running cloud requests становятся uncertain, async ID сохраняется; sync ambiguous POST не повторяется автоматически; completed capture chunks восстанавливаются из файлов/journal |
| Partial results | Chunk segments + succeeded job commit атомарны; failed следующий chunk не уничтожает предыдущий; summary требует полного transcript |
| Schema / timing | Canonical Pydantic/OpenAPI entities; nullable timing/confidence, chunk precision явно отличается от segment; out-of-range time отвергается |
| Speaker provenance | Канал и speaker отличаются; speaker labels scoped chunk, не выдаются за established human identity |
| Grounded summary | JSON schema, existing source IDs, дословная evidence quote; owner/due_date только literal cited values либо null; raw transcript сохраняется отдельно |
| No fake production success | Missing key/invalid price → waiting_config/paused_budget без provider call; сетевые/JSON/grounding ошибки не превращаются в demo transcript |
| Bounded memory | PCM сохраняется постепенно, очереди capture bounded; импорт FFmpeg stream + bounded chunks; base64 file size проверяется до read_bytes |
| Local security | Host/Origin allowlist, CSRF для mutations, secret absent in public config/OpenAPI, запрещено редактировать key через API; official HTTPS Polza host only, redirects/proxy env disabled |
| Export | TXT/MD/JSON/DOCX работают на synthetic Russian text; JSON содержит версии/source IDs, partial export явно отделён от готового summary |

## Не подтверждено этой приёмкой

- Реальное качество русской STT, точность имён/дат/чисел/отрицаний и смысловая достоверность договорённостей. Структурная provenance проверка не доказывает semantic entailment.
- Настоящий Windows microphone + WASAPI loopback, длительная аппаратная запись, unplug/driver failure и реальная трёхчасовая сессия. Synthetic tests проверяют contract и resource bounds.
- Фактические Polza account access, billing/retention/limits, latency, Aiesa queue и RUB receipt на оплаченном запросе. Публичная metadata и MockTransport этого не доказывают.
- При unknown-price override cap ограничивает положительные owner-set reservations; они не являются доказанным верхним пределом фактического списания Polza. Неизвестная цена по умолчанию блокирует отправку.
- Automatic recovery ambiguous synchronous POST без provider receipt. Это безопасно остаётся uncertain и требует сверки; exactly-once внешнего API не обещается.
- Это backend review. Полная browser end-to-end UI, launcher/doctor lifecycle, benchmark quality и готовность всех документов должны проверяться отдельно общей acceptance процедуры.

## Snapshot исходников

| Файл | SHA256 |
|---|---|
| `backend/secretary/api.py` | `26EA17552A83AB816E1B678C4943BB9200E59BFA4CB96239B8F3EFE4F0967589` |
| `backend/secretary/application/worker.py` | `3502D0CE87E6B9DF899EEB3AA792D318F067D12A5C209100554235C5291A449E` |
| `backend/secretary/infrastructure/database.py` | `58B4A6BB6F8FD09F66B9B377D02599709B86787AAEEF62ABBC4DADA92DB07E80` |
| `backend/secretary/infrastructure/polza.py` | `F533527FB468BAC634745568B68FA0F0D16CCBAC820677AD67882EF3A426642C` |
| `backend/secretary/infrastructure/audio.py` | `1C27898E16F460F1F0E125CBABF87D9B2A43D94394F094D12E69F465A6B26D61` |
| `backend/secretary/application/preparation.py` | `D814562F4FD6E8D2C9A841E0A0EAFBCFC5B699699817290A6BDCCE055C5D063A` |
| `backend/secretary/application/exporting.py` | `5757A3EEB0E21B7C2AE14CA722B9F167E17B5886C3B473F62722FA1ECEE847DD` |
| `backend/secretary/settings.py` | `5675A23AF1C41BBC13DC2F52B4FCDC8E781AA34E647FED40CDE572D6D48C3DC0` |

Snapshot снят после focused test run. Позднейшие изменения исходников требуют отдельной оценки влияния; эта запись не выдаёт будущий код за проверенный.

## Дополнение общей приёмки

После этого snapshot root/backend проверили playback с открытым WAV и новыми чанками на Windows: воспроизведён HTTP 500 при replace; исправлен immutable cache, channel locking и bounded timeline-preserving copy. Три дополнительных regressions проходят. Последняя browser-проверка также выявила возврат к той же встрече после формы создания и continuation отменённой STT; они исправлены в UI и state handling backend. Повторные browser flows PASS; дополнительно 6 backend и 6 frontend regressions PASS. Общая Python suite — 135 PASS. Новые результаты общей приёмки и ограничения приводятся в [VALIDATION.md](VALIDATION.md); исходный snapshot выше сохранён как история независимого review.

