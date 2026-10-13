# T12R/R4: четыре source guard до runtime side effects

## Дополнение 2026-10-12, 03:15 МСК: operation-ID audit текущей admission-границы

Offline batch `.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests/test_maintenance_executor.py tests/test_team_paid_maintenance.py tests/test_team_secretary_maintenance.py tests/test_team_runtime.py -q --tb=short` завершился **110 passed / 0 failed / 1 прежнее Starlette/httpx deprecation warning / 24,43 с / exit 0**. Он подтверждает имеющийся lifecycle drain/no-replay и четыре Polza restore-authority veto.

Новый source trace уточнил gap: `MaintenanceTicket` хранит `operation_id`, но `assert_dispatch_admission()` не получает ожидаемый ID конкретной отправки и потому не сравнивает его с ticket. `TeamRuntime._loop()` открывает общие worker tickets через `admitted_operation(..., kind)` без operation ID; MAX уже знает ID в `BotSend`, а Vikunja — ID команды, однако final admission check его не проверяет. У Vikunja дополнительно разрешён kind `intake`. Следовательно текущий ticket обеспечивает drain/participant/run/kind, но пока не доказывает привязку внешнего side effect к той же operation.

Production код не менялся: частичное включение проверки без миграции каждого worker и side-effect seam могло бы либо пропустить несовпадающий ID, либо нарушить штатные пути. Следующая offline RED-карточка должна сначала доказать отказ при missing/mismatched operation ID и zero HTTP/native mutation, затем связать каждую owner claim с exact operation ticket и повторно проверить barrier-first/ticket-first. Это не заменяет `R4AdmissionBindingV1`: source-generation/decision digest, общий cross-process interlock и полный fixed-ledger evaluator по-прежнему отсутствуют. Activation/outbound OFF.

## Дополнение 2026-10-12, 03:09 МСК: Polza fail-closed по каждой authority

В `tests/test_team_paid_maintenance.py` добавлены четыре synthetic случая: restore-marker отдельно в `secretary`, `team`, `billing` или `vikunja` при полном четырёх-authority наборе блокирует платный Polza POST до `MockTransport` и до создания Billing charge. Новый parametrized набор — **4 passed**; связанный maintenance/runtime batch — **110 passed / 1 прежнее Starlette/httpx deprecation warning / 27,84 с**. Полный offline `tests audit/tests` после тестового изменения — **5 287 passed / 0 failed / 1 прежнее предупреждение / 1 481,01 с / exit 0**.

Production code не менялся. Это закрывает только тестовое покрытие существующего fail-closed пути; оно не закрывает `R4AdmissionBindingV1`, атомарность меж-source dispatch или R4 activation. Live Polza, credentials, public network и пользовательские данные не использовались; activation/outbound OFF.

## Дополнение 2026-10-12, 00:50 МСК: backup writer соблюдает root-only restore hold

RED воспроизвёл потерю внешнего restore marker: без guard row в БД, но с root-level `restore.json` в состоянии `restored_for_reconciliation`, backup создавал `complete` archive, не включая marker. Backup теперь проверяет все четыре authorities и derived metadata root read-only до lifecycle side effects, затем повторяет detector после `freeze()`/source write-lock до создания destination. Historical `restore.json` и `restore.partial.json` оба дают `deferred/backup_source_reconciliation_required`, marker не меняется, destination не создаётся; ошибка чтения fail-closed. RED — один ожидаемый fail; связанные `test_team_backup.py` + `test_secretary_restore_guards.py` — **76 passed / 0 failed / 1 существующее предупреждение / 34,00 с / exit 0**. Полный backend/audit suite **5 277 PASS** был до этого backup-writer изменения и не повторялся после него.

## Дополнение 2026-10-12, 00:47 МСК: `restore.partial.json` блокирует ранний crash recovery

Restore создаёт durable `restore.partial.json` раньше первой копии authority. RED-тест воспроизвёл crash до копирования: на target ещё нет DB guard rows и `restore.json`, а прежний `restore_guard_present()` сообщал `False`. Теперь вычисленный restore root проверяет partial marker до открытия БД; повреждённая/оборванная restore-папка блокирует startup. Регрессии: RED **1 failed** по ожидаемому отсутствию блокировки, затем GREEN **2 passed / 72 deselected**; полный backend/audit `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5 277 passed / 0 failed / 1 существующее предупреждение / 1 474,83 с / exit 0**.

Исправление закрывает один подтверждённый crash-window, не доказывает все crash points, все поколения fences или полное R5. Все проверки offline/synthetic; activation/outbound OFF, production stores и providers не использовались.

## Дополнение 2026-10-12, 00:08 МСК: исторический restore hold проверяется до копирования

R5 regression доказал, что структурно целый и заново корректно захешированный archive с историческим restore hold доходил до `_restore_guard()` лишь после создания destination и первой копии. Добавлена проверка source authorities на существующие restore markers во время `_validate_archive()`, до создания новой папки. Используется общий `restore_guard_present()`; ошибка чтения также блокирует импорт. Сценарии guard row, restore-specific trigger и `restore.json` теперь дают `backup_source_reconciliation_required`, не создавая target. Обычная пустая Billing guard table остаётся допустимой.

TDD RED→GREEN: до исправления отказал `backup_already_restore_blocked` после появления partial target; после исправления три parametrized regression прошли. Связанный backup/restore/context/runtime batch — **213 passed**, полный offline `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5 275 passed, 0 failed, 1 существующее Starlette/httpx deprecation warning, 1 582,99 с, exit 0**. Полный вывод отдельно не сохранялся.

Это закрывает только preflight slice R5: исторический активный hold нельзя тихо перенести в следующую restore-папку. Полная непрерывность generations/fences и crash recovery не доказаны. R4 остаётся IN_PROGRESS, R6 и owner/live gates открыты, activation/outbound OFF. Только offline/synthetic; без сети, credentials, production databases или платных запросов.

## Дополнение 2026-10-11, 23:24 МСК: read-only диагностика R3 context при заблокированном startup

Проверка показала, что `TeamRuntime.start()` обнаруживал restore marker и блокировал запуск, но не вызывал уже существующий R3 resolver. Из-за этого диагностика не различала подтверждённый фиксированный restore context и невозможность его проверить. Добавлен read-only вызов resolver только при наличии restore guard. Он сообщает фиксированный код `team_restore_context_verified_blocked` либо `team_restore_context_unavailable`; этот результат никогда не разрешает активацию. Runtime в обоих случаях остаётся blocked и выходит до control DB, Team, worker, provider и outbound side effects.

TDD: новый synthetic regression сначала упал из-за отсутствующего компонента `restore_context`, после исправления прошёл. Проверки: модуль context **29 passed**; связанный startup/recovery/restore/dispatch batch **181 passed**; полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` завершился **5 272 passed, 0 failed, 1 существующее Starlette/httpx deprecation warning, 1 422,37 с, exit 0**. Полный вывод остался в terminal session и отдельный лог-файл не сохранялся.

Уточнена семантика Billing guard: одно лишь существование пустой `maintenance_restore_guard` не означает restore, потому что обычный `BudgetRepository` создаёт эту таблицу при инициализации схемы. Регрессия `test_empty_billing_guard_table_without_restore_marker_is_not_blocked` фиксирует это поведение. Блокируют marker row и независимые остаточные restore markers; неизвестные/неполные состояния остаются fail-closed.

Проверка была offline/synthetic. Общий R4 proof/source-generation-bound predicate, native authenticated GET/advancing head, текущие owner/provider decisions и fresh execution/auth lineage всё ещё не подтверждены; R5/R6, ручная owner acceptance и production/live gates открыты. Activation/outbound OFF; публичные Go proxy и внешние сервисы не вызывались.

## Дополнение 2026-10-11, 22:29 МСК: новый hold во всех четырёх БД после restore

Проверка backup/restore обнаружила пропуск: restore_backup() устанавливал новый maintenance hold в Secretary, Team и Billing, но не в восстановленной БД Vikunja. RED-регрессия подтвердила отсутствие таблицы hold в Vikunja. Теперь после проверки каждого скопированного файла новый hold записывается во все четыре authority; он содержит новый restore id и commitment manifest.

Первый полный прогон после добавления четвёртого hold выявил зависимость: native-history catalogue считал точную таблицу hold в Vikunja неизвестной business schema. Каталог теперь исключает только полностью проверенный протокол guard metadata; любое неполное или изменённое представление fail-closed с native_restore_guard_invalid. Другие неизвестные native objects не игнорируются.

Проверки: backup tests — **39 passed**; два новых native-history regressions — **2 passed**; затронутый restore/source-preparation batch — **766 passed**; полный .venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short — **5270 passed, 0 failed, 1 существующее предупреждение Starlette/httpx, 1390,21 с, exit 0**. [Полный лог](../.runtime/team-rollout/r4-four-authority-restore-final-offline-20261011-190304.log), SHA-256 C7188BBDEC37E094D88AB1C5561FF8868B2003BF7936C923479BE15BBBA2DA66. Первый полный прогон до исправления native catalogue дал 26 failures и 50 errors; их причина устранена, повторный полный набор зелёный.

Это закрывает найденный пропуск нового hold, но не доказывает непрерывность исторических guards/fences между поколениями backup/restore; R5 остаётся частично реализованным. R4 также остаётся IN_PROGRESS: единый proof/source-generation-bound interlock, native authenticated GET/advancing head, актуальные owner/provider решения и fresh execution/auth lineage не подтверждены. Activation/outbound остаются OFF. Все проверки были offline/synthetic: реальные базы, MAX, Polza, Vikunja production, credentials и публичные Go proxy не использовались.

## Дополнение 2026-10-11, 21:17 МСК: fail-closed recheck у MAX/Vikunja dispatch

На последней границе owner-runtime HTTP mutations не было локальной проверки активного maintenance ticket и повторного чтения всех четырёх restore authorities. Это оставляло окно после startup/начала worker operation. RED воспроизвёл 9 случаев: отсутствие ticket для MAX/Vikunja и появление позднего restore marker в каждой из четырёх authorities перед POST.

Добавлен `MaintenanceRepository.assert_dispatch_admission()`: он использует только привязанные к maintenance run пути Secretary/Team/Billing/Vikunja, сверяет ambient ticket с active participant/run и разрешённым kind, затем read-only проверяет restore markers непосредственно перед внешней mutation. TeamRuntime передаёт эту зависимость в owner MAX и Vikunja clients. Отказ не отправляет HTTP; MAX возвращает `rejected`, Vikunja — fixed error code. Ошибка чтения или неполная authority set закрывают действие.

TDD: исходный тестовый срез дал **9 FAIL**; после исправления focused выборка `tests/test_team_runtime.py -k "owner_external_mutations_require or owner_max_dispatch or owner_vikunja_mutation"` — **9 passed**. Связанный backend-набор (`test_team_runtime`, Vikunja/MAX transport и identity, bot/notification workers, maintenance, backup/recovery) — **614 passed / 1 прежнее Starlette/httpx warning / exit 0**. Полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5267 passed / 0 failed / 1 существующее предупреждение / 1431,14 с / exit 0**; [полный лог](../.runtime/team-rollout/r4-late-dispatch-guard-offline-20261011-175248.log), SHA-256 `4f8a3817a7617937f0976647a88138a937a4d3d60a9ae8f4edba3f45aae8624e`.

Это узкая fail-closed защита dispatch seam, а не R4 activation permit: maintenance ticket всё ещё не связан с proof digest/source generation. Чтение четырёх SQLite и HTTP POST не атомарны; если source generation меняется после последнего read, от этого защищает только общий R4 interlock, который ещё не реализован. Общий permission predicate, native authenticated GET/advancing head, актуальные owner/provider решения и fresh execution/auth lineage остаются незакрытыми; activation/outbound OFF. Прогон был локальным synthetic/offline: публичные Go proxy, MAX, Polza, Vikunja production, credentials и пользовательские базы не использовались.

## Дополнение 2026-10-11, 20:40 МСК: проверка owner admission и R4 callers

Прослежены поддерживаемые side-effect entry points. `TeamRuntime.start()` проверяет четыре restore authorities до создания control DB, затем строит runtime внутри `job_startup` admission. Каждый штатный worker вызывается циклом `_loop()` через `admitted_operation()`, удерживая ticket до завершения операции/receipt; owner HTTP обёрнут в `AdmittedASGI`, Secretary routes — в `MaintenanceMiddleware`, а SQL connection methods открывают admission и повторно проверяют restore markers. MAX notification/bot dispatch выполняется внутри этих worker loops; `PublicationWorker` имеет отдельную admission. В кодовой базе не найден production caller, который запускает `run_once()` напрямую вне owner runtime; такие вызовы находятся в loop-ах и тестах. Прямой вызов worker API без owner composition остаётся неподдерживаемым обходным способом, но подтверждённого production пути через него нет.

Из этого аудита исправление runtime не выводится: штатная lifecycle admission уже удерживается на наблюдавшихся sinks. Но билет содержит только deployment/participant/run/kind/operation/fence и не связан с R4 proof digest/source generation; lifecycle gate не является permission predicate. Поэтому общий predicate, native authenticated GET/advancing head, актуальное owner/provider решение и fresh execution/auth lineage остаются обязательными блокерами; activation/outbound OFF.

Офлайн-проверка связанных restore context/ledger, SQL guards, recovery, TeamRuntime и paid dispatch:
`.venv\\Scripts\\python.exe -B scripts/run_offline_tests.py tests/test_restore_activation_context.py tests/test_restore_activation_decision.py tests/test_restore_activation_ledger.py tests/test_restore_fresh_evidence.py tests/test_restore_runtime_evidence.py tests/test_billing_restore_sql_opening.py tests/test_team_restore_sql_opening.py tests/test_team_runtime_recovery.py tests/test_team_runtime.py tests/test_team_paid_maintenance.py tests/test_team_secretary_maintenance.py -q --tb=short`
→ **334 passed, 0 failed, 1 прежнее Starlette/httpx deprecation warning, 434,29 с, exit 0**. Тесты использовали synthetic/offline authorities и mock transports; production-код в этой карточке не менялся.

## Дополнение 2026-10-11, 19:09 МСК: Polza проверяет четыре restore authority до dispatch

Новый offline regression выявил, что платная Polza-граница проверяла только Billing. Остаточный R4 marker в Secretary, Team или Vikunja мог появиться после первичной проверки, а запрос всё равно достигал transport. Шесть parametrized RED cases подтвердили пропуск на каждом из трёх non-Billing источников — как при preflight, так и после callback перед submit.

`assert_paid_billing_allowed()` теперь проверяет весь переданный набор read-only authorities. Основной Secretary передаёт Secretary + Billing; TeamRuntime — Secretary, Team, Billing и Vikunja. Polza повторно проверяет их после сохранения submission checkpoint и до `mark_submitted`; при появившемся marker запрос остаётся неотправленным, unsubmitted reservation освобождается, а checkpoint получает причину отказа. Все production пути используют тот же HTTP/DB слой, проверки выполнялись только с локальным `httpx.MockTransport`.

Affected offline suite через `scripts/run_offline_tests.py` — **286 passed, 0 failed, 1 прежнее Starlette/httpx deprecation warning, 27,89 с**. Полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5 258 passed, 0 failed, 1 прежнее предупреждение, 1 391,73 с, exit 0**; [полный лог](../.runtime/team-rollout/r4-shared-paid-guard-offline-20261011.log). Исходный RED, последующая targeted GREEN и полный прогон сохранены.

Это закрывает один fail-closed paid-dispatch seam, не весь R4. Общий transaction-bound activation predicate, native authenticated GET/advancing head, актуальное owner/provider решение и fresh execution/auth lineage отсутствуют; activation/outbound остаются OFF. Ни Polza, ни MAX, сеть, credentials или пользовательские базы в прогоне не использовались.

## Дополнение 2026-10-11, 18:11 МСК: полная регрессия paid preflight

После исправления `assert_paid_billing_allowed()` проверен полный `.venv\\Scripts\\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short`: pytest завершил все **5 251** тестов успешно, без failed tests, с одним существующим Starlette/httpx warning за **1 348,36 с**. Лог: [full offline run](../.runtime/team-rollout/full-offline-20261011-1605.log). Регрессия Polza была включена в полный набор; реальный Polza/credentials не использовались. Отдельный LRU defect, обнаруженный тем же прогоном, исправлен независимым tie-break на порядковом счётчике и покрыт regression-тестом.

Результат не закрывает R4: общий permission predicate, native authenticated GET/advancing head, fresh execution/auth lineage и актуальные owner/provider proofs всё ещё отсутствуют; activation и outbound остаются OFF. Владелец подтвердил продолжение только офлайн, поэтому Go module names/versions публичным сервисам не отправлялись.

## Дополнение 2026-10-11, 15:36 МСК: общий restore detector на платной границе Polza

Проверка финальной платной границы нашла расхождение: `assert_paid_billing_allowed()` смотрел только на таблицу `maintenance_restore_guard`, а runtime и нижние SQL guards распознают также неполные protocol markers и Team restore metadata. Добавлен RED→GREEN regression для `secretary_r4_native_prepared_hold` без основной guard-таблицы; preflight теперь вызывает общий read-only `restore_guard_present()` и возвращает `maintenance_restore_blocked` до dispatch. Ошибки чтения остаются fail-closed как `maintenance_billing_unavailable`; существующие runtime и бюджетные транзакции не ослаблены.

Проверка семи затронутых файлов (`test_budget_dispatch_boundary.py`, `test_secretary_restore_guards.py`, `test_monthly_budget.py`, `test_cloud_budget_boundary.py`, `test_cloud_budget_scopes.py`, `test_polza.py`, `test_provider_boundary_regression.py`): **230 passed, 0 failed, 1 прежнее предупреждение Starlette/httpx, 20,72 с, exit 0**. Новый тест до исправления упал из-за отсутствующего отказа, после исправления прошёл. Реальный Polza и credentials не использовались.

Это согласование одного restore preflight, не реализация общего activation predicate. Native authenticated GET/advancing head, fresh execution/auth lineage и текущие owner/provider approvals остаются обязательны; activation и outbound остаются OFF.

## Дополнение 2026-10-11 15:14 МСК: повторная affected-проверка runtime boundaries

Свежая выборка `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_restore_activation_decision.py tests/test_secretary_restore_guards.py tests/test_team_runtime.py tests/test_team_runtime_recovery.py tests/test_budget_dispatch_boundary.py tests/test_voice_final_dispatch.py -q --tb=short` завершилась: **211 passed, 0 failed, 252.26 с, exit 0**. Дополнительно `npm run test:processing` — **300 passed**, `git diff --check` — PASS (только обычные предупреждения Git о LF→CRLF). Эти тесты подтверждают сохранение текущего fail-closed режима в журнале R4, старте Team, восстановлении, бюджетном резерве и финальной отправке; они не создают activation grant и не проверяют живого Vikunja/провайдеров.

Статический аудит по-прежнему не даёт безопасно подключить единый R4 predicate: runtime configuration не содержит доверенного полного контекста backup/restore/lifecycle, а native authenticated GET/advancing head и fresh execution/auth lineage остаются фиксированными blockers. Поэтому кодовую активацию не добавлял, `activation_supported=false` и outbound OFF сохранены.

## Дополнение 2026-10-11 14:27 МСК: Team restore metadata и локальный restore

Полный прогон выявил regression после распознавания `restore.json`: локальный Secretary restore со `state=verified` ошибочно считался Team-копией, из-за чего проверка guard расходилась с `protect_restored_connection()`. Исправлено: Team metadata со `state=restored_for_reconciliation` блокирует старт и каждое отдельное подключение к authority, включая базу без SQL guard markers; локальный `verified` restore остаётся доступным. Metadata ищется в shared root всех четырёх источников и в двух фиксированных компонентах Team restore layout. Добавлены тесты на обе ветки и согласованность read-only connection.

Полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short`: **5 244 passed, 0 failed, 1 прежнее предупреждение Starlette/httpx, 1 575,02 с, exit 0** после семантического исправления. После последнего сужения путей поиска затронутый набор повторён: **186 passed, 0 failed, 71,39 с**; локальный backup/restore тест также прошёл отдельную выборку **36/36**. `git diff --check` — PASS с обычными LF→CRLF advisory warnings.

Это локальная защита восстановления, не завершение R4. Общая activation predicate, доверенная native lineage, authenticated native GET/advancing head и owner/provider approval остаются обязательными; activation/outbound не включались. Реальные базы, сеть, Polza и MAX не использовались.

## Дополнение 2026-10-11 12:53 МСК: неполные restore markers теперь блокируют runtime

В `maintenance_binding.restore_guard_present()` и `protect_restored_connection()` обнаружен пропуск: protocol-marker таблицы и остаточные `backup_restore_no_*` объекты не блокировали доступ, если базовой `maintenance_restore_guard` не было. Добавлен общий read-only запрос к `sqlite_master` для `backup_restore_no_*`, `restore_source_epoch_*`, `secretary_r4_native_*` и `secretary_native_activation_v1_*`; любой такой marker теперь fail-closed блокирует подключение/запуск даже без основной guard-таблицы. Обычный Billing case с пустой guard-таблицей и только `billing_restore_*` не изменён.

Пять targeted cases прошли подтверждённый RED→GREEN цикл; отдельный тест residual trigger добавлен затем. Смежный restore/runtime набор, включая этот тест: **494 passed**. Полный запуск `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short`: **5 241 passed, 0 failed, 1 существующее Starlette/httpx deprecation warning, 1 564,70 с, exit 0**; `git diff --check` перед обновлением журнала — PASS (только advisory LF→CRLF).

Это узкий fail-closed guard fix, не завершение R4: общая activation predicate, доверенное восстановление native lineage и необходимые authenticated-read gates по-прежнему отсутствуют; activation/outbound остаются OFF. Сеть, Polza, MAX и пользовательские базы не использовались.

Дата: 2026-10-06. Статус: `COMPLETE_OFFLINE_SLICE`; общий R4 остаётся `IN_PROGRESS`, activation/outbound остаются выключены.

## Что обнаружено

Оба составных runtime entry point проверяли guard в Secretary, Team и Billing, но пропускали Vikunja. `TeamRuntime.start()` создавал/bind-ил maintenance control DB до проверки restore guard. При неполном или разошедшемся restore-наборе один guard в Vikunja мог остаться незамеченным; новый control store и последующий gateway/provider setup могли быть начаты.

## Исправление

- Добавлен общий `restored_runtime_guard_present(settings)`: учитывает явный restore-block флаг и guard во всех четырёх источниках (`secretary`, `team`, `billing`, `vikunja`). Ошибка чтения, отсутствующее поле или некорректный путь ведут к отказу.
- TeamRuntime выполняет этот preflight до создания maintenance DB, регистрации участника и построения клиентов. При блокировке он выдаёт локальный degraded health status, не создаёт control DB, не принимает запросы и не запускает worker-ы. Проверка после binding сохранена как повторная защита от drift.
- Owner-side Secretary runtime использует тот же preflight до control DB и создания локальных компонентов.

## Проверка

Сначала два новых теста воспроизвели дефект: TeamRuntime продолжил startup при Vikunja-only guard и создал control DB; owner-side runtime дошёл до создания control store вместо отказа. После исправления:

```powershell
.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_team_runtime.py tests/test_secretary_restore_guards.py tests/test_team_lifecycle.py tests/test_budget_dispatch_boundary.py tests/test_voice_final_dispatch.py -q --tb=short
```

Результат первоначальной affected выборки: **180 passed, 26.37 s, exit 0**. После полного прогона был расширен gateway probe fixture новым обязательным `vikunja_database_path`. Финальная объединённая выборка:

```powershell
.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_restore_guard_probe_custody.py tests/test_team_runtime.py tests/test_secretary_restore_guards.py tests/test_team_lifecycle.py tests/test_budget_dispatch_boundary.py tests/test_voice_final_dispatch.py -q --tb=short
```

Итог: **190 passed, 42.59 s, exit 0**. Включены startup/lifecycle, restore custody probes, legacy restore, budget dispatch и voice final-dispatch проверки. Все тесты используют изолированные synthetic SQLite/HTTP fixtures; реальные базы, Polza, MAX, Vikunja-сервис и учётные данные не использовались.

Полный backend/audit запуск сначала выявил два дефекта только в старых тестовых `SimpleNamespace` fixtures в `test_restore_guard_probe_custody.py`: в них отсутствовал четвёртый path `vikunja_database_path`, из-за чего новый helper fail-closed блокировал чтение до SQLite. Тестовая synthetic settings fixture дополнена этим путём; затронутая объединённая выборка прошла (**190 passed**). После исправления полный запуск был повторён: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` → **5171 passed / 1 warning / 1948.51 s (32:28) / exit 0**. Единственное предупреждение — устаревающая связка `httpx` с `starlette.testclient`. Это полное offline backend/audit подтверждение; реальные базы, MAX, Polza, сервис Vikunja и credentials не использовались.

## Граница результата

Это закрывает только полный перечень источников в двух startup preflight точках. Это не общий activation permit и не завершение R4: финальное owner/provider решение, native authenticated GET с advancing head, fresh execution/auth lineage и propagation predicate через SQL/recovery/admission/claim/reserve/final dispatch остаются обязательными. R5, R6, T9 owner manual acceptance, live MAX/Polza/phone и 24-hour pilot также открыты.

## R4 cross-boundary source audit — 2026-10-06

Read-only поиск `rg -n "collect_prepared_activation_context|current_activation_consent_commitment|record_blocked_activation_decision|shared_permission_predicate_missing|native_authenticated_get_missing" backend scripts` подтвердил границу интеграции: prepared-context collector вызывается только из `restore_activation_staging` и `restore_source_epoch_writer`; экспортируемые инфраструктурные consent/blocked-decision функции пока не имеют runtime caller в `backend` или `scripts`. В decision ledger обязательные blockers включают `shared_permission_predicate_missing`, `native_authenticated_get_missing` и `fresh_execution_lineage_missing`; сохранённое состояние остаётся `activation_blocked`, `activation_supported=false`, `outbound_enabled=false`.

Следующий R4 срез должен подключать эти данные к единому authority, а не трактовать отдельный source-preparation result, новую control DB или config Boolean как permit. Пока нет квалифицированного native authenticated GET/advancing head и общих проверок lineage, production runtime остаётся fail-closed. Проверка была статической; native Vikunja, реальные аккаунты/providers и owner stores не читались.

## Дополнение 2026-10-11: остаточные restore markers при неполной строке guard

Новая регрессионная проверка воспроизвела fail-open: если в SQLite сохранилась таблица `maintenance_restore_guard` и один из restore-specific triggers `backup_restore_no_*`, но singleton-строка отсутствует, startup preflight и per-connection guard раньше считали источник обычным. Исправление трактует такую комбинацию как заблокированный restored source и сохраняет SQL read-only. Пустая базовая Billing guard-таблица с обычными `billing_restore_*` triggers остаётся штатной и не блокирует runtime.

Первая RED-проверка дала 2 FAIL; первоначальное правило «любая таблица означает restore» было отвергнуто, так как штатный Billing runtime тоже содержит пустую guard-таблицу. После уточнения по restore-specific trigger проверен affected-набор: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_secretary_restore_guards.py tests/test_billing_restore_sql_opening.py tests/test_team_restore_sql_opening.py tests/test_team_runtime.py tests/test_team_runtime_recovery.py tests/test_restore_guard_probe_custody.py tests/test_team_secretary_maintenance.py tests/test_team_paid_maintenance.py -q --tb=short` → **218 passed, 1 existing Starlette/httpx deprecation warning, 75.06 s, exit 0**. Проверены startup/API, четыре SQLite boundary families, recovery, billing и owner-side maintenance; используются только synthetic DB/HTTP fixtures.

Первый полный backend/audit прогон завершился **5234 passed / 1 failed / 1 warning / 1554.70 s**: единственный сбой был time-sensitive тест `test_approvals_are_append_only_same_operator_and_fresh` с целым сроком `int(time.time())+301`, который из-за округления иногда оказывался внутри допустимых 300 секунд. Изолированный тест прошёл; тестовые будущие сроки `+301` заменены на явный запас `+600` в двух однотипных проверках, production logic сроков не менялась. Связанные модули прошли **163 passed / 20.67 s**. Повторный полный backend/audit suite `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` завершился: **5235 passed, 0 failed / 1 existing Starlette/httpx deprecation warning / 1542.99 s / exit 0**.

Это узкая целостность локального marker-а, не единый R4 permission predicate. Если и guard row, и все restore-specific triggers удалены или заменены, одного SQL marker-а недостаточно; exact source identity, shared decision, native authenticated GET/advancing head и fresh-work lineage остаются обязательными.

## Актуальный runtime seam audit — 2026-10-11

Повторный read-only аудит текущего checkout подтверждает, что общий permission predicate ещё не подключён:

- Поиск runtime callers для `current_activation_consent_commitment`, `record_blocked_activation_decision` и `RuntimePermissionPredicate` в `backend`/`scripts` нашёл только объявления двух consent/decision функций и blocker `shared_permission_predicate_missing`. Эти функции пока не являются runtime authority.
- `TeamRuntimeSettings` содержит только `project_dir`, пути к пяти БД и поля `outbound_enabled`/`restore_blocked`; путей к `backup_dir`, `restore_dir` и `lifecycle_path` в runtime config нет. `TeamRuntime.start()` проверяет четырёх-source restore markers, затем использует `settings.outbound_allowed`. Это fail-closed restore guard, но не чтение последнего owner decision.
- `prepared_restore_evidence()` и `_current_context()` требуют проект, backup, restore, maintenance, lifecycle и config пути. Добавленный в этом checkpoint fixed resolver выводит R3 paths из settings и inventory, но пока не является runtime caller и не разрешает activation.
- Decision ledger допускает только `activation_blocked`; он всегда сообщает `activation_supported=false` и `outbound_enabled=false`. Фиксированные blockers `native_authenticated_get_missing` и `fresh_execution_lineage_missing` также не закрыты.

Следующий кодовый шаг не должен добавлять config Boolean, принимать caller-selected evidence paths или считать существующий source-preparation результат разрешением. Fixed R3 context resolver добавлен и проверен отдельно; следующий шаг — определить общую predicate authority и безопасный способ её передачи/recheck в startup/native launch, SQL/recovery/admission, billing reservation и финальную dispatch транзакцию. Resolver пока не привязан к этим runtime callers. До готовности current owner/provider decision, native authenticated GET с advancing head и свежей execution/auth lineage любые новые пути остаются заблокированными. Первоначальный seam audit был статическим; точные focused tests нового resolver приведены в checkpoint выше.

## Checkpoint 2026-10-11 — fixed R3 runtime-context resolver

`restore_runtime_context.resolve_runtime_restore_context()` теперь связывает exact `TeamRuntimeSettings`, загруженный штатным loader, с одной восстановленной копией: target выводится из Team DB пути, все четыре настроенных source paths сверяются с `restore.json`, а archive и исходные runtime paths берутся из фиксированного `.runtime/team-operator/restores/<restore_id>/inventory.json`. Inventory служит только подсказкой путей; существующий R3 collector повторно проверяет его фактическое содержимое и source receipts. Configuration удерживается от записи на всё время разрешения. Resolver не принимает caller-provided paths/evidence/boolean grant, ничего не создаёт и возвращает только `activation_supported=false`, `outbound_enabled=false`.

Финальная focused проверка: `tests/test_restore_activation_context.py` — **27 passed / 36.54 s / exit 0**; `tests/test_team_runtime.py` — **50 passed / 7.10 s / exit 0**. Затем offline runner для шести связанных config/startup/native/lifecycle/maintenance модулей завершился **198 passed / 1 upstream Starlette/httpx deprecation warning / 84.25 s / exit 0**. `git diff --check` — **exit 0**. Evidence: `.runtime/team-rollout/t12r-runtime-context-20261011.txt`, `.runtime/team-rollout/t12r-runtime-settings-20261011.txt` и `.runtime/team-rollout/t12r-runtime-context-callers-20261011.txt`. RED до появления resolver-а и scope-order regression сохранены в истории тестового выполнения; ручная browser acceptance, native authenticated GET/advancing head, единый R4 predicate, current owner/provider approval, fresh execution lineage и activation остаются незакрытыми. Внешние API и credentials не использовались.

## Checkpoint 2026-10-11 — shared predicate contract/source map (design only)

Текущий source-to-sink map и контракт без разрешения активации добавлены в [`team-restore-reconciliation.md`](contracts/team-restore-reconciliation.md). Проверенные seams: standalone Secretary startup проверяет две DB markers, owner wrapper и TeamRuntime — все четыре; каждый SQL repository повторно удерживает local restore guard; Team recovery/claims используют свои source/operation fences; Polza проверяет authorities до резервирования и до dispatch; публикация, уведомления и Team workers пока не используют общий R4 predicate; native lane остаётся диагностическим и не подтверждает authenticated GET/head. `current_activation_consent_commitment()` и `record_blocked_activation_decision()` не имеют runtime callers; ledger допускает только `activation_blocked`.

Это design checkpoint, не production-code change и не qualification. Contract запрещает использовать caller booleans/path hints как grant, требует operation-bound recheck, запрещает частичный cross-file переход guards и оставляет `activation_supported=false`/`outbound_enabled=false` при любом missing/stale evidence. Конкретный cross-process lease/transaction primitive остаётся невыбранным; до его доказательства никакой permit не подключать. Внешние сервисы и native process не запускались.

### Existing maintenance barrier review

`MaintenanceRepository.enter()` writes an active ticket under `BEGIN IMMEDIATE` with the current fence; `begin_barrier()` changes mode to `draining` and increments the fence; subsequent `enter()` calls reject non-`open`; `freeze()` refuses while active tickets remain. This is a viable drain primitive for scoped runtime work. It is not yet an R4 permission primitive: `MaintenanceTicket` has deployment/participant/run/kind/operation/fence but no restore epoch or proof digest; the control DB stores four configured paths, not their current identities or an activation decision; `api.create_app()` remains callable without the owner wrapper/maintenance repository. In the owner `TeamRuntime`, every scheduled worker is wrapped by `_loop()`/`admitted_operation()` through its result/receipt; `PublicationWorker` also holds a nested admission. Direct use of worker classes outside that owner composition does not acquire the runtime-level ticket. No current caller invokes `RuntimePermissionPredicate` or the blocked-decision writer.

Proposed versioned binding is specified in [`team-restore-reconciliation.md`](contracts/team-restore-reconciliation.md#предложение-operation-bound-admission-ticket-v1). It is an offline design only. Next implementation gate: synthetic RED tests for missing/v0, stale, mismatched, replayed and partial four-source evidence plus `begin_barrier` interleavings; consumers must remain blocked until the complete fixed-ledger evaluator and transaction/dispatch interlock exist. A partial per-file commit or unbound maintenance ticket must not enable any source. No schema or runtime change has been made.

Focused offline verification of the existing barrier/startup/paid paths: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_maintenance_executor.py tests/test_team_paid_maintenance.py tests/test_team_secretary_maintenance.py tests/test_team_runtime.py -q --tb=short` → **97 passed / 1 existing Starlette/httpx deprecation warning / 24.75 s / exit 0**. This verifies the current maintenance fences and their synthetic interleavings; it does not verify the missing R4 predicate or activation.
