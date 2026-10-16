# Secretary + Vikunja Free + MAX Implementation Plan — Sol 6.1

## Checkpoint 2026-10-14 00:10 МСК: безопасная блокировка второго локального экземпляра

Владелец повторно подтвердил видимость карточек после входа и выбора «Проект 7»; этот manual PASS ограничен списком. На основном локальном интерфейсе обнаружен orphan run: `.runtime/processes.json` ссылался на завершившийся PID/port `63800`/`8765`, а offline `run_server.py` PID `9384` слушал `8767`. `start.ps1` теперь блокирует запуск второго экземпляра этого checkout до port check/bootstrap/`Start-Process` и ничего не останавливает/не присваивает. `doctor.ps1` показывает неподтверждённый listener по PID/порту/run ID и не запрашивает его health или storage probe. Живая проверка: повторный запуск отказал, `8765` свободен, `8767`/PID `9384` и launch record не изменились; `/health=ok`, `/ready=local_ok`, `production_qualified=false`.

Проверки: launcher focused tests **9/9**, скрипты проходят parser в Windows PowerShell 5.1 и PowerShell 7, `git diff --check` PASS. Рабочий экземпляр оставлен доступным владельцу. Offline-only; ручной прогон кнопок/action receipt, live providers, TLS, phone, 24/7 и production qualification отдельно не подтверждены.

## Checkpoint 2026-10-14 00:20 МСК: Team UI state transitions и Python API contracts

Повторный `npm run test:processing`: **302 passed**, `npm run typecheck` и `npm run lint` — exit 0. `tests/test_team*.py`: **1 269 passed**; MAX/Vikunja/task publication/job contract modules: **552 passed**; в каждом Python запуске одно прежнее Starlette/httpx deprecation warning. Проверены synthetic API contracts, operation-ID/receipt transitions, conflicts, UI state races и task workflow. Ни live-сервисов, ни credentials, ни внешнего трафика. Это не закрывает ручной прогон самих кнопок и сохранения или production gates.

## Checkpoint 2026-10-13 23:24 МСК: полный offline прогон и новый ручной handoff

Полный `.venv\\Scripts\\python.exe -B scripts\\run_offline_tests.py tests audit\\tests -q --tb=short` после стабилизации Windows process probe: **5 428 passed / 0 failed / 1 существующее предупреждение Starlette/httpx / 1 654,29 с / exit 0**. Лог: `.runtime/team-rollout/full-offline-goal-after-timeout-20261013-225650.log`. Предыдущий полный прогон дал 5 427 passed и одно `WaitForSingleObject(..., 5000)` timeout в `test_native_inputs_cwd_is_accepted_by_real_windows_process_creation`; затронутый модуль изолированно прошёл 6/6, тот же кейс — 3/3 последовательных повторов. В тесте увеличен только максимальный срок завершения простого дочернего CWD probe до 15 секунд; assertions на успешный exit, путь и ограниченный вывод сохранены. После изменения модуль прошёл 6/6, затем весь offline-набор прошёл. Production-код не менялся.

Основной ручной Secretary остаётся отдельным disposable `--offline --isolated-offline` процессом на `127.0.0.1:8767`: `/health=ok`, `/ready=local_ok`, `production_qualified=false`, `cloud_enabled=false`. Новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-6b8675c8a0a1426e80a7f5c23d12d1bc`, PID `16368`, порт `58046`, максимум 30 минут. Fixture/UI страницы — HTTP 200; анонимный `/api/team/v1/me` — 401. Вкладки открыты в браузере. Пользователь ранее подтвердил только видимость карточек; ручной mock-action preview/receipt ожидает проверки на этом run. Внешние MAX/Polza/Vikunja и реальные задачи не вызывались.

Общий план всё ещё IN_PROGRESS. Activation/outbound OFF; native trust/current-head producer, R4–R6 и live/owner/24-hour gates не закрыты. Продолжаем только офлайн до отдельного решения владельца.

## Checkpoint 2026-10-13 14:11 МСК: свежий synthetic UI handoff

Срок предыдущей Team fixture завершился. Поднят новый loopback-only `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-d05d96fada3641bab022baab99ebecc2`, PID `38652`, `localhost:50623`, TTL до примерно 14:41 МСК. `/fixture`, `/fixture/status`, `/team/`, `/team/team.html` → 200; анонимный `/api/team/v1/me` → ожидаемый 401. Обе ручные ссылки поставлены в очередь Codex; в UI используются synthetic участники/задачи и mock Vikunja. Пользовательский PASS остаётся только для видимости карточек; task actions и live/production интеграции не квалифицированы.

## Checkpoint 2026-10-13 13:57 МСК: Team API/button verification and offline local app

По просьбе владельца продолжены офлайн проверки. Все 38 `tests/test_team*.py` плюс API/OpenAPI/Vikunja/MAX fixture modules (`test_vikunja_adapter.py`, `test_max_contract_fixture.py`, `test_openapi_export.py`, `test_task_delivery_contracts.py`, `test_job_contract.py`) завершились **1 482 PASS / 0 FAIL / 1 существующее Starlette/httpx warning / 517,24 с / exit 0**. Frontend: **302 processing PASS**, **1 release-manifest PASS**, lint и TypeScript PASS; production Vite build изолирован в `.runtime/team-ui-verification-2026-10-13-1345` (**1 777 modules**), `git diff --check` PASS.

Основной Secretary поднят на `127.0.0.1:8765` через `scripts/start.ps1 -Port 8765 -NoBrowser -NoBootstrap -Offline`; `/health=ok`, `/ready=local_ok`, `cloud_enabled=false`, `production_qualified=false`. Изолированный disposable import UI `8767` доступен. Владелец вручную подтвердил видимость карточек на Team fixture; это не подтверждает кнопки, реальные Vikunja/MAX/Polza, HTTPS/TLS, 24/7 или production. Все источники и тесты в этой карточке синтетические/offline, внешние запросы не выполнялись.

Общий план остаётся IN_PROGRESS: activation/outbound OFF, R4–R6 и owner/live gates открыты. В следующем offline шаге нельзя подменять native DB head диагностическим `native_events.seq`; native current-head producer, общий activation predicate и доверенная continuity lineage не доказаны.

## Checkpoint 2026-10-13 13:04 МСК: fixed blocked-activation evaluator и текущий ручной handoff

Текущий blocked evaluator `evaluate_fixed_blocked_activation(settings)` закреплён в offline R4: шесть затронутых restore/R4 modules — **169 PASS / 0 FAIL / 325.33 s / exit 0**; отсутствие/изменение/неbinding ledgers и cross-process readback проверяются без side effects. Он не выдаёт source generation, permit или activation. Следующий R4 шаг не закрывается автоматически: подготовленный native history сохраняет genesis sequence 0, observation journal имеет собственный diagnostic sequence, а non-test `SourceGenerationV1` producer отсутствует. Последовательности не смешивать; определить отдельный typed current native-read evidence contract прежде чем подключать consumer.

Для ручных проверок запущен отдельный `--offline --isolated-offline` import UI на `127.0.0.1:8767`, с новой disposable БД, пустым ключом и `cloud_enabled=false`; `/`, `/health`, `/ready` и `/api/v1/config` отвечают 200, `production_qualified=false`. Синтетический WAV импортируется только по ручному выбору файла. Свежий `HTTP_SYNTHETIC_UI_ONLY` Team run — `localhost:51515`; предыдущий порт `56660` уже не слушает, а IP-алиас `127.0.0.1:51515` отвергается host allowlist-ом. Ссылки и шаги в `docs/TEAM_TESTING_CURRENT.md`; никакого owner PASS не заявляется.

## Checkpoint 2026-10-12 12:29 МСК: main app restarted offline

The previous process was no longer listening on port8765. The owner-safe command `scripts/start.ps1 -Port 8765 -NoBrowser -NoBootstrap -Offline` started it successfully; fresh probes returned `/` 200, `/health=ok`, `/ready=local_ok`, `production_qualified=false`, `cloud_enabled=false`, and `local_cost_limits_enabled=false`. No config edit or external request was made. The isolated, tested frontend build was installed into active `frontend/dist` after backing up the previous static files to `.runtime/team-rollout/frontend-dist-backup-before-price-copy-20261012-0931`; the live JS asset contains the new price explanation and doctor reports release parity healthy. Main page and the verified HTTP synthetic fixture/Team UI are queued in Codex.

## Checkpoint 2026-10-12 12:20 МСК: offline price-rejection diagnosis and refreshed HTTP fixture

Read-only local aggregation found **68 failed STT jobs, all HTTP 400**: 34 retain allowlisted `BAD_REQUEST`, 34 retain no safe provider code; none retain an allowlisted parameter or machine reason. Meeting identities, raw error text, transcripts and secrets were not emitted. The user-provided screenshot shows a Polza `provider.max_price` route rejection: remaining monthly account quota is independent from a per-unit route ceiling. The cached public catalog is dated 2026-10-01 and is not an account-route receipt, so it cannot establish the current route price. The live Secretary remains `cloud_enabled=false` and `local_cost_limits_enabled=false`; stored STT/meeting cap values are inactive in this current runtime. No provider request or config change was made.

Clarified the frontend error/settings text and added regressions to explain the three separate controls: per-request ceiling, Polza monthly quota and per-meeting budget. Fresh checks: frontend **301 passed**, typecheck/lint/build exit 0 (**1,777 modules**, isolated build); targeted offline Polza failure/configuration tests **106 passed / 1 existing warning / 15.38s**. A stale `ready` manifest was traced to the short-lived command shell killing its child server; new `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-26f511629d794b8eafd72b195a0d6e1b` is held by a live session and verified `ready` at `localhost:59200` until **12:48:15 MSK**. Browser pages are queued; manual owner actions remain pending.

## Checkpoint 2026-10-12 08:50 МСК: штатная offline Windows диагностика

`doctor.ps1` подтвердил принадлежащий проекту PID, offline launch, health/readiness и storage write probe; `start.ps1 -Offline -NoBrowser -NoBootstrap` безопасно распознал уже работающий экземпляр. `production_qualified=false`, cloud выключен, capture/Team runtime/live providers не qualified. `stop.ps1` не запускался во избежание остановки доступного пользователю локального приложения. Doctor сообщил только агрегат jobs (68 failed, 112 succeeded, 1 cancelled, 1 waiting_config, 0 stale); причины отказов не исследовались. Не использовались устройства или сеть.

## Checkpoint 2026-10-12 08:43 МСК: fallback OpenAPI response приведён к фактическому 503

Live API trace нашёл, что fallback `GET /` возвращал 503 при отсутствии frontend bundle, но export schema описывала 200. Исправлены статус, описание и response model; canonical `docs/openapi.json` regenerated, `frontend/src/generated/api.d.ts` синхронизирован. Добавлен runtime TestClient assert точного HTTP 503/body с loopback Host. `test_openapi_export.py` + `test_backend.py`: **64 PASS / 1 существующее предупреждение / 31,38 с / exit 0**; typecheck, lint и отдельный production build (1 777 modules) — exit 0. Live и canonical API paths совпадают, кроме ожидаемого schema-only fallback `/`. Полный backend/audit suite 5 380 PASS был до локального изменения; его не повторяли. Production/manual gates остаются открытыми.

## Checkpoint 2026-10-12 08:29 МСК: frontend проверен; UI automation имеет host blocker

Свежий frontend gate завершён: processing **300/300**, release manifest **1/1**, typecheck/lint/build — exit 0, собраны **1 777 modules** в изолированный `.runtime` путь. HTTP synthetic fixture проверена по entrypoints и JS/CSS assets (все 200) и оставлена владельцу до **08:57:30 МСК** для локального smoke. BrowserSkill automation не смогла открыть Chrome session из-за отказа extension `Creating browser windows from extensions is not supported in Owl`; обход другим backend не выполнялся. Это не браузерный/manual PASS и не production qualification.

## Checkpoint 2026-10-12 08:22 МСК: полный offline набор после blocked fence candidate

После первичного падения сквозных source-epoch tests найдена причина: immutable `SourcePreparationControl` проверял exact-набор context keys до записи. Схема дополнена candidate SHA и expected successor fence с закрытой проверкой типа и диапазона; негативные тесты покрывают malformed hash, bool и overflow. Семь связанных offline restore/R4 модулей прошли **334/334 за 260,26 с**. Полный backend/audit запуск после этой правки прошёл **5 380 PASS / 0 FAIL / 1 прежнее предупреждение Starlette/httpx / 1 525,75 с / exit 0**. Кандидат остаётся `prepared_blocked`; fence transfer, runtime admission и снятие guards не реализованы. Все проверки офлайн, без proxy egress/live services/credentials/owner data.

## Checkpoint 2026-10-12: blocked startup regression and successor-fence candidate

TeamRuntime now checks an existing maintenance control mode before binding restored sources or registering a participant; `restore_blocked` remains blocked without gateway/provider construction. `SuccessorFenceCandidateV1` derives the proposed next fence only from captured R5 lineage and rejects missing/stale/conflicting candidate records. Its JSON round-trip is deterministic but does not prove durable SQLite/power-loss recovery or transfer the fence. Targeted runtime/recovery/context suite: **144 PASS**; candidate DTO suite: **51 PASS**. Full offline `tests audit/tests`: **5 373 PASS / 0 FAIL / 1 existing Starlette/httpx deprecation warning / 1 591.89s / exit 0**. R5 remains `PARTIAL_OFFLINE`, R4/R6 and manual/live gates remain open. No public module proxy, MAX, Polza, Vikunja, credentials, owner data or deployment was used.

## Checkpoint 2026-10-12 06:10 МСК: source-generation commitments; R5 high-water remains open

Добавлены pure versioned `SourceGenerationV1`/`R4AdmissionBindingV1` DTO с opaque commitments и проверкой scope; они не имеют runtime consumer и явно не дают activation/outbound разрешения. `.venv/Scripts/python.exe -B scripts/run_offline_tests.py tests/test_team_backup.py tests/test_restore_admission_binding.py -q --tb=short` — **82 PASS / 1 прежнее warning / 38,77 с / exit 0**. Свежая команда `.venv/Scripts/python.exe -B scripts/run_offline_tests.py tests/test_team_backup.py tests/test_restore_source_epoch.py tests/test_restore_source_epoch_writer.py -q --tb=short` завершилась **190 PASS / 0 FAIL / 1 прежнее Starlette/httpx warning / 168,35 с / exit 0**.

Кодовая трассировка R5: backup manifest записывает barrier fence, restore metadata — только hash manifest, новая `MaintenanceRepository` при создании начинает с fence `0`; максимальный fence, достигнутый в старой generation после архива, не имеет доказанной lineage. R4 остаётся IN_PROGRESS, R5/R6 PARTIAL_OFFLINE, activation/outbound OFF. Не принимать caller-supplied high-water и не ослаблять guards. Codex Security diff scan `9b49b66d-e36f-44bf-953b-c5cbe4e3439e`: 0 подтверждённых кандидатов в 24 app-source rows; статический частичный review, не production qualification.

Следующая offline карточка: установить доверенный источник и правила преемственности maintenance fence, затем проверить missing/stale/conflicting history на synthetic fail-closed отказ. По решению владельца никакие из 319 module names/versions не отправлять public proxy/sumdb; провайдеры и production data не использовались.

## Checkpoint 2026-10-12 03:09 МСК: Polza restore-source regression и full offline pass

Добавлены четыре synthetic проверки: поздний restore-marker в каждой из `secretary/team/billing/vikunja` блокирует paid Polza POST до mock transport и Billing charge. Связанный maintenance/runtime batch — **110 PASS**; полный offline backend/audit suite после добавления тестов — **5 287 PASS / 0 FAIL / 1 прежнее Starlette/httpx warning / 1 481,01 с / exit 0**. Production code не менялся; R4 proof binding/interlock и activation не закрыты.

Предыдущая UI fixture истекла; новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-e228495d21f342bc8fbbb58963fe2dd9` доступен до **03:39:49 МСК** на `localhost:61633`; `/fixture/status`, `/fixture`, `/team/` → HTTP 200. Ручная инструкция и точная stop-команда: [TEAM_TESTING_CURRENT.md](../../TEAM_TESTING_CURRENT.md). Код не использовался, owner acceptance и live MAX/Polza/Vikunja/phone/24h остаются открытыми. Activation/outbound OFF.

## Checkpoint 2026-10-12 02:39 МСК: refreshed local manual-test handoff

Предыдущая disposable HTTP fixture завершилась по TTL; проверка не обнаружила её активный serve-процесс. Поднят новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-7bb0d358c90646c49bc47923d068a476` на `localhost:51093`, TTL до 03:09:10 МСК. `/fixture/status`, `/fixture`, `/team/` → HTTP 200; ссылки поставлены в очередь открытия панели Codex. Инструкция, код и точная stop-команда записаны в [TEAM_TESTING_CURRENT.md](../../TEAM_TESTING_CURRENT.md). Пользовательский код не читался/не пересылался; авторизованная ручная приёмка, owner receipt, MAX/Polza/live Vikunja, phone и 24h остаются незавершёнными. Activation/outbound OFF.

## Checkpoint 2026-10-12 01:33 МСК: полный offline suite после backup-writer изменений

Полная команда `tests audit/tests` после последнего production patch прошла: **5 283 passed / 0 failed / 1 существующее Starlette/httpx предупреждение / 1 510 с / exit 0**. Новый HTTP synthetic handoff доступен до 02:03:09 МСК: [fixture](http://localhost:57887/fixture), [Team UI](http://localhost:57887/team/). R5 остаётся partial; ручная owner приёмка не выполнена, activation/outbound OFF.

## Checkpoint 2026-10-12 01:04 МСК: fence прогрессирует только в проверенной текущей lineage

В synthetic bound deployment два последовательных backup имеют разные barrier IDs и возрастающий `barrier_fence`; matching native quiescence, финальный maintenance gate open и принадлежащий native процесс жив. Весь затронутый набор — **80 passed / 0 failed / 1 прежнее предупреждение / 38,67 с / exit 0**. Между restored control stores fence lineage ещё не доказана; R5 остаётся `PARTIAL_OFFLINE`.

## Checkpoint 2026-10-12 01:02 МСК: restored generation не может создать следующее backup до reconciliation

Добавлен offline synthetic chain test: backup → restore → новая backup-служба на restored authorities. Следующая generation отклоняется с `backup_source_reconciliation_required` до создания backup root/destination; native PID и исходный gate не меняются. Backup/restore guard набор: **79 passed / 0 failed / 1 прежнее предупреждение / 38,54 с / exit 0**. Это не доказывает монотонную maintenance fence после снятия hold; R5 остаётся `PARTIAL_OFFLINE`.

## Checkpoint 2026-10-12 00:58 МСК: офлайн restore fault boundaries

Расширен R5 fault-injection: остановка сразу после каждого из пяти manifest copy entries и перед итоговым `restore.json`. `restore.partial.json` блокирует runtime в каждом окне, уже пройденные guard-insert этапы и все четыре authority перед финальной metadata проверены. Backup/restore guard набор: **78 passed / 0 failed / 1 существующее предупреждение / 35,95 с / exit 0**. Полный backend/audit прогон 5 277 тестов выполнен до последнего backup-writer изменения, после него не повторялся; R5 остаётся `PARTIAL_OFFLINE`.

R4 offline deny-all worker/parent набор повторно прошёл **7/7**. `go.exe` не найден в PATH, поэтому Go/helper/cache acquisition не запускались; публичным proxy и другим сетевым сервисам ничего не отправлялось. Candidate `NOT LAUNCHABLE / NOT QUALIFIED`, module names/versions остаются офлайн.

Ручная синтетическая fixture доступна до **01:09:10 МСК**: [fixture](http://localhost:52888/fixture) и [Team UI](http://localhost:52888/team/). Ручной receipt не подтверждён. Activation/outbound OFF; никаких live Polza/MAX/Vikunja/credentials/owner DB не использовали.

## Checkpoint 2026-10-12 00:50 МСК: backup также сохраняет root-level restore hold

Воспроизведён второй R5 gap: при `restore.json` со статусом `restored_for_reconciliation`, но без guard row в исходных БД, новый backup возвращал `complete` и не включал marker в архив. `BackupService.backup()` теперь откладывает операцию при source restore marker до любых writes и повторно проверяет его после freeze/write-lock до создания destination. RED — ожидаемый `complete` вместо `deferred`; после исправления backup/restore + Secretary guard набор — **76 passed / 0 failed / 1 прежнее предупреждение / 34,00 с / exit 0**. Полный backend/audit **5 277 PASS** был завершён после предыдущего partial-marker fix, до этого последнего backup-writer изменения; полный набор после него ещё не повторялся.

R5 остаётся `PARTIAL_OFFLINE`: доказаны и закрыты два локальных среза — crash до первой копии и запрет следующего поколения при root-only historical hold; непрерывность всех generations/fences и полный crash recovery не доказаны. Локальный synthetic handoff на [fixture](http://localhost:52888/fixture) и [Team UI](http://localhost:52888/team/) доступен до **01:09:10 МСК**. Activation/outbound OFF, только offline/synthetic.

## Checkpoint 2026-10-12 00:47 МСК: crash-before-first-copy restore marker fail-closed

RED→GREEN воспроизвёл R5 crash-window: после записи `restore.partial.json`, но до первой копии DB, `restore_guard_present()` возвращал `False`. Detector теперь считает наличие этого marker-а блокировкой; добавлены regression для partial restore и для runtime startup, который должен остановиться до создания control DB. RED — 1 ожидаемый fail; GREEN — **2 passed / 72 deselected**; полный backend/audit suite — **5 277 passed / 0 failed / 1 существующее Starlette/httpx warning / 1 474,83 с / exit 0**.

R5 остаётся `PARTIAL_OFFLINE`: исправлен конкретный crash-before-first-copy путь, но непрерывность всех generations/fences и полный crash recovery не доказаны. Новая видимая fixture: [synthetic input](http://localhost:52888/fixture), [Team UI](http://localhost:52888/team/), run `t9-http-ui-d0c2515d982d47269111f44eba20e0ca`, порт `52888`, TTL до **01:09:10 МСК**. Это disposable `HTTP_SYNTHETIC_UI_ONLY`; визуальное подтверждение кнопок и owner manual acceptance всё ещё открыты. Только offline/synthetic, activation/outbound OFF.

## Checkpoint 2026-10-12 00:20 МСК: расширенная offline-проверка R6 и ручной handoff

Полный прогон шести R6-модулей (`restore_source_preparation_control`, `restore_activation_context`, `restore_runtime_evidence`, `restore_activation_decision`, `team_backup`, `team_runtime_recovery`) завершился **260 passed / 0 failed / 194,45 с / exit 0**; один существующий Starlette/httpx deprecation warning. `test_team_ui_http_fixture.py` — **7 passed**, frontend `test:processing` — **300 passed**, `test:release-manifest` — **1 passed**. Полный backend suite ранее прошёл 5 275 tests; production code в этом checkpoint не менялся.

Локальный `HTTP_SYNTHETIC_UI_ONLY` стенд виден в браузере. HTTP fixture tests прошли, однако браузерная попытка нажать «Новый синтетический код» не подтвердила изменение страницы; это не засчитано как визуальный/ручной PASS. Стенд остаётся для ручной проверки до **00:38:32 МСК**: [fixture](http://localhost:59493/fixture), [Team UI](http://localhost:59493/team/). R6 остаётся `PARTIAL_OFFLINE`; R4/R5, owner acceptance и live/24h gates открыты, activation/outbound OFF. Только synthetic/local: без proxy egress, Polza, MAX, production Vikunja, credentials и пользовательских данных.

## Checkpoint 2026-10-12 00:08 МСК: R5 active historical holds block before restore writes

RED выявил, что архив с историческим restore hold доходил до отказа после создания destination и частичного копирования. `_validate_archive()` теперь read-only проверяет все четыре authorities общим detector-ом до создания папки назначения; active guard row, restore-specific trigger и `restore.json` отвергают архив. Пустая штатная Billing guard table допустима. Три новых parametrized regressions — PASS; связанный batch **213 PASS**; полный offline `tests audit/tests` — **5 275 PASS / 0 FAIL / 1 прежнее Starlette/httpx warning / 1 582,99 с / exit 0**. Полный лог не сохранялся.

R5 остаётся PARTIAL_OFFLINE: подтверждён запрет переноса активного исторического hold в новую restore-папку, но непрерывность всех fences/generations и crash recovery не доказана. R4 IN_PROGRESS; R6 и owner/manual/live acceptance открыты. Activation/outbound OFF. Новая fixture на [синтетическом входе](http://localhost:59493/fixture) и [Team UI](http://localhost:59493/team/) действует до 00:38:32 МСК; ручной сценарий — [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md).

## Checkpoint 2026-10-11 23:24 МСК: R3 context diagnostic verified, full offline suite green

Добавлена read-only диагностика существующего R3 restore-context resolver в TeamRuntime startup path. При restore marker runtime сообщает фиксированный результат проверки контекста, но в любом случае блокирует запуск до control DB/workers/providers/outbound; это не разрешение на activation. Новый regression прошёл RED→GREEN; context module **29 PASS**, связанный restore/runtime batch **181 PASS**, полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5 272 PASS / 0 FAIL / 1 прежнее Starlette/httpx warning / 1 422,37 с / exit 0**. Полный terminal вывод не сохранялся в отдельный лог.

Уточнение по guard detector: пустая Billing `maintenance_restore_guard` создаётся штатной схемой и сама не доказывает restore; тест закрепляет это. Restore row и отдельные residual protocol markers продолжают fail-closed проверяться. Обновлённая локальная fixture: `HTTP_SYNTHETIC_UI_ONLY`, run `t9-http-ui-c7de7fccae7a40bfb69ee1fc4ac39625`, port `49773`, PID `57992`, действует максимум до **23:55:52 МСК**; три нужных HTTP маршрута и Secretary `/health` дали 200/ok. См. [`TEAM_TESTING_CURRENT.md`](../TEAM_TESTING_CURRENT.md).

Все проверки офлайн и синтетические; Go proxy, Polza, MAX, production Vikunja, credentials и пользовательские БД не использовались. R4 остаётся INCOMPLETE: общего proof/source-generation-bound predicate, native authenticated GET/advancing head и свежей owner/provider/execution-auth lineage нет. R5/R6 и ручная/live acceptance открыты; activation/outbound OFF. Продолжаем только офлайн согласно решению владельца.

Ruling: не подключать этот диагностический preflight как разрешающий activation predicate, пока не доказаны native authenticated GET/advancing head, текущие owner/provider decisions и свежая execution/auth lineage — иначе частичные локальные признаки могут ошибочно открыть восстановленный runtime; цена решения — production activation остаётся недоступной до выполнения этих gates.

## Checkpoint 2026-10-12 00:14 МСК: R6 synthetic fault/no-replay subset revalidated

Выделенная выборка из шести модулей (`restore_source_preparation_control`, `restore_activation_context`, `restore_runtime_evidence`, `restore_activation_decision`, `team_backup`, `team_runtime_recovery`) прошла **39 passed / 221 deselected / 34,26 с / exit 0**. Collect-only подтверждает сценарии partial preparation, crash before second commit, изменённые четыре sources/retained inputs, mismatched process proof, повтор owner nonce, частичный backup/restore, старый outbox и startup без replay/dispatch. Это закрывает только синтетическую offline часть R6; актуальное owner/provider решение, реальное восстановление и 24-hour пилот остаются незапущенными.

## Checkpoint 2026-10-11 22:45 МСК: frontend suite повторно зелёный

На текущем worktree повторно прошли npm run test:processing (**300/300**), npm run test:release-manifest (**1/1**), npm run typecheck, npm run lint и npm run build (Vite 1 777 modules). Синтетические /fixture/status, /fixture, /team/ отвечают HTTP 200. Визуальный browser smoke нового run не выполнен: Browser Use daemon не подключился из-за ограничения Windows Job Object; обходной backend не использовался. Ручная проверка доступна по ссылкам в [TEAM_TESTING_CURRENT.md](../../TEAM_TESTING_CURRENT.md); owner acceptance pending. Live integrations и R4/R5/R6 по-прежнему не квалифицированы.

## Checkpoint 2026-10-11 22:29 МСК: restore hold установлен во всех четырёх authorities

Исправлен найденный backup/restore пропуск: после копирования и проверки каждого файла hold создаётся в Secretary, Team, Billing и Vikunja. Native-history validator распознаёт ровно валидную схему restore guard metadata, чтобы control table не принималась за business schema; частичная/изменённая схема завершается fail-closed. RED воспроизведён до фикса; backup tests 39 PASS, native-history regressions 2 PASS, affected restore/source-preparation batch 766 PASS; полный .venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short — **5 270 PASS / 0 FAIL / 1 существующее предупреждение / 1390,21 с / exit 0**. [Лог](../../../.runtime/team-rollout/r4-four-authority-restore-final-offline-20261011-190304.log), SHA-256 C7188BBDEC37E094D88AB1C5561FF8868B2003BF7936C923479BE15BBBA2DA66.

Публичные прокси и живые интеграции не вызывались; credentials и пользовательские БД не использовались. R5 частично сделан, но исторические generations/fences и crash recovery не квалифицированы. R4 общий source-generation-bound interlock, native GET/head, owner/provider decision и fresh lineage остаются незакрыты; activation/outbound OFF. Свежее локальное синтетическое UI окружение проверено по HTTP 200 и доступно до примерно 22:58 МСК; ссылка и сценарий — в [TEAM_TESTING_CURRENT.md](../../TEAM_TESTING_CURRENT.md).

## Checkpoint 2026-10-11 21:17 МСК: owner mutation dispatch fail-closed

В завершение офлайн R4 guard-аудита на MAX/Vikunja mutation clients добавлена проверка active owner maintenance ticket нужного kind и повторная read-only проверка четырёх bound restore authorities непосредственно перед HTTP mutation. RED 9/9; focused 9 PASS; связанный набор 614 PASS; полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` — **5267 PASS, 0 FAIL, 1 существующий warning, 1431,14 с, exit 0**. Полный лог: [r4-late-dispatch-guard-offline-20261011-175248.log](../../../.runtime/team-rollout/r4-late-dispatch-guard-offline-20261011-175248.log). Продолжение только offline; Go module metadata не отправлялась в публичные прокси.

Патч закрывает только dispatch seam: повторная проверка SQLite не атомарна с HTTP POST и не удерживает source generation после последнего read. Общий proof/source-generation-bound R4 interlock/predicate ещё нужен. Native authenticated GET/advancing head, current owner/provider decision и fresh execution/auth lineage остаются blockers; activation/outbound OFF. Synthetic UI fixture доступна до ~21:47 МСК для ручной проверки экранов и кнопок; owner acceptance pending. R4 остаётся IN_PROGRESS.

## Checkpoint 2026-10-11 19:50 МСК: offline deny-all egress candidate проверен

Зафиксирован RED→GREEN для isolated native worker и parent copies: `run_owned()` теперь отказывает любому online Go child или неполной offline env до process/Job inspection и `Popen`; parent принимает этот фиксированный failure code, не квалифицируя запуск. Worker — **4/4**, parent terminal/receipt — **2/2**; только synthetic tests, Go/process/network/SQL/native operations — 0. Frozen helper `69a1aef1…` и parent `e9a8842d…` неизменны. Новый worker SHA `eb61ec8d79a5784e024e0279e8e5996bcf8f4cf8b9a22d99d42e4d6ba145085b`, parent SHA `e341182490d0703970d8403485ab8db253ea72421b7913216000939e562bdf61`. Candidate — deny-all для текущего offline-only режима, NOT LAUNCHABLE/NOT QUALIFIED; complete pins/receipt freeze, independent review и новый exact allowlist нужны до будущего public metadata запроса. Подробности: [`TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md`](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md).

## Checkpoint 2026-10-11 19:39 МСК: локальный ручной handoff обновлён, R4 остаётся offline-only

Повторный source review замороженного native dependency worker подтвердил известный P1: `mvs-list` отправляется с публичным `GOPROXY` до локальной проверки транзитивных записей. Проверенные SHA источников совпали с текущими pins (`check_dependencies.py` `69a1aef1…`, `run_dependencies.py` `e9a8842d…`); candidate из 319 module paths не отправлялся, Go/helper/proxy не запускались. По решению владельца внешний metadata acquisition запрещён; замороженная карточка сохранена без изменений. R4 остаётся IN_PROGRESS, activation/outbound OFF.

Для продолжения доступной ручной проверки запущен новый synthetic-only loopback fixture: `t9-http-ui-65ec88f0a8974f2cb513a14665d33450`, `localhost:64784`, PID `71596`, max lifetime 30 минут. `/fixture/status`, `/fixture`, `/team/` и основной `/health` вернули HTTP 200. Ссылка Team UI поставлена в очередь панели Codex; актуальные ссылки и безопасный сценарий записаны в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md). Тест не касается Polza/MAX/рабочих данных и не закрывает owner manual/TLS/телефон/production gates.

## Checkpoint 2026-10-11 19:25 МСК: synthetic browser workflow verified

В loopback-only `HTTP_SYNTHETIC_UI_ONLY` browser smoke успешно проверены вход свежим одноразовым кодом, Today/Kanban/Matrix, filter «Мои/Все», refresh, task create preview→confirm→verified receipt, Eisenhower quadrant preview→confirm, status preview→confirm и cancellation без изменения уже принятого статуса. Первый код истёк по ожидаемому TTL 5 минут; просроченный код был отклонён без создания сессии. Карточка/изменения остаются только в disposable fixture. Полный UI smoke и ограничения записаны в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md); ручная приёмка владельца, MAX/TLS/реальные уведомления, R4 activation, live integrations и 24-hour pilot открыты.

## Checkpoint 2026-10-11 19:15 МСК: frontend offline verification

После полного offline backend/audit прогона текущий frontend повторно проверен: processing **300 PASS**, release-manifest **1 PASS**, typecheck/lint/production build — exit 0; `git diff --check` — exit 0. Итоги и логи внесены в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md). Loopback-only synthetic fixture после сборки доступен (`/fixture/status`, `/fixture`, `/team/` — HTTP 200); owner manual acceptance, внешние интеграции и production gates остаются открытыми. Внешние сервисы и публичные Go proxy не вызывались.

## Checkpoint 2026-10-11 19:10 МСК: ручной synthetic handoff обновлён

После завершения полного backend/audit набора поднята новая disposable fixture `HTTP_SYNTHETIC_UI_ONLY`: run `t9-http-ui-46de18b368a144a981d9c4903c69918e`, fixture `t9-ui-93f22c61545c490497512e88311e5b36`, `http://localhost:53493`, PID `39480`, watchdog до ~19:40 МСК. `/fixture/status`, `/fixture`, `/team/` — HTTP 200, status ready. Ссылки и сценарий — в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md). Только loopback и mock Vikunja; owner manual acceptance, live API, MAX WebView/телефон, R4 activation и 24h pilot не подтверждены.

## Checkpoint 2026-10-11 19:09 МСК: R4 paid-dispatch guard strengthened, offline full pass

RED показал, что Polza paid check видел Billing, но пропускал partial R4 marker в Secretary/Team/Vikunja. Добавлены шесть parametrized regressions: все три non-Billing sources отказали тесту до исправления — отдельно при preflight и при marker, созданном после durable checkpoint перед dispatch. `assert_paid_billing_allowed()` теперь принимает все четыре read-only authorities; API передаёт Secretary + Billing, TeamRuntime — все четыре. Перед `mark_submitted` authority перечитываются; при отказе unsubmitted charge освобождается, HTTP не вызывается, failure checkpoint сохраняется.

Affected offline набор: **286 passed / 0 failed / 1 существующее Starlette/httpx warning / 27,89 с**. Обязательный полный `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short`: **5 258 passed / 0 failed / 1 прежнее предупреждение / 1 391,73 с / exit 0**, [лог](../../.runtime/team-rollout/r4-shared-paid-guard-offline-20261011.log). Test transport — `httpx.MockTransport`; live Polza/MAX, public proxy, credentials и пользовательские данные не использовались.

R4 остаётся IN_PROGRESS: этот sub-boundary не заменяет общий transaction-bound activation predicate, native authenticated GET/advancing head, current owner/provider decision или fresh execution/auth lineage. Activation/outbound OFF; R5–R6 и ручная/24-hour/live приёмка остаются открытыми. Go module paths наружу не отправлялись согласно offline-only решению владельца.

## Checkpoint 2026-10-11 18:11 МСК: полный backend/audit повтор завершён, продолжаем offline-only

Исправлены две найденные границы: Polza paid preflight теперь использует общий read-only restore detector и блокирует остаточный R4 marker без базовой guard-таблицы; `PlaybackCache` разрешает равные `time_ns()` порядковым счётчиком, чтобы LRU удалял реально наименее использованный файл. Для LRU сначала добавлен regression, который упал на прежнем коде; после исправления весь cache-набор прошёл **4/4**. Полный повтор `.venv\\Scripts\\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` завершился по pytest summary: **5 251 passed, 0 failed, 1 прежнее Starlette/httpx предупреждение, 1 348,36 с (22:28)**; полный лог — `.runtime/team-rollout/full-offline-20261011-1605.log`.

Владелец решил продолжать только офлайн. Кандидат из 319 Go module names/versions (SHA `f236804a…`) не отправлялся в `proxy.golang.org`/`sum.golang.org`; внешние metadata-запросы запрещены до нового решения. Свежий handoff: `HTTP_SYNTHETIC_UI_ONLY`, run `t9-http-ui-a2e3e2e760994c01a58bbe5d15512af1`, порт `50302`, PID `49976`, TTL до `18:40:36` МСК; `/fixture/status`, `/fixture`, `/team/` → HTTP 200. Ссылки и безопасный сценарий — в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md). Manual acceptance, native TLS/MAX/телефон, live integrations, R4 activation, R5–R6 и 24-hour pilot остаются открытыми; activation/outbound OFF.

## Checkpoint 2026-10-11 15:14 МСК: HTTP handoff открыт, runtime gates перепроверены

Старый HTTPS-переход не использовался и сертификаты не обходились. В Chrome профиля Dmitry открыта действующая синтетическая fixture `http://localhost:55667/fixture`; Team UI доступен по `http://localhost:55667/team/`. Явный `status --run-root .runtime/team-rollout/t9-http-ui-2c7f450f9bdb48ddaf183c8915720ba7` подтвердил `state=ready`, PID `59076`, TTL до `15:19` МСК. Это короткоживущий `HTTP_SYNTHETIC_UI_ONLY` стенд; ручное подтверждение владельца всё ещё ожидается.

Повторно выполнены frontend **300/300** и targeted restore/startup/recovery/budget/final-dispatch **211/211** тестов; `git diff --check` PASS. R4 activation/outbound остаются OFF: общий predicate ещё не может быть безопасно подключён без фиксированного доверенного restore context, native authenticated GET/advancing head и fresh execution/auth lineage. Детали — [`TEAM_R4_RUNTIME_GUARD_VALIDATION.md`](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md) и [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md).

## Checkpoint 2026-10-11 15:00 МСК: локальный ручной handoff и UI/API регрессии

Старый HTTPS fixture не требует обхода предупреждения: актуальные страницы доступны по [localhost fixture](http://localhost:55667/fixture) и [Team UI](http://localhost:55667/team/) в disposable `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-2c7f450f9bdb48ddaf183c8915720ba7` (PID `59076`, port `55667`, TTL до `15:19:05` МСК). Cookie сохраняет Secure-флаги; чужие origin/host отклоняются. Browser smoke проверил три представления, проект/фильтр, refresh, черновик, status/quadrant previews и отмену без записи.

Исправлено движение задачи: текущие status/quadrant исключаются из меню. Проверки: 300 frontend tests, 1 release-manifest test, 1 636 offline Team/MAX/Vikunja/API tests — все passed; typecheck/lint/build — PASS. `/team/` не обязан отвечать на приватном Secretary `:8765`: канонический Team UI подаёт отдельный Gateway `:8766` за reverse proxy/public release; текущий synthetic fixture проверяет отдельный mock-контур, listener `:8766` отсутствует. Owner manual acceptance и live TLS/MAX/WebView/Vikunja/Polza/production остаются открытыми; activation/outbound OFF. Отчёт: [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md).

## Checkpoint 2026-10-11 14:27 МСК: исправлен restore metadata regression; handoff обновлён

Новый metadata guard различает Team restore (`state=restored_for_reconciliation`) и обычный local Secretary restore (`state=verified`). Runtime preflight и `protect_restored_connection()` теперь согласованно блокируют Team authority, если SQL markers отсутствуют, при этом локальный backup restore остаётся рабочим. Полный backend/audit прогон: **5 244 passed / 0 failed / 1 прежнее предупреждение Starlette/httpx / 1 575,02 с**; финальное сужение путей поиска проверено affected-набором **186 passed**, включая backup/restore coverage. Подробная запись — [`TEAM_R4_RUNTIME_GUARD_VALIDATION.md`](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md).

Текущая ручная fixture: `HTTP_SYNTHETIC_UI_ONLY`, run `t9-http-ui-0937f31834cb48648a4aef30022cc524`, port `54003`, PID `33616`, TTL до `14:55:14` МСК. Используйте [fixture](http://secretary-t9.localhost:54003/fixture) для выдачи синтетического кода и [Team UI](http://secretary-t9.localhost:54003/team/) для входа и ручного сценария; код не отправляйте в чат. Маршруты дали HTTP 200, но ручная приёмка всё ещё pending. TLS, MAX WebView, реальные интеграции, production и телефон не проверены; R4 activation/outbound выключены.

Основной Secretary поднят для локальной проверки по [http://127.0.0.1:8765/](http://127.0.0.1:8765/), в изолированном offline режиме и на пустой scratch-БД `.runtime/manual-offline-qa-20261011`. Health и главная страница отвечают; облако и API-ключ выключены, обработка через Polza здесь недоступна. Run PID `60140` зарегистрирован для штатной остановки через `scripts\stop.ps1`.

## Checkpoint 2026-10-11 13:04 МСК: HTTP handoff и актуальность R4 upstream

Старая HTTPS fixture не требует перехода через сертификат: действующий `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-4b034744539a4fd9a6efa657e4467502` на `secretary-t9.localhost:58303` отвечает HTTP 200. Прямые ссылки и пошаговая synthetic-проверка — в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md); run ограничен TTL до ориентировочно 13:18 МСК. Для ручного входа выпустить новый код на fixture и ввести локально; не пересылать его в чат. Реальные задачи, Vikunja, MAX и Polza не задействованы.

Повторная проверка официальных Vikunja releases/configuration/security docs не обнаружила нового релиза после v2.7.0 и документированного SQLite read-only режима. Проверка зафиксирована в [`TEAM_R4_NATIVE_READONLY_FEASIBILITY.md`](../../TEAM_R4_NATIVE_READONLY_FEASIBILITY.md). Она подтверждает продолжение прежнего R4 native GET blocker, не меняет NoWrite custody и не разрешает activation/outbound. Остальные owner/provider, R5–R6, ручные и live gates остаются открытыми.

## Checkpoint 2026-10-11 12:53 МСК: restore-marker guard и доступный T9 handoff

Устранён fail-open в общем restore guard: marker-таблицы `restore_source_epoch_*`, `secretary_r4_native_*`, `secretary_native_activation_v1_*` и остаточные `backup_restore_no_*` теперь блокируют startup/connection даже при отсутствующей `maintenance_restore_guard`. Пять targeted cases прошли RED→GREEN; отдельный residual-trigger test включён в смежный restore/runtime набор из 494 passed. Полный backend/audit suite — **5 241 passed, 0 failed**, одно существующее предупреждение Starlette/httpx, exit 0. Frontend: **299 processing + 1 release-manifest test passed**, typecheck/build и lint — PASS. Общая R4 остаётся INCOMPLETE; activation/outbound OFF.

Новый loopback-only `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-4b034744539a4fd9a6efa657e4467502` на порту `58303` дал HTTP 200 на `/fixture/status`, `/fixture`, `/team/`. Browser smoke подтвердил синтетический вход, 23 задачи проекта 7 в «Сегодня»/Kanban/матрице, обновление списка, карточку задачи, пустую форму/валидацию и preview с отменой без записи. Текущие вкладки оставлены для владельца: [`страница синтетических участников`](http://secretary-t9.localhost:58303/fixture), [`Team UI`](http://secretary-t9.localhost:58303/team/). Ручная приёмка, TLS/MAX/телефон, live integrations и production всё ещё не квалифицированы.

## Checkpoint 2026-10-11 12:03 МСК: ручная ссылка исправлена, R4 продолжается

Старая HTTPS-ссылка для T9 была неактуальна и требовала недоступного для пользователя перехода через предупреждение. В Chrome открыты актуальные HTTP fixture и Team UI на `http://secretary-t9.localhost:64250/`; run `t9-http-ui-55bec915237a44a4a1848cebd3642eac`, PID `53920`, истекает в 12:28:21 МСК. `/fixture/status`=200, `127.0.0.1` отклоняется предусмотренной проверкой hostname. Пользователь может открыть прямые HTTP-ссылки из [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md), выпустить новый синтетический OTP локально и войти в Team UI; код в чат не передавать. Это не закрывает ручную приёмку. R4 остаётся `IN_PROGRESS`, активация и outbound выключены; следующий engineering шаг ограничен общим fail-closed permission predicate и не обходит отсутствующие native authenticated GET/advancing head и fresh execution lineage.

## Checkpoint 2026-10-11 12:05 МСК: проверены runtime seams общего predicate

Текущий `TeamRuntimeSettings` не содержит фиксированного backup/restore/lifecycle контекста, а R4 consent/blocked-decision APIs не вызываются runtime. Существующий collector требует эти пути и проверяет текущие четыре source authorities; ledger пока записывает только `activation_blocked`. Добавлен статический аудит [TEAM_R4_RUNTIME_GUARD_VALIDATION.md](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md). Следующая реализация начинается с доверенного fixed-path resolver-а, привязанного к restore/epoch identity, затем общего predicate на всех обозначенных boundary points. Caller Boolean или произвольный evidence path не принимаются; активация/outbound остаются OFF до native GET/advancing head, fresh lineage и полноценного current decision.

## Checkpoint 2026-10-11 10:36 МСК: T9 narrow viewport прошёл synthetic browser flow

Отдельный изолированный `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-7a60f9a1ac30485f908d67b808784f1e`/58995 прошёл authorized Today/Kanban/Matrix при 390×844 и 320×740 с mobile/touch emulation. Document width всегда равен viewport; Matrix не переполняет экран; Kanban использует внутренний горизонтальный scroll region. На 320 px status preview→confirm получил квитанцию `Применено и проверено`; та же задача со статусом «В работе» видна в Today, Kanban и Matrix. Изолированный run остановлен; screenshot и детали записаны в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md). Это не проверяет реальный touch/телефон/MAX WebView. Ручной handoff владельцу на порту64322 оставлен живым до10:56:53 МСК.

## Checkpoint 2026-10-11 10:28 МСК: HTTP handoff после TLS interstitial

Chrome не пропускает пользователя через локальное HTTPS-предупреждение; trust store и сертификаты оставлены без изменений. Новый loopback-only `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-28ea7b3c61d74643b58eea793ddcafbb` (`t9-ui-3aceeeee986a4566953b61ba22a24aa7`), порт `64322`, PID `40316`, старт 10:26:53 МСК, максимальный TTL до 10:56:53 МСК. `/fixture/status`, `/fixture`, `/team/` дали HTTP 200; fixture и Team UI открыты в Codex In-app Browser. Подробные шаги ручного входа и теста — в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md). Ручная приёмка владельца остаётся открытой; synthetic run не квалифицирует TLS, MAX/телефон, живые интеграции или production.

## Checkpoint 2026-10-11 10:26 МСК: status control закрыт для synthetic UI, ручная приёмка открыта

На отдельном `HTTP_SYNTHETIC_UI_ONLY` run подтверждён полный browser status-flow: preview `Новая` → `В работе`, статус не меняется до подтверждения, серверная квитанция `Применено и проверено`, та же задача видна как «В работе» в Канбане, Матрице и «Сегодня». Manual run и disposable status run истекли; PID/listener отсутствуют. Owner manual acceptance и narrow viewport/touch остаются открытыми.

Offline proof от 10.10: **632 API contract tests PASS**, **280 restore/quarantine tests PASS**, frontend **299/299**, typecheck/lint PASS, изолированный Vite build **1777 modules/8.92с/exit0**. У каждого backend набора осталось по одному известному Starlette/httpx deprecation warning. Эти проверки не закрывают реальный MAX/Polza/Vikunja, HTTPS/WebView, owner/provider reconciliation или 24-hour pilot. R6 synthetic crash/changed-proof/no-replay subset получил свежий targeted PASS, но вся R6 остаётся INCOMPLETE; R5 generation hold не реализован.

R4 source review остаётся заблокированным до egress guard/review/fresh freeze и решения владельца по передаче 319 module paths/versions публичным Go services; никаких новых module paths этим продолжением не отправляли.

## Checkpoint 2026-10-10 13:14 МСК: свежий UI handoff, автоматический smoke и открытый R4 gate

Для ручной синтетической проверки открыт [HTTP fixture](http://secretary-t9.localhost:49939/fixture) и [Team UI](http://secretary-t9.localhost:49939/team/) в Codex In-app Browser; run `t9-http-ui-733c82eae4154755b11b07b108e72339`, port `49939`, PID `52148`, max lifetime 30 минут от 13:10:38 МСК. `probe_team_ui_http.py status`=`ready`; процесс/listener присутствуют, три HTTP GET дали 200. Старые detached runs могли завершаться с manifest, всё ещё помеченным ready; текущий сервер удерживается открытой сессией запуска. Подробные шаги и ограничения — в [`TEAM_TESTING_CURRENT.md`](../../TEAM_TESTING_CURRENT.md). Ручная приёмка владельца pending.

На отдельном disposable synthetic run автоматизированный smoke подтвердил OTP, загрузку проекта/задач, переключение Today/Kanban/Matrix и фильтра, сохранение большого project ID, preview/cancel черновика и preview/confirm отдельной задачи с verified receipt. Выбор статуса автоматизацией не подтверждён; изменение не сохранялось. Fixture-contract subset ранее прошёл **6/6**, одно существующее предупреждение Starlette/httpx. Ни TLS/MAX WebView/телефон, ни production integration не квалифицированы.

Source review R4 выявил P1: online `mvs-list` использует публичные `proxy.golang.org`/`sum.golang.org` до проверки module paths; `validate_pair()` проверяет только синтаксис, а parent receipt validation наступает после запроса. Поэтому текущие launch pins не запускать. Нынешние 72 pure/model checks проходят, но не квалифицируют реальный Go/proxy egress. Никакие module paths в этой проверке не отправлялись.

До продолжения acquisition нужно решение владельца по передаче exact 319 module path names и версий публичным Go services; затем fail-closed запросный allowlist, регрессия «вне набора блокируется до передачи», независимое review и новый exact freeze. Точный набор и SHA зафиксированы в [`TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md`](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md). До выполнения gate activation/outbound OFF, R4 INCOMPLETE.

## Checkpoint 2026-10-09 11:43 МСК: R4 policy hardening и HTTP handoff

В draft-карте R4 закрыты три source-level findings: process cap на Windows Job, проверка фактического file handle с fail-closed неизвестной идентичностью, независимая семантическая валидация online/list/graph/offline env manifests. Пять standalone pure-наборов после RED→GREEN прошли **72/72**; actual helper/Go/GCC/network/SQL/native API — **NOT RUN**. Контракт обновлён и совпадает между root и private draft (`4db394b9…081ad6`).

Статус фазы R4 остаётся **INCOMPLETE**: необходимы независимое post-repair review, fresh exact launch freeze и отдельное решение владельца по 319 точным module paths до любых public proxy requests. Общий activation/outbound остаётся OFF.

Для ручного UI открыт свежий [HTTP synthetic fixture](http://secretary-t9.localhost:50754/fixture) и [Team UI](http://secretary-t9.localhost:50754/team/), run `t9-http-ui-668ab0c2cdd34cdaa3cac957c52b5d70` (до ориентировочно 12:13 МСК); доступность страницы без TLS interstitial проверена. Данные полностью синтетические, Vikunja mock; ручная приёмка владельца, TLS, MAX/телефон, Polza и 24-hour operation не квалифицированы.

## Checkpoint 2026-10-09 11:22 МСК: HTTP handoff продлён для ручной приёмки

Владелец сообщил, что Chrome не позволяет пройти локальное TLS-предупреждение. Сертификаты и trust store не менялись. Подготовлен новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-5adc729f98034ea7aa084a84ce9f991d`, port `58637`, TTL 30 минут, ориентировочно до 11:52 МСК. [Fixture](http://secretary-t9.localhost:58637/fixture) и [Team UI](http://secretary-t9.localhost:58637/team/) открыты в браузере; `/fixture`, `/team/`, `/fixture/status`=200, основной Secretary `/health`=200. Synthetic-only: MAX, Polza, рабочие данные и платежи не использовались; owner manual acceptance остаётся pending.

Свежая fixture: `tests/test_team_ui_http_fixture.py` **5 passed**, status ready. Затронутые startup/backup checks **12 passed**; PowerShell syntax и `git diff --check` прошли. Осталось известное предупреждение Starlette/httpx deprecation и Git LF→CRLF advisory. Это офлайн/loopback evidence, не live acceptance.

## Checkpoint 2026-10-09 11:02 МСК: offline Secretary relaunch and HTTP handoff without TLS warning

Пользователь сообщил, что пройти локальное TLS-предупреждение Chrome не удаётся; сертификат и trust store не менялись. Для ручной проверки используется новый точный HTTP-origin `http://secretary-t9.localhost:50841`; certificate bypass не нужен. Запрос к `127.0.0.1:50841` возвращает ожидаемый 403 из-за проверки Host, поэтому hostname менять нельзя. Временная fixture имеет TTL 30 минут от старта (примерно до 11:24 МСК).

Основной Secretary запущен через `scripts/start.ps1 -Offline -NoBrowser -NoBootstrap` на `127.0.0.1:8765`, run id `73fcca73-143f-417c-af0b-5d88deb8ae35`, отдельная пустая БД в `.runtime/manual-offline-34c62dfd1bd04de299f58e0969911f7c/data`; outbound=false. `/health`=`ok`, `/`=200, `/team/`=404, `doctor.ps1` подтвердил identity и offline guard. Это не Team runtime и не production-активация.

Перед запуском исправлена ротация логов: `scripts/start.ps1` раньше перезаписывал два общих файла `server.stdout.log`/`server.stderr.log`. Теперь новые логи получают имя с launch UUID, записываются в `processes.json` и не затрагивают старые журналы. RED: 2 passed/1 failed; GREEN: 3 passed. Совместная startup/instance-lock/backup выборка: 12 passed, 1 прежнее Starlette/httpx deprecation warning; PowerShell parser и `doctor.ps1` прошли.

Для Team UI поднята независимая `HTTP_SYNTHETIC_UI_ONLY` fixture `t9-http-ui-9e49e01375fe497aad61d24404c48b19`, порт50841. [Синтетические участники](http://secretary-t9.localhost:50841/fixture), [Team UI](http://secretary-t9.localhost:50841/team/) и `/fixture/status` отвечают 200. Старый тестовый порт65381 истёк. Коды доступа генерируются только на fixture-странице и действуют пять минут; не пересылать их в чат. Team UI использует synthetic SQLite/mock Vikunja; реальные данные, MAX, Polza и платные запросы не задействованы.

В IAB открыта главная Secretary и экран входа Team. На основной странице безопасно проверены открыть/закрыть Settings и Participants, на пустой базе; сохранений не было. Settings сообщает, что серверный Polza key настроен, но показывает только флаг; cloud processing=false. Значение ключа не читалось и не отображалось, outbound вызовов Polza не было. «Новая встреча» не создала запись; «Обновить состояние» не изменило страницу. Реальная запись, импорт файла и разрешение микрофона не запускались.

После повторного прогона startup/instance-lock/backup affected suite: **12 passed, 0 failed, 1 прежний Starlette/httpx deprecation warning, 5,51 с, exit 0**. `doctor.ps1`, PowerShell parse и `git diff --check` прошли. Owner manual acceptance не выполнен; сценарий и временные ссылки записаны в `docs/TEAM_TESTING_CURRENT.md`.

Решение по exact 319 module paths/public proxy не получено; acquisition не запускался. R4–R6, narrow viewport/touch, HTTPS/MAX, live Polza/Vikunja и 24h pilot остаются открытыми; общий goal ACTIVE.

## Checkpoint 2026-10-08 23:00 МСК: synthetic UI smoke and HTTP handoff

Пользователь сообщил, что пройти локальное TLS-предупреждение в Chrome не удаётся. Trust store и сертификаты не менялись; для ручной проверки подготовлена отдельная `HTTP_SYNTHETIC_UI_ONLY` fixture на loopback, порт65381, истекает примерно через 23:20 МСК. В браузере Codex открыты [синтетические участники и одноразовые коды](http://secretary-t9.localhost:65381/fixture) и [Team UI для входа](http://secretary-t9.localhost:65381/team/); `/fixture` и `/team/` ранее ответили 200. Одноразовый код обновлён на странице fixture и не записывается в отчёт. Стенд использует disposable SQLite и mock Vikunja; реальные MAX, Polza, рабочие встречи и данные не используются.

В текущем synthetic browser smoke проверены переключение Сегодня/Канбан/Матрица, выбор проекта и фильтра, создание задачи без выдуманного срока, подтверждение preview, проверенная квитанция, установка «Важно + Срочно» и смена статуса на «В работе» с переходом карточки в соответствующую колонку. Для каждого изменения серверная квитанция сменилась с «Принято в очередь» на «Применено и проверено». Ширина 390 px и touch в этом проходе не эмулировались; responsive CSS breakpoint есть, но ручное узкое окно и MAX WebView остаются открытыми. Ручная приёмка владельца pending.

## Checkpoint 2026-10-08 22:25 МСК: R4 private-module policy approval pending

Fresh scoped security review текущего dependency card: 0 reportable findings, coverage partial; открыт конкретный policy gap — синтаксис пути не классифицирует public/private GitHub/custom-domain репозиторий, а online MVS может отправить путь публичному proxy. Создан и locally verified кандидат из **319** точных путей pinned `go.sum`, `OWNER_REVIEW_REQUIRED`; сетевых обращений не было. До решения владельца нельзя выполнять acquisition. Подробности, hashes и варианты решения в [T12R dependency evidence](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md). После решения: exact allowlist/fail-closed code and tests → independent review → new source freeze → только затем bounded metadata attempt. Остальные gates не закрываются этим checkpoint; общий goal ACTIVE.

## Checkpoint 2026-10-08 21:52 МСК: R4 source revalidated before fresh review

Текущий registry новой dependency-card перепроверен по рабочей копии: **310 файлов / 4 875 473 байта**, 0 missing, 0 SHA mismatch, 0 unsafe paths. AST parse текущих worker и parent прошёл; pure receipts подтверждают актуальные worker/parent/contract hashes, **56 worker/policy/trace/parallel + 14 parent groups PASS**. [Receipt](../../.runtime/team-rollout/r4-native-ro-dependencies-20261008-cd32aa5b96be478c875d439467cefbf8/pre-review-source-revalidation-20261008.json).

Статус ограничен read-only source revalidation: он не заменяет независимое ревью и launch freeze. Текущие review receipts имели несовпадающие SHA и помечены историческими. Свежий review type selection ещё pending; до актуального ревью и freeze actual helper/Go/network/SQL/native запуск не выполняется; metadata preparation и full R4 остаются незакрытыми.

## Checkpoint 2026-10-08 21:48 МСК: TLS handoff repaired with bounded HTTP fixture

Пользователь не смог пройти Chrome TLS interstitial. Trust store и сертификаты не изменялись. Предыдущий HTTP handoff истёк; создан свежий `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-b33bc949420c4ae5a850194d5b48d8d1`, port54652, TTL около30 минут (до22:15 МСК). `/fixture/status`, `/fixture` и `/team/` ответили HTTP200. Focused `tests/test_team_ui_http_fixture.py`:5 passed/0 failed/1 warning,14,63 с/exit0. Ссылки: [синтетические участники и одноразовые коды](http://secretary-t9.localhost:54652/fixture), [Team UI](http://secretary-t9.localhost:54652/team/). Пользователю оставлен прямой HTTP-вариант; ручная приёмка пока pending.

Основной Secretary запущен на loopback `127.0.0.1:8765` с `--offline`, отдельной scratch-БД и run id `521691f0-f735-4195-ad5c-1d0b4c56a557`; `/health` возвращает `ok`. Team runtime не смонтирован в этом процессе (`/team/` →404), поэтому Team UI остаётся отдельной синтетической fixture. Рабочие встречи, live Vikunja/MAX/Polza и расходы не задействованы.

## Checkpoint 2026-10-08 21:34 МСК: контрактные тесты и свежая R4 source binding

- API/Team/MAX/Polza/Vikunja/OpenAPI offline suite: **2 066 passed, 0 failed, 0 skipped, 1 warning** across 55 files; [полный список и receipt](../../.runtime/team-rollout/secretary-team-api-test-20261008.json).
- Frontend: **289/289 tests**, typecheck, lint and isolated production build PASS; [receipt](../../.runtime/team-rollout/frontend-validation-20261008.json).
- Synthetic browser smoke: PASS for task views, filters and preview/confirm/verification flows on mock Vikunja. The HTTP handoff in this checkpoint expired around 21:44 MSK; the fresh owner link is in the 21:46 checkpoint above.
- R4 now has a new versioned card bound to the present working tree: exact 310-source registry SHA256 `48c59ac6b19aebef3f5032afcc133f61707f1d9cd3210170a0040e8d8d5590e0`, 310/310 current hashes, 70/70 pure modeled probes. The prior stale-pin card remains closed and unchanged.
- **Still IN_PROGRESS:** independent code/security review and exact launch freeze; actual helper/Go dependency acquisition; full R4; live MAX/Polza/Vikunja, phone/HTTPS owner acceptance and 24-hour soak. No live service, Go, network, SQL, payment, or native operation was performed in this checkpoint.


## Checkpoint 2026-10-08 21:15 МСК: HTTP handoff и актуальный T9 synthetic smoke

Chrome не позволил владельцу пройти локальное TLS предупреждение. Для ручного осмотра открыт отдельный `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-d1fb2efea80247768b96346f014227a0`, порт58474, max lifetime30 минут: [fixture](http://secretary-t9.localhost:58474/fixture) и [Team UI](http://secretary-t9.localhost:58474/team/). Страница входа открыта в Chrome; ручная приёмка владельца остаётся pending.

В disposable run выполнен browser smoke: login, Today/Kanban/Matrix, own/all filter, большой project ID, comment/status/priority/proposed-due/create command preview→confirm→verified receipt, assignment/title preview, due toggle и text rendering XSS-shaped string. Подробный receipt: [browser-outcome.json](../../.runtime/team-rollout/t9-http-ui-34f01000f62d4859b171f20c2ff4eda0/browser-outcome.json). Smoke process остановлен. Это synthetic UI evidence; TLS, production Vikunja, MAX, Polza, phone и 24-hour qualification отсутствуют.

Главный план остаётся **IN_PROGRESS**: R4–R6 и owner/live gates открыты. Следующий R4 acquisition-card не должен переиспользовать закрытый запуск, упавший на `pin_changed`; текущая рабочая копия является источником истины по последней инструкции пользователя. Любой новый профиль/карточка должны быть свежими, неизменяемо закреплёнными и ограничены текущими источниками без затрагивания Git state.

## Checkpoint 2026-10-08 15:25 МСК: offline full pass and restore-copy integrity

Пользователь не смог продолжить через Chrome TLS interstitial; trust store не изменён. Новая одноразовая loopback fixture `t9-http-ui-9f630b670c2c4d40a117dcc6918f5739` на port62171 открыта в браузере Codex по HTTP; синтетические ссылки и статус — в [TEAM_TESTING_CURRENT.md](../../TEAM_TESTING_CURRENT.md). Owner manual acceptance pending; live MAX/Vikunja/Polza, внешний HTTPS, телефон и 24h soak не проверены.

В локальном backup restore найден copy-time TOCTOU: исходная проверка архива была до копирования; regression воспроизвела принятие изменённого media. Теперь restore сравнивает размер и SHA-256 результата каждого media и database copy с манифестом. Focused backup/CLI/runtime evidence — **54 passed**; `py_compile` и `git diff --check` прошли, `ruff` недоступен в офлайн-venv.

Плановый полный запуск `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short` завершился **5 189 passed, 0 failed, 1 warning, 1 726,95 с (28:46), exit 0**. Предшествующий прямой `pytest -q` не использовал ожидаемый `scripts/` import path и дал два import errors; через проектный wrapper `tests/test_restore_runtime_evidence.py` прошёл **47/47**, а затем прошёл весь suite. Итог и ограничения зафиксированы в [журнале реализации](../../TEAM_IMPLEMENTATION_STATUS.md).

Статус остаётся **IN_PROGRESS**: Team restore reconciliation R4–R6 и human/live gates не закрыты. Резервное копирование проверено только на synthetic data; восстановление личной БД, проверка playback/export, работа на реальном устройстве и 24-часовой пилот остаются открытыми.

## Checkpoint 2026-10-08 14:15 МСК: renewed HTTP-only Team handoff

Пользователь не смог пройти Chrome TLS interstitial, поэтому без изменения trust store поднята новая одноразовая fixture `t9-http-ui-4c6562c20dc64b0babb4a1a95d449eb2`, порт50626, `HTTP_SYNTHETIC_UI_ONLY`, max lifetime30 минут. Fixture и Team UI открыты в Codex на [синтетической странице входа](http://secretary-t9.localhost:50626/fixture) и [доске](http://secretary-t9.localhost:50626/team/). Она использует mock Vikunja и тестовую БД; реальный MAX/Polza/owner data не задействованы. Ручная приёмка владельца всё ещё pending.

## Checkpoint 2026-10-08 14:10 МСК: offline Secretary исправлен и запущен на scratch-БД

Реальная ошибка: `create_app(outbound_enabled=False)` корректно clamp-ил загруженную настройку SQLite только в памяти, но `/api/v1/config` всё ещё принимал `cloud_enabled=true` и сохранял его. Добавлен fail-closed API guard (`409 runtime_outbound_disabled`), флаг `--offline` в `scripts/run_server.py` и `-Offline` в `scripts/start.ps1`; offline нельзя сочетать с Team runtime или принять за обычный уже работающий процесс. RED→GREEN регрессия покрывает сохранённое `cloud_enabled=true`; live probe повторил PATCH на отдельном пустом процессе и подтвердил 409/выключенное облако.

Affected suite: **133 passed, 0 failed** (`test_backend`, offline launcher, lifecycle, Team Secretary maintenance и gateway isolation). PowerShell parse PASS. Основной Secretary открыт на `http://127.0.0.1:8765/`, `offline=true`, `key_configured=false`, отдельная scratch-БД без встреч; ручной UI доступен без TLS interstitial, но облачные вызовы выключены. Рабочая БД/ключ/Polza не использовались.

На disposable Team UI run/51750 проверены status, priority, comment, due date и refresh; на run/65133 — views, filters и task creation. Оба теста были synthetic/mock и disposable; run/51750 остановлен. Владелец не смог пройти прежнее TLS предупреждение; Team UI owner acceptance, integration в основной runtime, live Vikunja/MAX/Polza, телефон и 24-hour soak остаются открытыми. Общий goal и полный план остаются IN_PROGRESS.

## Checkpoint 2026-10-08 01:35 МСК: deterministic MAX overlap fixture repair

Свежий полный backend/audit тест выявил один нестабильный отказ: `test_two_process_shared_authority_load_fault_invariants` не увидел MAX overlap при 20мс случайном окне (**5178 passed / 1 failed / 1837,52с**). Исправлена только тестовая фикстура: первый запрос каждого настоящего процесса записывает durable попытку, затем ждёт второго участника вне SQL transaction. Точечный тест:1 PASS/60,16с; весь `test_team_load.py`:8 PASS/77,12с. Повтор полного backend/audit после правки обязателен.

Для ручного синтетического UI вместо истёкшей fixture открыт новый run `t9-http-ui-da9825b9fb8e4123ac755c34d06643f9`, порт49344, до примерно02:05:34 МСК. `/fixture`, `/team/`, `/team/team.html`, `/fixture/status` проверены; ссылки открыты в Chrome. Реальный MAX/Polza, owner data и платежи не использовались.

## Checkpoint 2026-10-08 01:02 МСК: refreshed T9 handoff and current runtime boundary

После невозможности пройти локальный HTTPS interstitial без изменения trust store штатно остановлен предыдущий synthetic run; создан `HTTP_SYNTHETIC_UI_ONLY` `t9-http-ui-14949d7ba144418296777ff534cfafa1`, порт52425, TTL до примерно01:31:58 МСК. Четыре fixture routes (`/fixture`, `/team/`, `/team/team.html`, `/fixture/status`) вернули HTTP200; fixture и Team UI открыты в Chrome, ручной ввод кода/приёмка не выполнялись. Это синтетическая UI-витрина с mock Vikunja, не production Gateway/MAX/Polza.

Основной runtime на8765 отвечает на `/` и `/health` кодом200, но `/team/` и `/api/team/v1/health` возвращают404; процесс принадлежит другой worktree и не перезапускался. Свежая полная backend/audit offline suite запущена, но пока не засчитана; в этой итерации frontend processing tests 289/289, typecheck и lint прошли. Следующий R4-срез остаётся отдельной финальной проверкой acquisition inputs/review/freeze; повтор закрытой metadata-карточки запрещён.

## Checkpoint 2026-10-08 00:36 МСК: T9 HTTP handoff and T12R point-file diagnostic

После истечения предыдущего synthetic run проверен его terminal state (`stopped`, процесса нет) и поднят новый `HTTP_SYNTHETIC_UI_ONLY` run `t9-http-ui-646d3fcae28448058e141daa7942f740`, порт 53230, TTL 30 минут. Использована свежая отдельная сборка `.runtime/qa-build-20261007`; подготовка удалила ровно один MAX SDK tag и оставила 3 локальных assets. `/fixture` и `/team/` дают HTTP 200; обе страницы открыты в Chrome. В Team UI обнаружено ожидаемое отсутствие MAX Bridge; одноразовый код не вводился. Реальные аккаунты и сервисы не использовались; ручная приёмка владельца остаётся открытой.

Завершена отдельная read-only диагностика последнего файла из прежнего GCC trace: четыре frozen режима `path_open`, `exact_sha_untraced`, `exact_sha_traced`, `exact_read_traced_independent_hash` прошли на одном файле 28 062 байта; все четыре SHA совпали. Полный diagnostic elapsed — 76,17 мс; запрещённых операций, stat failures, Go, сети, SQL и provider calls — 0. [Отчёт](../../.runtime/team-rollout/r4-inputs-point-dxva2api-20261008/point-file-report.json), SHA256 `152098976b4b8cd4ca486e5a12515c3da55febdbb0c5d904d962dd64759d1428`. Результат ограничен одним сохранённым файлом и не устанавливает причину прежнего timeout. Следующий R4 шаг — отдельное frozen acquisition решение; прежняя card не повторяется.

## Checkpoint 2026-10-06 15:55 МСК: T9_LOGIN_NARROW_VIEWPORT_AND_R4_UPSTREAM_CHECK

Сохранён снимок synthetic Team login при viewport 390×844 CSS px, touch off:
видимых горизонтальных обрезаний нет. Проверялся только login; owner views,
touch и MAX WebView остаются открытыми. Повторный официальный upstream-check
не нашёл релиза новее v2.7.0 или опубликованной read-only SQLite настройки; R4
остаётся открыт до отдельного architecture ruling. Детали в
[`TEAM_BROWSER_VALIDATION_2026-10-06.md`](../../TEAM_BROWSER_VALIDATION_2026-10-06.md)
и [`TEAM_R4_NATIVE_READONLY_FEASIBILITY.md`](../../TEAM_R4_NATIVE_READONLY_FEASIBILITY.md).

## Checkpoint 2026-10-06 15:49 МСК: T9_SYNTHETIC_UI_HANDOFF_REFRESHED

После истечения fixture на 50944 поднята новая `HTTP_SYNTHETIC_UI_ONLY` fixture
на loopback:57969 (`t9-http-ui-f14dce3eabc84a3bb9a1ca82a99598e6`), проверены
HTTP 200 для `/fixture`, `/team/`, `/team/team.html`; fixture и Team UI открыты
в браузере Codex. Это даёт локальную synthetic UI-сессию без Chrome TLS
interstitial, но не закрывает ручную приёмку партнёров или готовность к
эксплуатации. Владелец не выполнял вход/кнопочные сценарии на этом handoff;
T13 manual checkbox остаётся открытым. Подробности и unverified cases —
[`TEAM_BROWSER_VALIDATION_2026-10-06.md`](../../TEAM_BROWSER_VALIDATION_2026-10-06.md).

## Checkpoint 2026-10-06 15:41 МСК: T12R_NATIVE_READONLY_CONTRACT_BLOCKER

Pinned Vikunja v2.7.0 перед SQLite соединением требует `O_RDWR|O_CREATE`, затем включает WAL. Закрытый `DETACHED_PROCESS` native attempt под NoWrite закончился Windows sharing denial до HTTP; source bytes/SQL/guards остались неизменны. Microsoft указывает, что `CREATE_NO_WINDOW` игнорируется с `DETACHED_PROCESS`; текущий native launcher уже detached, но успешный native startup не доказан. Сохранены pinned binary/guards; RW fallback, clone и allowlist expansion не применялись. Подробности и Ruling: [T12R native read-only feasibility](../../TEAM_R4_NATIVE_READONLY_FEASIBILITY.md). R4 остаётся открытым; независимые offline T9/T13 продолжаются.

## Checkpoint 2026-10-06 15:25 МСК: T9_OFFLINE_SUITE_PASS_AND_HTTP_FIXTURE

Полный backend/audit набор завершился: `tests audit/tests` — **5171 passed / 1 warning / 1948.51 s (32:28) / exit 0**. Предупреждение — устаревшая связка `httpx` с `starlette.testclient`.

Свежая `HTTP_SYNTHETIC_UI_ONLY` fixture открыта в Codex: [вход и коды](http://secretary-t9.localhost:50944/fixture), [Team UI](http://secretary-t9.localhost:50944/team/). Оба маршрута ответили HTTP 200; TTL примерно до 15:48:39 МСК. Пользователь не смог пройти Chrome TLS interstitial, настройки сертификатов не менялись. Это оставляет ручную приёмку pending и не квалифицирует TLS/MAX WebView/телефон/production integrations.

## Checkpoint 2026-10-06 13:32 МСК: T9_OWNER_HANDOFF_REFRESHED

Предыдущий временный стенд остановлен; новая clean `HTTP_SYNTHETIC_UI_ONLY`
fixture `t9-http-ui-b39fbce11b274dd2a559a2e52eb0f879` открыта во встроенном
браузере Codex на порту 52369 до примерно 14:02 МСК. Свежие коды видны только на
локальной fixture странице. Owner manual acceptance остаётся открытым.

## Checkpoint 2026-10-06 13:29 МСК: T9_SYNTHETIC_BROWSER_COMMANDS_VERIFIED

Отдельный Chrome run на `HTTP_SYNTHETIC_UI_ONLY` прошёл через подтверждение и
mock-квитанцию для смены статуса, матрицы, создания, комментария и перевода
важной задачи в «На проверке». Статус-тест, ранее `NOT_CONFIRMED`, повторён с
другим значением; дефект не воспроизведён. Automation fixture остановлен.
Чистая ручная фикстура открыта во встроенном браузере Codex на порту 61735 до примерно 13:45 МСК; коды обновлены в 13:29 и действуют 5 минут;
см. [отчёт](../../TEAM_BROWSER_VALIDATION_2026-10-06.md). Owner acceptance,
настоящие TLS/MAX/телефон/Vikunja/Polza, restart/backup/24h остаются открытыми.
Код приложения этим браузерным прогоном не менялся.

## Checkpoint 2026-10-06 00:35 МСК: HUMA_SINGLE_TRANSFER_PASS_WITH_LIMITS

Исправлена причина Huma output_invalid: итоговый worker guard использовал 180 с при штатном worker budget 1500 с. Новый AST-only набор прошёл 10/10; затем ровно одна actual Go-команда go mod download прошла parent/worker проверки. Huma v2.39.1, archive/go.mod sums и SHA-256 трёх локальных cache-артефактов совпали; worker exit 0/stderr 0 B, source/control pins неизменны, custody закрыт. [Независимый final receipt](../../../.runtime/team-rollout/r4-single-huma-budget-align-closure-20261006-94c2f2bfbd42/final-receipt.json).

Первый повторный запуск был остановлен до worker и Go из-за широкого ACL папки; его receipt сохранён без исправления. Следующая попытка использовала новую папку, созданную штатной приватной процедурой. Подтверждён только один изолированный Huma transfer: Go cache приложения не усыновлён, native Vikunja API и полный R4 не квалифицированы. Ручная UI-приёмка, production TLS, MAX/телефон и 24-часовой запуск остаются открытыми; synthetic fixture доступна в [текущем отчёте](../../TEAM_TESTING_CURRENT.md) до 01:35:45 МСК.

## Исторический checkpoint 2026-10-06: **HTTP_SYNTHETIC_BROWSER_RETEST_PARTIAL**

HTTPS fixture блокировался предупреждением локального сертификата, поэтому поднят loopback HTTP synthetic stand без certificate bypass и без изменения trust store. [Страница для ручной проверки](http://localhost:56843/team/) оставлена в Chrome до **00:23:49 MSK 06.10.2026**. В synthetic browser flow проверены обновление истории без повторного открытия карточки, срок `06.10.2026 12:30 Europe/Moscow`, классификация задачи и её переход в квадрант Эйзенхауэра, Канбан/матрица, фильтр «Все», отдельный проект с ID `9007199254741009`. Это не заменяет ручную приёмку владельцем и не квалифицирует production/TLS/MAX. См. [отчёт](../../TEAM_TESTING_CURRENT.md) и [evidence](../../../.runtime/team-rollout/t9-http-manual-retest-20261006-534a8bc60da64d7ca8243dde2762d824/browser-outcome.json).

Предыдущая закрытая Huma-попытка была отклонена по output_invalid; новая изолированная попытка устранила stale guard и прошла один transfer. Старый receipt сохранён как историческое evidence. Native API/full R4/cache adoption по-прежнему не подтверждены.

Checkpoint 2026-10-05: **CANONICAL_UI_BUGFIXES_TESTED_ONLY**. Исправлены описание задачи и обновление истории в открытой карточке; два исправления проверены в Chrome на кандидате, а перенесённый UI bundle совпал с ним. [Canonical checks](../../../.runtime/team-ui-candidate/canonical-checks.json): 128 PASS, typecheck/lint/build exit0; 3 UI файла изменены, 2 добавлены. Все прежние 310 point entries неизменны; новый набор 315 — их объединение с 5 UI путями, а не инвентаризация репозитория.

Отдельная [Huma диагностика](../../../.runtime/team-rollout/r4-single-huma-transfer-2176479bdf4e44bca808f0cc77811b4d/closed-single-huma-diagnostic.json) закрыта с `deadline` на `guards_before`, Go calls=0; процесс завершён. Две последующие проверки guards также остановлены по deadline. [Последняя проверка](../../../.runtime/team-rollout/r4-tool-input-boundaries-3986f30b41c54ab2945b055e8f6a288e/guard-phase-receipt.json) завершилась за 150,157 с: Go `verify_tree` не вернулась за 101,370 с, GCC не начиналась, Go child/network calls=0. Эти диагностики использовали worker budget 180 с; штатные worker 1500 с / inputs 360 с сохранены. Причина UNKNOWN; metadata/native/fullR4 не приняты. Далее — одна Huma transfer попытка в новой private card со штатными лимитами, одним Go command 120 с после guards PASS и полными before/after checks: **NOT_PREPARED / NOT_RUN**.

[Ручной HTTP-стенд](http://localhost:60595/team/) с синтетическими данными подтверждён только снимком доступности 05.10.2026 23:18:35 MSK; срок работы до 23:37:27 MSK. [Browser outcome](../../../.runtime/team-rollout/t9-canonical-http-fixture-00f30eafa9e24a66bbb377b7be5d90a2/root-browser-canonical-outcome.json); [инструкции и текущий отчёт](../../TEAM_TESTING_CURRENT.md). Ручная приёмка срока/ролей, TLS/native/MAX/телефон/24h остаётся открытой. Certificate bypass/trust changes не выполнялись; paid 0, Polza budget 3000 ₽/месяц, Activation/outbound OFF, goal ACTIVE. Ниже прежние checkpoints сохранены без изменений.

Checkpoint 2026-10-05: **PUBLIC_PROXY_OBSERVATIONS_COMPLETE_ONLY**. Root `741f7e`/exit0: 6 actual HTTP200, 2,6444053 с до публикации, 1 155 B прочитано; ZIP body reads=0, Go commands=0, control pins совпали до/после. [Availability report](../../../.runtime/team-rollout/r4-public-proxy-availability-bb5aa171959b4ed9a6bff5a244ea8979/availability-first.json) SHA256 `21edd3b498caafd98968c4c8de4876741b3cfd46bde16769cd4f1a2f125dfcfd`, root read `21d8c1`.

Это только наблюдение доступности public endpoints: причина прежнего timeout, эквивалентность Go TLS client/ZIP transfer, metadata/native/fullR4 не доказаны. Закрытый coarse failure остаётся неизменным; metadata retry не выполнялся.

Следующая single declared Huma Go transfer diagnostic — **SOURCE_ONLY / ACTUAL_NOT_RUN**. Два UI исправления планируются, canonical UI ещё не исправлен. Краткий [текущий отчёт](../../TEAM_TESTING_CURRENT.md) отделяет model tests, actual UI/HTTP и открытые проверки. Activation/outbound OFF, paid 0, Polza budget 3000 ₽/месяц, goal ACTIVE. Ниже сохранены предыдущие checkpoints.

Checkpoint 2026-10-05, coarse 1aeefddf: **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Единственная actual попытка `b2ef77`/session96827 → `9a0ce4`, shell70: полная before-input проверка прошла за 12,4127 с — 27 512 files/2 120 directories/1 207 312 580 B, оба manifests совпали. Выполнены 2 actual Go commands с разрешённой public module network: первый batch 32 pairs/34,984 с/exit0; второй 32 pairs/120,437 с остановлен по `deadline`, exit70, stdout/stderr 0. В worker `commands` сохранён только первый completed record; второй подтверждён отдельным run artifact. Original HANDLE/Job membership/wait/EOF и root-only Job total 3 подтверждены. Worker 229,625 с/`declared_download/deadline`, parent 229,953 с сохранил первичный отказ; source310/profile control pins unchanged. Positive custody/full after/metadata/native/fullR4 не квалифицированы.

Свежие 61 modeled PASS (33+14+14), failures/errors/skips 0, остаются модельным evidence. Root принял [independent outcome](../../../.runtime/team-rollout/r4-native-ro-dependencies-coarse-1aeefddf28294586ac37f7045118a642/outcome-failed-coarse-metadata.md), SHA256 `8b1d4c95d3a87b0c1d17ab24a6809a86518b3f823963af6a01a63cabf385ff76`, 15 232 B, на `d4ae90`. Root closure `469f67`/exit0: snapshot 4 024 files / 935 directories / 63 547 762 B, 2,3207 с; [closed-failed-coarse-metadata-card.json](../../../.runtime/team-rollout/r4-native-ro-dependencies-coarse-1aeefddf28294586ac37f7045118a642/closed-failed-coarse-metadata-card.json) SHA256 `a78fa612b6135589f2355babb0f6499cfc6d602f6c988da76eede8fadffee10d`. Карточка закрыта и immutable. Первый closure `8c2b84`/exit1/0writes сохранён; первичная деталь `NOT_CAPTURED`. Read-only point comparison зафиксировала ordinary Windows path 264 chars / WinError3, extended-length вызов для того же файла успешен; исправлены только auxiliary I/O calls. Это не причина Go deadline.

Причина module/network задержки **UNKNOWN**. Далее — отдельная bounded normal-TLS public proxy/sumdb availability диагностика для Huma и completed control; probe **NOT_RUN**, без metadata retry или повышения budgets. T9 partial 23 browser flows и 58 fixture checks (33+25) неизменны; raw HTML/internal-origin description и history-after-reopen bugs ожидают отдельного canonical UI исправления. Date/member/TLS/MAX/real phone/manual/live/24h gates открыты. Activation/outbound OFF, paid 0, Polza budget 3000 ₽/месяц, goal ACTIVE; готовность приложения не заявляется.

Ниже сохранены предыдущие checkpoints; их coarse NOT_RUN относится к состоянию до этой единственной actual попытки.

Checkpoint 2026-10-05: **HTTP_SYNTHETIC_UI_PARTIAL_VERIFIED**. Root start `52a4f0`: fixture server ready на 3332; проверены 23 synthetic browser flows, 33 adapter checks и 25 publication checks PASS. Root stop `10196b`/exit0: cleanup complete и child exit observed. [Browser outcome](../../../.runtime/team-rollout/t9-http-fixture-b15bb1b9a4c84f7aafe82d8ff65293f5/root-http-browser-outcome.json) SHA256 `490eeb7545ecc61dcb6a3161ce20116ce3344a57c876bd48721937a77695434c`. Все кнопки не объявляются PASS: описание показывает serialized HTML/internal secretary-origin marker; история подгружается после повторного открытия карточки. TLS/Secure cookie, member-role UI, positive datetime input, external due resolution, real phone/MAX/native Vikunja и full T9/full R4 **NOT_QUALIFIED**.

**CLOSED_FULL_INPUT_DIAGNOSTIC_ONLY**: без fine instrumentation исходные Go/GCC checks завершились за 36,071 с: 27 512 files / 2 120 directories / 1 207 312 580 B, оба manifests совпали. [Diagnostic report](../../../.runtime/team-rollout/r4-full-input-untraced-d7efe50245b54e90b1107a6611059e57/full-input-diagnostic-first.json) SHA256 `5324ddf082aa14214ad29ff55bd394a03988272eb9975ecfe6396a781d60b616`; [closure](../../../.runtime/team-rollout/r4-full-input-untraced-d7efe50245b54e90b1107a6611059e57/closed-full-input-diagnostic.json) SHA256 `32c812b3ff1ea15a917d2740fdbbdc0cf853c62c93c0e9d11f72882101f81c8f`. Это диагностика с uncontrolled cache/host load и concurrent synthetic UI, не доказательство причины прежнего deadline или metadata admission. Source310 unchanged, paid 0.

Новая coarse-trace карточка 1aeefddf подготовлена source-only: append-only [contract](../../contracts/team-native-read-profile-dependencies.md) SHA256 `af627f3ddca7ce4edd7ff2e24f8a42f85e1f00dd18defba6c88a4c46c4e50625`; granularity `tool_verification_only`, operation details `NOT_OBSERVED`, исходные checker guards и 360/1500 budgets сохранены. Новые reviews/tests/freeze/metadata actual ещё не подтверждены. F238 остаётся CLOSED_FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED, `Go commands=0`, policy NOT_EXERCISED. Activation/outbound OFF; Team/production/manual/live/phone/24h не квалифицированы, goal ACTIVE.

Ниже сохранены прежние checkpoints; их T9 NOT_RUN относится к состоянию до этого synthetic browser результата.

Checkpoint 2026-10-05, acquisition f238a6: **CLOSED_FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**. Root `dfdabf`/exit0 закрыл только failed byte evidence: 33 files/4 directories/598441B, 0,203 с; `closed-failed-acquisition-card.json` SHA256 `0aff9a000b46281929d909e8b19976e082668727c4ebcccb75bc137fd2837357`. Independent outcome SHA256 `3a9ccdc5138413d86a2afbe29d78dc00453eadd44fefe68b8f5ac8607f049ea6` принят root. Source310 point checks совпали; это не full after/custody qualification. Original metadata failure не изменён: worker419,047 с/preflight/deadline, partial inputs trace, actual Go0 и acquisition policy NOT_EXERCISED. Прежние 67 modeled PASS остаются отдельным evidence.

Первый root closure `4e5764`/exit1/0writes сохранён. Read-only `a7236f` показал различную семантику ctime в Windows Python 3.12: path lstat использовал birthtime, handle fstat — mtime для неизменённого bootstrap файла. Исправлена только cross-API snapshot comparison: common birthtime/dev/ino/size/mtime/type/nlink/attributes, с отдельной точной стабильностью полной path/handle информации до/после. Это repair снимка, не дополнительный runtime допуск и не изменение custody guards.

Ограниченный I/O sample `c5d25c`: 64 public small files/128 reads, 0,313 с, report SHA256 `c14fa8295eacca233025a5b7b3e5689b3f5bf122a8dedab16bbb4fd559bac5a0`, **BOUNDED_IO_SAMPLE_COMPLETE_DIAGNOSTIC_ONLY**. Serial phases суммарно0,093 с, four-worker phases0,110 с; quantized clock и warmed cache ограничивают выводы. Выигрыш четырёх потоков, причина host задержки и full-input verification не доказаны. Далее — конечная point-file диагностика frozen AST `sha/read` и trace до любых parallel changes или новой Go-попытки. Same-card retry запрещён. [Отчёт и pointers](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохраняют exact artifacts.

Private T9 b15 HTTP synthetic adapter остаётся draft в работе; его tests/server/browser **NOT_RUN**. Он не квалифицирует исходный HTTPS/security/manual gate; bypass сертификатов/trust changes не выполняются. Native build/API/fullR4/manual/live/phone/24h открыты, Team NOT_READY_FOR_MANUAL_TEST, activation/outbound OFF, paid 0, goalACTIVE.

Ниже сохранены исторические checkpoints; их pending outcome/closure относится к состоянию до этого root snapshot.

Checkpoint 2026-10-05, acquisition f238a6: **FAILED_PREFLIGHT_METADATA_NOT_QUALIFIED**, root closure ещё не выполнена. One-shot `d9c08d`/session72116 → `3380e3`, shell70: worker `FAILED_NOT_QUALIFIED/preflight/deadline`, 419,047 с, `commands=[]`. Новая acquisition policy не была фактически проверена: Go/job owner/env/cache не достигнуты. Build/test/native API/SQL/network/paid/owner/host mutations 0. Предыдущие 67 modeled PASS сохраняются отдельно от actual результата.

Before trace остановилась на бюджете 360,000 с: `inputs_returned=false`, Go `verify_tree_returned=true`, GCC false; cumulative hashed bytes 1 003 806 855 B. Open: 25 071 операций/296,782 с. Последняя записанная позиция — GCC `include/dxva2api.idl`; это позиция отказа, а не доказательство дефекта файла или host cause. Сохранена только partial trace, полная before-input проверка не прошла. Причина задержки **UNKNOWN**; antivirus/cache causality не установлена.

Parent419,359 с сохранил первичный `deadline`, original worker exit70, known HANDLE wait/signaled/Close, readers/EOF; parent timeout false. Source310 и profile control pins совпали до/после. Positive physical/held-file custody close и полные after inventories не квалифицированы. Independent outcome и failed-card root snapshot ожидаются; статус CLOSED не заявляется. Freeze выполнен до actual: initial b2c74641 → final ef3f1912, старый freeze сохранён; уточнена только stale interface prerequisite reference, exact eight-member launch map a188412a не изменился. [Exact report pins](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохранены отдельно.

Далее — закрытие failed evidence и ограниченная диагностика file I/O без автоматического повторения этой карточки. Параллельно готовится отдельный `HTTP_SYNTHETIC_UI_ONLY` adapter draft для T9; он не заменяет исходный HTTPS/security/manual gate. Обход сертификатов и trust changes не выполняются. Team NOT_READY_FOR_MANUAL_TEST; native/fullR4/T9/manual/live/phone/24h открыты, activation/outbound OFF, paid 0.

Ниже сохранены исторические checkpoints, включая прежнее LAUNCH_FREEZE_NOT_YET; актуальный actual результат указан выше.

Checkpoint 2026-10-05, acquisition f238a6: **67 modeled PASS** — worker 33, parent 14, input trace 8, acquisition policy 12; все четыре reports имеют `PURE_PASS`, failures/errors/skips 0. Это проверки моделей; actual Go/native/SQL/network 0. Текущие worker `aa47dcf8…`, parent `d92ebcad…` и contract `412daa51…` получили независимый source/design review. Gap проверки unexpected cache `*.mod` исправлен в source verifier и независимо рассмотрен; фактическая metadata acceptance этим не подтверждена. Первая focused попытка сохранила `PURE_FAIL`: 12 groups, 6 fixture errors при 0 assertion failures. Изменены только два fixture loci, worker/parent между failed и repaired probe не менялись; старый отчёт сохранён.

**ACTUAL_METADATA_NOT_RUN / LAUNCH_FREEZE_NOT_YET**. Новая actual Go-команда ещё не выполнялась; metadata NOT_QUALIFIED. Следующий шаг — завершить проверку exact final inputs и launch freeze, затем один bounded metadata запуск. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED. Build tests, native API/fullR4, T9/manual/live/phone/24h не квалифицированы; activation/outbound OFF, paid 0. [Подробный evidence](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) сохраняет SHA reports и границы review.

Ниже сохранены предыдущие checkpoints; их TESTS_NOT_RUN и pending gap относятся к прежним source revisions.

Checkpoint 2026-10-05, acquisition f238a6: **FINAL_CONTRACT_WORDING_REPAIRED_NOT_RUN**. Текущий canonical/private contract — SHA256 `412daa51e4cf401beb7f4f1b78370c4c21e10f5acfe9281cfd6ce65eaddc3362`, 35 934 B. Прежняя promotion b5dba42b сохранена как история. Уточнены границы исторической observability-проверки, effective replacement mapping, проверка acquired namespace до Go discovery и consumer membership до последующих helper source reads, область h1 для loaded/acquired files, обязательный source freeze и общее окно 600 с Go preparation для offline graph. [Policy](../../TEAM_DEPENDENCY_ACQUISITION_POLICY.md) обновлена; records `contract-policy-review-repair.json` / SHA256 `ece1110c06fdf85fe34082d78f46b20616c22c387a621f2a80c89c5d19e415ae` и `contract-policy-final-wording.json` / SHA256 `df8e696b99ffcb594722dfb48c311bc0bc460c90988e6451d4dfe1423d008718` сохраняют последовательность изменений.

Source helpers остаются **DRAFT / TESTS_NOT_RUN**, новый actual Go: 0. Независимый source reviewer нашёл gap проверки unexpected cache `*.mod`; verifier исправляется для всех соответствующих проходов, приёмка исправления ещё не выполнена. Далее нужны affected probes, независимый review и freeze перед единственным новым metadata запуском. Последний actual e4099 остаётся CLOSED_FAILED_METADATA_NOT_QUALIFIED; native build/API/fullR4/T9/manual/live/phone/24h не квалифицированы, activation/outbound OFF, paid 0.

Ниже сохранены предыдущие checkpoints; их «актуальный» статус и next шаг относятся к моменту записи.

Checkpoint 2026-10-05, T12R/R4: [MVS context e4099](../../TEAM_MVS_ERROR_CONTEXT_VALIDATION.md) — **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Worker599,094с/`mvs_graph/checksum_missing`, ordinal2 `cloud.google.com/go/compute/metadata@v0.3.0`; frozen original `go.sum` не содержит обеих h1 сумм, namespace содержит только `.info`. Runtime/test discovery не достигнута; unused module и pair прежней7925 не доказаны.76 modeled PASS (33+14+8+21), failed focused setup0 groups сохранён отдельно. Actual8 Go exit0/196 declared pairs/public network used, shell1/worker70/parent599,375с; полная inputs_before343,953с/27512files/2120dirs/1207312580B совпала. Open273,818с — наблюдение без host-cause proof; прежний423с cause UNKNOWN. Source310/profile control pins unchanged; positive physical/held8/final Job/full after proof отсутствуют. Root `243794`/session89661 → `6db592`, exit0/242,969с закрыл только failed byte snapshot23084files/5796dirs/417332426B, SHAb6e7d504d355176fe44b0ab3a102456f0f29a16faa2b1068ab943c08f256f36b; оба prior closure failures0writes сохранены, второй cause UNKNOWN.

Текущий этап **ACQUISITION_POLICY_PREPARED_NOT_RUN**: [новая политика](../../TEAM_DEPENDENCY_ACQUISITION_POLICY.md), отдельная draft f238a6 карточка; worker/parent/tests дорабатываются, новые probes/reviews/freeze/actual0. Сохранить frozen sums, разделить graph records и acquired files, проверить consumer membership до source reads и offline graph/cache invariance. Metadata/build/native API/fullR4 ещё не квалифицированы. Activation/outboundOFF, paid0, budget3000 ₽/месяц; T9browserNOT_RUN, Team **NOT_READY_FOR_MANUAL_TEST**, manual/live/phone/24h открыты, goalACTIVE.

Canonical policy promotion `01575b`/exit0 — **CANONICAL_POLICY_PROMOTED_NOT_RUN**: contract SHAb5dba42b68330868e9cb49eb8c667c4109ed1a2d2a8bc56da47e11c2bdddd0c4; promotion receipt4ca71cc55115c0ff4b53d6116670e1a79402d4a12f89b0bd2cadfdfce02da7e8, actual Go0. Это contract preparation, не новая test/metadata acceptance.

Следующий checkpoint7925 сохранён как исторический; его next шаг заменён текущим этапом выше.

Checkpoint 2026-10-05, T12R/R4: [metadata7925 с input trace](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md) — **CLOSED_FAILED_METADATA_NOT_QUALIFIED**. Worker275,750с/mvs_graph/checksum_missing; полная before-input inspection64,406с/27512files/2120dirs/1207312580B завершена.55fresh modeled PASS (33+14+8).8actual Go commands exit0 (7 download batches196pairs+mvs-list), разрешённая public dependency network использована; build/test/SQL/paid/owner/host0. Parent276,015с/70 сохранилchecksum, original HANDLE wait/EOF/Close и source310/profile control pins unchanged подтверждены. Positive physical/held8 closed receipts, final Job proof/full after checks отсутствуют; точная отвергнутая MVS pair NOT_RECORDED. Root closure1b6a12/exit0/8,172с:23103files/5796dirs/417437161B, failed snapshot SHA55b16c9fcd7cb95c34b5d5373af178dd6a93578182428d4a664c93de6be2988f; это не dependency provenance PASS. Прежний423с timeout cause UNKNOWN. Next: новая source-only failure-context diagnostic card, без checksum relaxation или old-card retry. Activation/outboundOFF, paid0, T9browserNOT_RUN, Team **NOT_READY_FOR_MANUAL_TEST**, fullR4/manual/live/phone/24h открыты, goalACTIVE.

Ниже — исторические checkpoints; содержащиеся в них NOT_RUN и next шаги относятся к моменту соответствующей записи.

Checkpoint 2026-10-05, T12R/R4: [preflight trace](../../TEAM_PREFLIGHT_TRACE_VALIDATION.md) — CLOSED_PREFLIGHT_TRACE_DIAGNOSTIC_ONLY; actual61,640с/inputs50,750с, Go/GCC full inventories match, shell0.8 collector groups +8 protocol cases +3 deadline boundaries modeled PASS; original child wait/EOF/Close и physical Python close подтверждены. Прежний423с timeout не воспроизвёлся, причина UNKNOWN. Следующая новая7925 metadata source-card добавляет bounded before/after trace; NOT_RUN. Metadata/build/init/API/fullR4/manual/live/24h открыты; T9 HTTPS warning unresolved/browser NOT_RUN; activation/outboundOFF,paid0,goalACTIVE.

Checkpoint 2026-10-05, T12R/R4: [диагностика подготовки зависимостей](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md) — actual read-only scan/hash127,735с, exit0; Go61,641с/GCC66,062с, оба inventories совпали. Independent outcome принят, CLOSED_INSPECTION_DIAGNOSTIC_ONLY; 120с недостаточно для этого замера. Failure propagation repair:8GREEN+14regression PASS, CLOSED_SOURCE_PURE_REPAIR_ONLY. Отдельная native-private metadata-карточка932476 выполнена с shared inspection360с; остальные пределы/все проверки сохранены. Новый metadata932476 FAILED/preflight/deadline423,031с/Go0 (`6d569c`→`7ebcb5`), CLOSED_FAILED только. Parent сохранил первичный deadline; knownchild wait/EOF/Close подтверждены.55fresh modeled PASS. Shared360 не устранилactual задержку; nextinstrumentedactual-context trace, не blindraise. Build/init/API ещё NOT_RUN. T9 local certificate handoff не удался, browser acceptance NOT_RUN; никакого обхода trust. FullR4/live/manual/24h открыты; activation/outboundOFF,paid0,Team NOT_READY_FOR_MANUAL_TEST,goalACTIVE.

Checkpoint 2026-10-05, T12R/R4: [dependency metadata preparation](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md) — pure33+14 PASS и quota RED→GREEN5 сохранены. Actual metadata attempt FAILED/preflight/deadline121,594с, parent shell70; original child wait/EOF/Close подтверждены, Go0. Найден shared120s toolchain inspection timeout и маскировка причины parent validator. Next: отдельный scan/hash performance diagnosis и минимальный failure propagation repair/new card. Build/init/API/fullR4/T9/live/manual/24h открыты; activation/outbound OFF, paid0, Team NOT_READY_FOR_MANUAL_TEST.

Checkpoint 2026-10-05, T12R/R4: [native read profile source preparation](../../TEAM_NATIVE_READ_PROFILE_VALIDATION.md) — CLOSED_SOURCE_PREPARATION_ONLY_PASS. Actual scope audit 5,187 с/exit0, independent outcome accepted; root v2 closure112,375 с/exit0,2311 frozenfiles. Private copy:7 additive source+1 test+2 narrow patches; current2293files/231dirs/28 516 597B. Original upstream и source310 unchanged. Первый closure отказ сохранён, причина UNKNOWN. Next: dependency worker/parent reviews+pure probes, exact public graph/Windows metadata/init closure, затем build/tests/genuine protected GET/all4. Все Go/build/test/native execution на новой карточке NOT_RUN. FullR4/T9/live/manual/24h открыты; activation/outboundOFF,paid0,Team NOT_READY_FOR_MANUAL_TEST,goalACTIVE.

Checkpoint 2026-10-05, T12R/R4: [приватный Python и CGO](../../TEAM_CGO_SMOKE_VALIDATION.md) — CLOSED_CGO_SQLITE_SMOKE_ONLY_PASS / worker136,609с / parentexit0. Fresh74 pure PASS; native gates и actual14SQL/8Readonly/1CantOpen/5Close0fail проверены, оба liveIAT snapshots совпали. Original fixture NoWrite/bytes/SQL/fullIDs и все closed custody receipts подтверждены; source310/GoGCC inventories/runtime789 unchanged. Root closure1127files, без retry. Next: source-derived native RO build, затем genuine protected GET/all4 readback. FullR4/T9/live/manual/24h открыты; activation OFF, paid0, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE.

Checkpoint2026-10-05, T12R/R4: [portable toolchain versions](../../TEAM_NATIVE_TOOLCHAIN_VALIDATION.md) actual3commands exit0, parent0,734с/worker0,328с, Go1.27.1/GCC16.2.0/windowsamd64 confirmed, exactJob1→4/Active1, closedexeNoWrite/source310 unchanged;7purePASS. FailedNO_WINDOWattempt retained/causeUNKNOWN. Parentvenvredirector vsphysicalJobowner distinguished; CGO launcher requiresoriginalphysicalHANDLE. VERSIONS_ONLY; CGO/source-derivedRO/native-auth/fullR4 remainopen. ActivationOFF, Team NOT_READY_FOR_MANUAL_TEST, paid0, goalACTIVE.


Checkpoint2026-10-05, T12R/R4: [verified native build inputs](../../TEAM_NATIVE_BUILD_INPUTS_VALIDATION.md) завершены: actual fresh exit0/527,953с/source310 unchanged; Go/GCC опубликованные SHA и Vikunja exact GitTREE c991 проверены, итоговые каталоги сверены. Private helper defects reproduced/repaired,61purePASS; прежние network/EOF failures сохранены. Только VERIFIED_INPUTS_ONLY: compiler/build/nativeRO/NoWrite/fullR4 NOT_RUN. Next: isolated toolchain versions+CGO SQLite smoke и отдельный RO native entrypoint. ActivationOFF, Team NOT_READY_FOR_MANUAL_TEST, paid0, goalACTIVE.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for sequential implementation; superpowers:subagent-driven-development may be used for isolated tasks/reviews. Steps use checkbox syntax. User/project instructions override skill suggestions to commit or deploy automatically.

**Goal:** Общие поручения трёх-десяти участников через голос в MAX, Канбан и матрицу, с подтверждением действий, напоминаниями и общим Polza budget 3 000 ₽/месяц.

**Architecture:** Secretary остаётся локальным приложением совещаний. Отдельный ограниченный Team Gateway управляет задачами через API бесплатной Vikunja, собственными inbox/outbox и MAX. Оба процесса используют общий журнал Polza; GPU для голосовых команд не нужен.

**Tech Stack:** Windows, существующие Python 3.12/FastAPI/httpx/Pydantic/SQLite и React/TypeScript; закреплённый native Windows Vikunja server; MAX HTTP API; Polza; HTTPS reverse proxy после сетевой проверки.

**Spec:** [2026-10-03-secretary-vikunja-max-design.md](../specs/2026-10-03-secretary-vikunja-max-design.md). Читать целиком один раз; на каждой задаче перечитывать соответствующий раздел.

**Status:** EXECUTING с 2026-10-03 по запросу владельца. Актуальные результаты и оставшиеся gates — в [TEAM_IMPLEMENTATION_STATUS.md](../../TEAM_IMPLEMENTATION_STATUS.md). Команды ниже задают проверки карточек; факт исполнения подтверждает журнал, а не наличие команды в плане.

Checkpoint2026-10-05, T12R/R4: [membership wire repair](../../TEAM_NATIVE_MEMBER_WIRE_VALIDATION.md) выполнен:277affectedPASS/18,44с/exit0 послеRED6FAIL/25PASS→31focusedPASS; current310. Fresh actual production nativeGET complete (identity/project/direct_users),41loopback attempts/3,344с/known cleanup/source310 unchanged. Next: отдельный source-derived read-only native observation entrypoint/build и fixed pin, сохранитьrelease/catalogue, strict original-source bytes/SQL/journals guards. FullR4/owner nonce/finaldecision/commonpredicate/head/R5–R6/T9/manual/live/24h обязательны; activationOFF, paid0, goalACTIVE.

Предыдущий checkpoint: Checkpoint2026-10-05, T12R/R4: [first-failure trace](../../TEAM_NATIVE_FIRST_FAILURE_VALIDATION.md) выполнен15,046с/exit0/source309 unchanged:3 actualGET200, validation native_membership_invalid, последующий custody refusal/WALSHM отдельный. Next: genuine member shape→meaningful API contract repair, отдельно native read-only path preserving strict R4goal. FullR4/owner nonce/finaldecision/commonpredicate/head/R5–R6/T9/manual/live/24h обязательны; activation OFF, paid0, goal ACTIVE.

Предыдущий checkpoint: Checkpoint2026-10-05, T12R/R4: [fresh actual native measurement](../../TEAM_MANAGED_NATIVE_ACTUAL_VALIDATION.md) завершён15,641с/exit0 выполнения; observer **MEASURED_NOT_QUALIFIED**. Child/listener/cleanup подтверждены, actualGET null, native mainSHA/WAL/SHM changes сохранены; остальные3 DB unchanged, final full collector refused. Current309 unchanged. Следующий шаг — bounded диагностика первого body exception и startup SQLite compatibility без удаления журналов/ослабления guards. FullR4/owner nonce/finaldecision/commonpredicate/head/R5–R6/T9/manual/live/24h обязательны; activation OFF, paid0, goal ACTIVE.

Предыдущий checkpoint2026-10-05, T12R/R4: [Windows launch cwd](../../TEAM_NATIVE_LAUNCH_DIRECTORY_VALIDATION.md) исправлен:162 affected PASS/23,32с/exit0, review accepted; current309 (307 unchanged+1 changed+1 new относительно308). Actualseed36 local API requests/cleanup успешен, следующий worker70 сохранён, exact-cwd Python probe WinError267/all4 DB unchanged после того сбоя. После repair потребовался новый fresh native measurement.

Предыдущий checkpoint2026-10-05, T12R/R4: полный [managed native coordinator](../../TEAM_MANAGED_NATIVE_VALIDATION.md) реализован offline:522 affected PASS/144,00с/exit0,128 новых+394 существующих. Continuous custody → purpose journal → originalHANDLE/Job/GET → exactstop → freshall-fourreadback → closedterminal; confirmed defects fixed and independent reviews accepted. Прежние301 unchanged+7 NEW/current308 на закрытии этой карточки.

## Global Constraints

- CWD: `D:\AI\Projects\Active\Secretary`; исходная опора `f85e1ff3e47fad42dd531d3a0ade4c478814e797`. Проверять актуальный Git status, сохранять чужие изменения.
- Модель исполнения: **`gpt-6.1-sol`**, `reasoning_effort=high`. Для T2/T4/T6/T7 и финального review предпочтителен `xhigh`, если доступен в настройках. Это рекомендуемые доступные режимы; не выдумывать размер контекста, temperature или API-параметры модели.
- Одна законченная карточка T за итерацию: контракт → значимый failing test → минимальное изменение → affected tests → review → evidence. Не запускать все карточки одной огромной генерацией.
- Независимому помощнику отдавать read-only review или непересекающиеся файлы с фиксированным контрактом. Не поручать одновременно двум агентам api.py, database.py, polza.py или общий frontend hook.
- Каждый новый task/command имеет immutable payload, UUID operation_id, actor, scope, ожидаемую revision/ETag; неопределённый внешний результат не является разрешением на повтор mutation.
- Бесплатная Vikunja, без Pro. Polza обязателен; общий budget 3 000 ₽/месяц, reset 1-го в 01:00 Europe/Moscow. Бот STT/LLM без локального GPU. Старые тарифные ceilings автоматически не возвращать.
- Секреты/встречи/образцы/DB/runtime не включать в Git, prompts, screenshots и отчёты. Логи содержат коды и IDs, не полные payloads/транскрипты.
- Native MAX voice и ручная точность Secretary пока NOT_QUALIFIED. Offline PASS не даёт live/production PASS.
- Никаких автоматических stage/commit/push, публикаций, покупок, установки Docker/WSL, смены VPN/питания/системного trust store. Skill `writing-plans` предлагает frequent commits; применяются более строгие пользовательские AGENTS.md — Git только по отдельному запросу.
- При отсутствии ключа/MAX/HTTPS выполнять все доступные offline-задачи. Останавливать только зависимый live/deploy шаг, с конкретным недостающим условием.

## Review Focus

1. Повторная версия итогов/подтверждения и timeout после принятого POST не создают дубль и не переписывают исполненную задачу — T3/T4/T5.
2. MAX account/проект/кнопка устарели либо отозваны: ни чтения чужих данных, ни платного STT — T6/T7/T9.
3. Одновременные расходы двух процессов и reset в 01:00 МСК не теряют резерв и не считают квитанцию дважды — T2/T8.
4. Старая кнопка/просроченное напоминание после переноса срока, закрытия или восстановления backup не меняет новую задачу — T4/T10/T13.
5. Windows logout/restart меняет рабочую директорию/учётную запись: DB paths остаются абсолютными, capture/DPAPI не переходят под SYSTEM — T0/T12/T13.

## Порядок и результаты

| Этап | Карточки | Результат |
|---|---|---|
| Основание | T0–T1 | Baseline, сетевые неизвестные, закреплённые внешние контракты |
| Данные и бюджет | T2–T5 | Общий месячный cap, канонические задачи, публикация без дублей |
| MAX и голос | T6–T8 | Изолированный вход, подтверждаемые команды, Polza без GPU |
| Работа команды | T9–T11 | Канбан/матрица, уведомления, безопасный HTTPS package |
| Эксплуатация | T12–T13 | Автозапуск, backup/restore, ручной и 24h пилот |

Критический путь: T0 → T1 → T2/T3 → T4 → T5/T6 → T7 → T8/T9/T10 → T11 → T12 → T13. Независимо можно делать T2 и T3 разными исполнителями; изменения общего PolzaClient только у владельца T2, затем T8 использует его контракт.

## Обозначения файлов и контрактов

Пути `frontend/`, `tests/`, `scripts/`, `config/`, `docs/` указаны относительно CWD. Сокращённые backend-пути `domain/`, `application/`, `infrastructure/`, `interface/`, `orchestration/`, `api.py` и `settings.py` всегда относятся к **`backend/secretary/`**, а не к корню репозитория. Например, `domain/team.py` означает `backend/secretary/domain/team.py`. Имена новых файлов — **планируемые**, их наличие не предполагается. Существующие точки интеграции: `backend/secretary/api.py`, `settings.py`, `application/worker.py`, `application/assignments.py`, `infrastructure/database.py`, `infrastructure/assignment_repository.py`, `infrastructure/polza.py`, `domain/assignment_evidence.py`, `frontend/src/components/AssignmentReviewPanel.tsx`.

Новые семейства:

- `domain/team.py`, `domain/task_delivery.py`, `domain/cloud_budget.py` — DTO и ports; без HTTP/React.
- `application/team_tasks.py`, `task_delivery.py`, `monthly_budget.py`, `bot_commands.py`, `voice_commands.py`, `reminders.py` — бизнес-сценарии.
- `infrastructure/team_database.py`, `team_repository.py`, `task_delivery_repository.py`, `budget_repository.py`, `polza_account.py`, `vikunja.py`, `max_bot.py`, `max_media.py` — SQL/HTTP.
- `interface/team_gateway.py`, `team_auth.py`, `local_team_routes.py`; `orchestration/team_worker.py` — отдельная поверхность и worker. Создать package `__init__.py` по необходимости.
- `frontend/team.html`, `frontend/src/team/TeamApp.tsx`, `api.ts`, `useTeamTasks.ts`, `TaskBoard.tsx`, `TaskMatrix.tsx`, `TaskDetail.tsx`, `Today.tsx`, `CommandPreview.tsx`; отдельный generated `team-api.d.ts`.
- `scripts/team/` — setup, doctor, start/stop/supervisor, scheduled tasks, backup/restore. Secret configs в игнорируемом `.runtime/team` и `.env.team`, примеры в `config/team`.

Shared DTO в T3: `TeamMember`, `TaskOrigin`, `TaskSnapshot`, `TaskCommand`, `CommandReceipt`, `PublishPreview`, `PublishCommand`; в T2: `CloudCharge`, `BudgetSnapshot`, `AccountUsage`. Полные обязательные поля описаны spec §§4–7 и уточняются тестами на T2/T3. Не вводить одинаково названные несовместимые модели в соседних задачах.

Обязательная `CommandReceipt`: `{acceptance_receipt, execution_state, current?}`. Acceptance неизменяема: `{operation_id, payload_hash, decision: accepted|rejected, decided_at, error_code?}`. Execution изменяется: `{state: queued|running|reconciling|applied|conflict|uncertain|rejected, revision, task_id?, verified_at?, error_code?, retry_after?}`. API 202 означает очередь, а не исполнение. Повтор operation_id + того же payload возвращает исходную acceptance и текущее execution_state; другой payload с тем же ID — 409. Прикладной статус читается отдельным GET и не требует повторной mutation. Current snapshot может быть новее завершившейся операции.

## T0. Baseline, изоляция проверок и предварительная диагностика

**Files:** создать `scripts/run_offline_tests.py`, `scripts/team/check_prerequisites.ps1`, `docs/TEAM_IMPLEMENTATION_STATUS.md`; тест `tests/test_team_preflight.py`. Существующие `scripts/check_lifecycle.py`, `start.ps1`, `.env` в baseline не запускать/не менять.

**Interfaces:** `check_prerequisites.ps1 -OutputPath <ignored path>` → JSON только capabilities/unknowns; `run_offline_tests.py <pytest args>` → exit code pytest с запрещённым внешним HTTP, production DB и live lifecycle.

- [x] Зафиксировать status/HEAD, версии локальных зависимостей, существующие пути/схемы по коду и документированную опору 848 backend/131 frontend; не объявлять эти числа свежим прогоном.
- [x] Написать `test_preflight_is_read_only_and_reports_unknown_ingress` и `test_offline_runner_rejects_external_http_and_production_data`: никакого bootstrap, процесса capture, записи в рабочие DB или произвольного сетевого GET в pytest.
- [x] Выполнить тесты до реализации, затем реализовать ограниченный runner/doctor; определить архитектуру Windows, свободные порты, доступные runtime, текущую учётную запись/DPAPI контекст, отдельный диск/место для backup.
- [x] Сетевой отчёт: домен, статический WAN IP, CGNAT, права на NAT, внешний порт 443 и TLS = verified/unknown, без изменения маршрутов. При неизвестном статусе список точных проверок владельца, а не вывод «ПК недоступен» по отсутствующему домену.
- [x] Запустить изолированный baseline: `.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q`; frontend `npm.cmd run test:processing`, `npm.cmd run typecheck`, `npm.cmd run lint`. Результаты/exit codes в `.runtime/team-rollout/`; точные файлы и свежие 923/131 результатов в журнале.
- [x] Review: исходные данные/процессы сохранены; known baseline failures классифицированы до интеграции. Существующие ошибки, не вызванные задачей, не маскировать и не чинить без оценки влияния.

## T1. Заморозить внешние контракты и Windows-поставку

**Files:** создать `config/team/integration-manifest.json`, `config/team/vikunja.yaml.example`, `docs/TEAM_EXTERNAL_CONTRACTS.md`, `docs/contracts/vikunja-v2.openapi.json`, `scripts/team/setup_vikunja.ps1`; тест `tests/test_team_external_contracts.py`.

**Interfaces:** manifest `{version, architecture, source_url, sha256, signature_check, api_version, observed_at}`; конфигурация возвращает абсолютные пути данных и localhost URL. Точные remote endpoints фиксируются из установленного OpenAPI, не из названий методов плана.

- [x] Тест `test_contract_handles_v2_items_problem_json_and_empty_204`, `test_setup_rejects_hash_mismatch_and_never_overwrites_live_install`.
- [x] Проверить v2.7.0 Windows server artifact и источник подписи/checksum. Скачать и распаковать только в изолированную project-local runtime папку при исполнении, не менять PATH; не путать server ZIP с desktop client.
- [x] Поднять синтетический Vikunja на свободном loopback порту, отдельной БД и fixture users. Прочитать `/api/v2/openapi.json`, подтвердить FREE creation/users/project permissions/bot user/token/labels/buckets/ETag. Не сохранять секретные пример-ответы.
- [x] Зафиксировать необходимые MAX types, URL/headers, initData validation и webhook contract по официальным docs; проверить Python httpx trust к API без токена/отправки личных данных. Не добавлять CA глобально и не использовать verify=false.
- [x] `scripts/run_offline_tests.py tests/test_team_external_contracts.py -q` → PASS; реальный localhost contract smoke отдельно маркировать LOCAL_INTEGRATION. Схемы/лицензии/provenance доступны следующему исполнителю.

## T2. Единый месячный ledger Polza

**Files:** создать `domain/cloud_budget.py`, `application/monthly_budget.py`, `infrastructure/budget_repository.py`, `infrastructure/polza_account.py`; изменить `settings.py`, `infrastructure/polza.py`, `application/worker.py`, при необходимости `scripts/benchmark.py`, `scripts/benchmark_speakers.py`; тесты `tests/test_monthly_budget.py`, `tests/test_cloud_budget_boundary.py`.

**Interfaces:** `MonthlyBudget.reserve(charge: CloudCharge) -> Reservation`; `settle(operation_id, receipt) -> BudgetSnapshot`; `mark_uncertain(operation_id)`; `snapshot(now) -> BudgetSnapshot`. `PolzaAccountClient.read_key_usage() -> AccountUsage`. `CloudCharge` содержит stable per-HTTP-attempt operation_id, category, period, request hash, estimate/reserve, optional meeting/command IDs.

- [x] Failing tests: `test_two_processes_cannot_reserve_more_than_shared_remaining`, `test_period_rolls_at_0100_moscow`, `test_unknown_reservation_survives_rollover`, `test_opening_usage_and_receipts_are_not_double_counted`, `test_disabled_legacy_limits_cannot_bypass_monthly_guard`.
- [x] Реализовать миграции новой billing DB, атомарные reservations, integer micro-RUB и provider opening balance/snapshot freshness. Approved Polza budget = 3 000 ₽ минус согласованные регулярные внешние расходы (по умолчанию 0); effective budget = min(approved budget, provider monthly cap). Меньший месячный лимит допустим и явно отображается, null/слишком высокий cap/иной период требует настройки, нулевой остаток — паузы расходов. Пороги уведомлений 50/80/90% effective budget (при 3 000 ₽ — 1500/2400/2700). Лимит провайдера автоматически не менять.
- [x] Подключить guard к **каждому оплачиваемому HTTP POST** PolzaClient, включая summary map/reduce/repair и benchmark. Принимать зависимость budget в client factory; production и CLI не могут создать незакрытый обходной client. Mock transport в unit tests остаётся тестовой зависимостью.
- [x] Сохранять existing usage API совместимым; новую сумму месяца считать по новому журналу и opening provider usage, не по сумме дублирующихся старых callback records. Account GET и polling уже принятого async job не резервируют новую платную операцию.
- [x] `test_invalid_paid_result_is_still_charged`, `test_missing_usage_is_not_zero`, `test_timeout_never_replays_paid_post`, `test_late_receipt_settles_once`, `test_key_rotation_does_not_reset_month_spend`, `test_worker_and_benchmark_share_guard`.
- [x] Прогон: `scripts/run_offline_tests.py tests/test_monthly_budget.py tests/test_cloud_budget_boundary.py tests/test_cost_controls.py tests/test_polza.py tests/test_polza_merge.py tests/test_stt_cap_resume.py tests/test_summary_rate_retry.py tests/test_script_limits_regression.py -q` → PASS. Независимый review: нет пропущенного оплачиваемого call path и возвращённых старых max_price.

## T3. Командная модель и durable storage

**Files:** создать `domain/team.py`, `domain/task_delivery.py`, `infrastructure/team_database.py`, `infrastructure/team_repository.py`; тест `tests/test_team_repository.py`.

**Interfaces:** `TeamRepository.accept_command(actor, command) -> CommandReceipt`; `claim_command(worker_id) -> ClaimedCommand | None`; `record_remote_result(...)`; `resolve_member(max_user_id) -> TeamMember | None`. Канонический task state хранит Vikunja; здесь mappings/receipts/projection/outbox.

- [x] Failing tests: `test_operation_replay_returns_original_receipt`, `test_same_operation_different_payload_conflicts`, `test_claim_is_atomic_across_processes`, `test_revoked_member_cannot_apply_queued_command`.
- [x] Реализовать миграции, schema version, UUID mappings, payload hash, scoped revisions, inbox/outbox, command journal, leases и uncertain recovery. MAX/Vikunja IDs в собственных HTTP DTO передавать десятичными строками; UUID — строками, revisions — проверенными неотрицательными целыми. Не преобразовывать внешние int64 IDs в JavaScript Number. Адаптеры сохраняют точный тип провайдера.
- [x] Зафиксировать состояния задачи/команды/доставки и переходы из spec. Изменения прав проверять повторно при применении. Установка срока и классификация — отдельные подтверждённые значения; null и false различаются.
- [x] `scripts/run_offline_tests.py tests/test_team_repository.py -q` → PASS; создать/восстановить копию fixture DB и повторно выполнить миграции без изменения данных.

## T4. Vikunja adapter и исполнитель задач

**Files:** создать `infrastructure/vikunja.py`, `application/team_tasks.py`, `orchestration/team_worker.py`; тесты `tests/test_vikunja_adapter.py`, `tests/test_team_tasks.py`. Durable regression review добавляет `tests/test_team_worker.py`, `tests/test_team_task_recovery.py`; owned synthetic runtime probe — `scripts/team/probe_vikunja_tasks.py`.

**Interfaces:** `VikunjaClient.get_task(id) -> TaskSnapshot`, `create_task(draft, marker)`, `apply_task_change(id, patch, expected_etag)`, `find_origin(project_id, marker)`, `list_project_tasks(cursor)`; `TeamTaskService.execute(claimed_command) -> CommandReceipt`. DTO не возвращает provider credentials.

- [x] Тесты HTTP boundaries: POST201, DELETE204, problem+json422/403, pagination, GET ETag/304, timeout после create, duplicate marker, read error, malformed success response. T1 доказал отсутствие remote write CAS в v2.7.0 (stale If-Match PUT/PATCH →200); 412 проверять как возможный отказ, не предполагать его обязательным.
- [x] Создать labels собственным bot user и mappings bucket IDs по закреплённому FREE контракту; не полагаться на custom fields/audit Pro. Чтение API identity проверяет returned task/project ID.
- [x] Реализовать durable per-task lease/fence в Gateway, свежий remote snapshot fingerprint и expected Gateway revision перед partial PATCH, затем GET после записи. If-Match не использовать как единственную защиту. Проверить conflict двух MAX/UI команд; штатные внешние concurrent native writes не разрешать, их best-effort обнаружение не равно CAS. Несколько шагов bucket/done/labels имеют сохранённый progress, uncertain и reconciliation; поля вне команды не затирать.
- [x] `test_accepted_create_lost_response_reconciles_without_duplicate`, `test_zero_search_does_not_authorize_retry`, `test_simultaneous_max_and_board_edit_conflicts`, `test_partial_move_not_reported_applied`, `test_external_edit_does_not_fabricate_actor_history`.
- [x] Прогон двух новых файлов → PASS; localhost FREE smoke: создать, прочитать, перенести, классифицировать, назначить, закрыть synthetic task в отдельном проекте. Удалять только собственные fixture IDs. Обновить contract evidence без текста реальных встреч.

T4 уточнения по фактическому runtime: trusted token receipt для `tk_` вместо недоступного GET `/user`; actual User.id вместо project-user relation.id; done coupling выключен во всех Kanban views выделенного проекта; пустой no-change PATCH304 требует GET; literal HTML защищает markers. Final evidence: 298 affected offline PASS и 22 native checks, включая 10 service receipts и 20 дополнительных create/move. Два ранних native HTTP500 сохранены как cause UNKNOWN для T12/T13, без replay. Подробности — `docs/TEAM_TASK_EXECUTION.md` и status.

## T5. Подтверждённая публикация из Secretary

**Files (реализованные boundaries T5):** `application/{publication_evidence,publication_commands,task_publications,publication_delivery}.py`, `infrastructure/{task_publication_repository,publication_bridge}.py`, `interface/internal_publications.py`, `orchestration/publication_worker.py`; изменены `api.py`, `infrastructure/database.py`, source/directory methods и `domain/{team,task_delivery}.py`; frontend `TaskPublicationPanel`/client/helpers и wiring в `MeetingResults`; отдельные `test_publication_*`, `test_task_publication_*`, `test_task_publications`, `test_task_delivery_contracts` и frontend publication tests. Source validation, formatter, storage и transport имеют отдельные проверяемые границы; прежний AssignmentReviewPanel не получает side effect публикации.

**Interfaces:** `preview_publish(scope, action_ids, target_project) -> PublishPreview`; `confirm_publish(command: PublishCommand) -> DeliveryReceipt`; `PublishCommand` = operation_id + exact source scope + selected action IDs + preview hash + watermarks + explicit resolved members/dates. Новый local API: `POST /api/v1/meetings/{id}/task-publications/preview`, `POST .../task-publications`, `GET .../task-publications`.

Pre-implementation contract ruling после T4: batch `PublishCommand.operation_id` отделён от stable per-item publication UUID. Каждый выбранный action получает один UUID по source key `(meeting_id,transcript_version,summary_version,action_id,destination_project_id)`; assignment revision в identity не входит. Нельзя использовать один origin marker для нескольких задач. Batch receipt содержит per-item acceptance/execution/task ID. В одной Secretary DB transaction читаются `_summary/_view/current_context/sources`, сравниваются assignment/roster/attribution revisions и `current_context_hash`, пишутся batch/items/outbox. Existing `summary_publications` не переиспользуется: это paid job→summary history. Перед публикацией fresh evidence/anchor/source hash проверяются даже для manual confirmed assignment. Source participant IDs и display names не являются TeamMember UUID. Неоднозначный due phrase отличается от явно подтверждённого «Без срока»; явный timezone сохраняется.

- [x] Тесты: confirmed assignment не публикуется сам; manual assignment с негодным evidence требует review; cross-meeting source запрещён; «к пятнице» требует разрешённой даты; guest без mapping не назначается по имени.
- [x] Одна transaction: прочитать snapshot, CAS watermarks, записать publication + outbox. Перед отправкой показывать текст/кому/куда/срок/цитату и что уйдёт команде. Только выбранные пункты.
- [x] Loopback `POST /internal/v1/task-publications` принимает отдельный service secret и дедуплицирует per-item delivery operation UUID; normal creation delivery ID равен publication UUID, proposal имеет отдельный semantic ID. `GET /internal/v1/task-publications/{operation_id}` возвращает acceptance/execution. После lost ACK только GET. Internal app отдельно от публичного сервиса; local admin команды через bridge недоступны. Public ingress block fixture проверяется при подключении Gateway T6.
- [x] `test_lost_ack_then_status_poll_reaches_applied_without_new_mutation`: immutable acceptance остаётся прежней, applied возникает только после remote read и содержит task_id; queued никогда не отображается как «Опубликовано».
- [x] `test_assignment_revision_does_not_create_new_remote_task`, `test_new_summary_requires_supersedes_decision`, `test_late_attribution_change_creates_correction_not_overwrite`, `test_double_click_and_lost_ack_keep_single_publication` и эквивалентные UI/HTTP regressions.
- [x] Новые/adjacent backend/audit **707 PASS**, frontend **161 PASS**, typecheck/lint/build и canonical OpenAPI/types → exit 0. T5 COMPLETE_OFFLINE_CONTRACT; runtime/native/live/manual proof не заявлен. Evidence в TEAM_IMPLEMENTATION_STATUS.md.

## T6. Изолированный Gateway, пользователи и вход

**Files:** создать `interface/team_gateway.py`, `interface/team_auth.py`, `config/team/.env.team.example` (исключение из ignore добавить только для этого пустого примера); тесты `tests/test_team_auth.py`, `tests/test_team_api.py`; изменить local routes только для owner invitations.

**Interfaces:** `create_team_app(settings, repository, clients) -> FastAPI`; `require_actor(request) -> Actor`. Routes: `POST /api/team/v1/session/max`, `POST /api/team/v1/session/code`, `DELETE /api/team/v1/session`, `GET /api/team/v1/me`, `GET /api/team/v1/tasks`, `GET /api/team/v1/tasks/{id}`, `POST /api/team/v1/commands`, `GET /api/team/v1/commands/{operation_id}`. Local owner API создаёт invitation, привязывает пользователя, отзывает доступ.

- [x] Тесты MAX signature, duplicate query keys, auth_date старше 300 секунд, future skew больше 30 секунд, replay initData с переставленными/иначе закодированными параметрами, code TTL 5 минут, 5 попыток на неизменный challenge, смена кода без сброса счётчика, invitation TTL 24 часа, revoked user/session, CSRF/Origin и cross-project IDs.
- [x] Реализовать server-side identity verification + opaque sessions, права owner/member и журнал. MAX user_id берётся только из проверенного контекста; поля actor от клиента игнорируются/отклоняются.
- [x] Gateway не импортирует capture/enrollment и не отдаёт local secrets/audio/DB paths. Отдельные DTO и OpenAPI `docs/contracts/team.openapi.json`; cookie поведение для mini-app тестируется отдельно на T13.
- [x] `test_public_gateway_has_no_local_admin_routes`, `test_member_cannot_assign_others_or_raise_budget`, `test_every_write_requires_scope_and_operation_id` + новый auth/API suite → PASS. Negative routes `/config`, `/session`, raw audio и internal ingest проверены на public ASGI ingress fixture; реальный HTTPS proxy — T11.
- [x] Public ingress fixture исключает `/internal/v1/task-publications` POST/GET из T5. Реальный peer нельзя получать из forwarding headers; запуск uvicorn с `proxy_headers=False`. Внешний HTTPS-адрес владельцу неизвестен: проверка домена, IP/CGNAT и доступа с мобильной сети предусмотрена до включения внешнего маршрута. Evidence733 affectedPASS + final64PASS, independent review closed; T6 COMPLETE_OFFLINE_CONTRACT, runtime/live gates впереди.

## T7. MAX transport, inbox и текстовые команды

**Files:** создать `infrastructure/max_bot.py`, `application/bot_commands.py`; расширить Gateway/worker; тесты `tests/test_max_transport.py`, `tests/test_max_inbox.py`, `tests/test_bot_commands.py`.

**Interfaces:** `MaxClient.send_message(...) -> SendReceipt`, `read_subscription()`, `register_own_subscription(...)`; `BotCommandService.handle(event) -> CommandPreview | DeterministicReply`; `POST /hooks/max` принимает raw body + webhook secret.

- [x] Проверить secret до parsing/dispatch; durable inbox до HTTP 200. Повтор одного message/callback получает 200, но не повторяет обработку. При недоступной БД — 503. Unknown sender — без media download/Polza.
- [x] Реализовать «Мои задачи», «Сегодня», «Просрочено», «Открыть доску», «Открыть матрицу», «Вход на компьютере», кнопки «Принять», «В работе», «Готово», «Помощь», «Предложить срок». Кнопка привязана к actor/scope/revision/action/expiry, не содержит секретов или изменяемого доверенного текста. Desktop-код выдаётся только привязанному участнику по T6/spec §6.
- [x] Free text создаёт proposal; распознавание intent относится к T8. Встроенные команды и кнопки не вызывают AI. Сообщения `queued/applied/uncertain/conflict` различимы.
- [x] `test_webhook_ack_precedes_slow_provider_work`, `test_callback_replay_is_single_effect`, `test_missing_message_is_quarantined_without_charge`, `test_subscription_recovery_touches_only_owned_subscription`, `test_429_and_ambiguous_send_have_distinct_retry_policy` → PASS. Пять namedcases дают13 параметризованныхPASS; affected suite1 099PASS, independentreview closed, T7 COMPLETE_OFFLINE_CONTRACT. Evidence: TEAM_IMPLEMENTATION_STATUS.md.
- [x] Реальный probe после готового HTTPS/бота: text, callback, native voice и отдельно audio file; пока инфраструктуры нет — сохранить NOT_TESTED с инструкцией владельцу, продолжить остальные offline карточки. Инструкция `docs/TEAM_MAX_BOT.md`; реальный probe/WebView/телефоны/HTTPS NOT_TESTED, не заменяется offline proof. Следующая карточкаT8.

## T8. Короткий голос через Polza и подтверждение intent

**Files:** создать `infrastructure/max_media.py`, `application/voice_commands.py`; расширить PolzaClient отдельным `parse_task_intent`, MAX command flow и billing integration; тесты `tests/test_voice_commands.py`, `tests/test_max_media.py`, `tests/test_voice_budget.py`.

**Interfaces:** `download_voice(event) -> ValidatedAudio`, `transcribe_voice(command_id, audio) -> Transcript`, `parse_task_intent(command_id, text, allowed_context) -> IntentProposal`; существующий budget T2 вызывается на каждый POST. `confirm_intent(actor, proposal_id, expected_revision, operation_id) -> CommandReceipt` использует T4, без повторного AI.

- [x] Проверить скрытие signed URL в логах, SSRF/redirect/DNS/private IP, фактические ограничения 10 MiB/120 секунд, неверный codec, subprocess timeout, слишком большой JSON Polza, отмену и очистку временных файлов. Media56PASS, внутри final1 765affectedPASS; realCDN/nativevoiceNOT_QUALIFIED.
- [x] Реализовать CPU normalization и JSON STT; model default Turbo, ru, json. Новый intent model default gpt-4.1-mini, внутренний предел ответа 1 024 токена, strict schema, максимум 5 предложений, максимум 1 repair. Имя HTTP-параметра ограничения токенов и поддержку structured output сверить по контракту выбранного маршрута. Ни tools, ни произвольного кода/API от модели. `max_tokens`+`json_schema`, public docs/catalog pinned в `docs/contracts/polza-intent-contract.json`; live capability/price не квалифицированы.
- [x] Сохранить checkpoint STT до intent; crash/retry не транскрибирует повторно. Повтор подтверждения не повторяет LLM. Account sender связывает автора; voiceprint не используется для login. Originalcontext/HTTP200/fences/atomicconfirm также проверены; ambiguouspaidPOST не повторяется.
- [x] `test_negation_and_reported_speech_do_not_create_task`, `test_ambiguous_person_or_date_requires_review`, `test_friday_preview_uses_event_timezone_not_server_date`, `test_prompt_injection_cannot_select_foreign_project`, `test_exhausted_budget_keeps_text_buttons_working`, `test_paid_timeout_remains_uncertain` → PASS. Все6names/13parametersPASS, `t8-plan-acceptance-green.txt`; finalaffected1 765PASS128.44s.
- [x] Подготовить synthetic/owner-approved benchmark ≥30 коротких команд с ожидаемыми именами, датами и отрицаниями. Live запуск — только на этапе исполнения с настроенным общим cap; первый тестовый пакет ≤100 ₽ внутри общего бюджета 3 000 ₽. Это предел конкретной проверки, не новая квота на встречу. Сохранить стоимость каждого вызова и качество; нативный voice MAX проверяется отдельно от модели STT. Подготовлены48syntheticcases/60fixturechecks, `PREPARED_NOT_RUN_LIVE`; реальные стоимость/качество/nativevoice остаются T12/T13, без mock подмены.

## T9. Team UI: сегодня, Канбан и матрица

**Files:** новые `frontend/team.html`, `frontend/src/team/*` из карты файлов, `frontend/src/generated/team-api.d.ts`; изменить Vite build inputs без замены текущего Secretary entry; тесты `frontend/tests/team-workflow.test.mjs`, `frontend/tests/team-auth.test.mjs`.

**Interfaces:** `TeamApi` реализует только публичный Team Gateway API. T9 уточняет обнаруженный пробел T6: добавить scoped GET `members`, `tasks/{id}/history`, `status` и отдельную read-only сверку проекта с durable временем полного успешного обхода. Справочник назначения owner-only, внешние автор/время nullable, неизвестный бюджет не равен нулю; чтение статуса не обращается к Polza. `useTeamTasks` хранит scoped snapshots, operation IDs и selection epochs. Внутренние Vikunja/Polza tokens никогда не приходят в браузер. Матрица и доска используют один TaskSnapshot.

- [x] Написать тесты одного task ID в двух views, статуса отдельно от важности/срочности, пустой классификации, конфликтов, late response при смене проекта, double submit и истёкшей сессии.
- [x] Реализовать «Сегодня», Канбан, матрицу 2×2, карточку/историю, preview команды. Desktop drag-and-drop дополнить доступным меню перемещения для клавиатуры и телефона; без обязательной новой DnD-зависимости. Реализация и model/controller tests завершены; реальная проверка кнопок в браузере остаётся следующей строкой.
- [x] В матрице показать «Не разобрано», задачу без срока и фильтр «Мои/Все». При изменении квадранта не менять bucket; при переносе колонки не менять labels. История отражает команды системы и отдельно внешние изменения без ложного автора.
- [x] Отображать pending/sync delay/conflict/uncertain, причины budget pause и последний успешный sync. Не оптимистически показывать applied до квитанции; сохранять новый пользовательский draft во время запроса.
- [x] `npm.cmd run test:processing` → 289 passed / 0 failed; `typecheck`, `lint`, `build` → exit 0. Только предупреждение Vite `[PLUGIN_TIMINGS]`: `vite:build-html` занял около 90% времени сборки.
- [x] Synthetic desktop browser smoke на текущей сборке: вход, выбор проекта/синхронизация, «Сегодня» / Канбан / Матрица, фильтр «Мои/Все», создание задачи, комментарий, отдельная смена статуса и важность/срочность; применённые mock-команды показаны как `Применено и проверено`.
- [x] Узкая синтетическая UI-проверка на `390×844` и `320×740` (`mobile=true`, `touch=true`): Today/Kanban/Matrix, отсутствие overflow у документа, внутренний горизонтальный scroll Kanban, status preview/confirm и одинаковое состояние задачи в трёх представлениях.
- [ ] Ручная приёмка владельца и реальный touch/телефон/MAX WebView — T13; эмуляция не заменяет устройство.

**Gate T9:** desktop и narrow browser-smoke пройдены только на disposable `HTTP_SYNTHETIC_UI_ONLY` с mock Vikunja; latest narrow run `t9-http-ui-7a60f9a1ac30485f908d67b808784f1e`/58995 остановлен после подтверждённой synthetic status-команды. Текущий owner handoff `t9-http-ui-28ea7b3c61d74643b58eea793ddcafbb` на порту64322 открыт до10:56:53 МСК; подробные шаги в [testing report](../../TEAM_TESTING_CURRENT.md). TLS, реальный MAX SDK/WebView/телефон/touch, owner manual acceptance, live Vikunja/Polza, уведомления и 24h остаются открытыми. Эмуляция не заменяет устройство, HTTP fixture не является production qualification. T9 и весь goal не считать принятыми. Внешнее изменение срока имеет отдельный owner-confirmed resolution gate T12 ниже.

## T10. Напоминания, контроль сроков и доставка

**Files:** создать `domain/notifications.py`, `application/reminders.py`, `application/notification_sender.py`, `infrastructure/notification_repository.py`, `orchestration/notification_worker.py`; schema7 TeamDatabase. Owner dashboard: безопасные DTO/repository/GET и отдельный frontend component. Тесты `tests/test_team_reminders.py`, `tests/test_notification_outbox.py`, sender/worker/cross-boundary/dashboard; сохранить legacy Bot/Voice receipt compatibility.

**Interfaces:** `ReminderScheduler.plan(now, canonical_snapshots) -> list[NotificationIntent]`; `NotificationSender.dispatch(intent, *, authorize, before_send, cancelled=None) -> SendReceipt`. Обязательные fenced callbacks обеспечивают актуальные ACL/задачи и атомарное сохранение точного `BotSend` до POST; intent text — только preview. Идемпотентность по spec §8. Clock внедряется зависимостью. `GET /api/team/v1/dashboard?project_id=...` — только текущему владельцу проекта, без секретов, raw payloads и выдуманных метрик.

- [x] Failing tests: напоминания за 24 часа и за час, сводка 09:00, тихие часы 21:00–09:00, границы дня/месяца, отсутствие и изменение срока, закрытие перед отправкой, отзыв получателя, смена исполнителя и повтор после рестарта.
- [x] Реализовать durable scheduler, canonical recheck перед отправкой, отмену старых due revisions, coalescing после downtime, bounded retries и отдельный uncertain send. Проверки task API бесплатны и не вызывают LLM.
- [x] `test_restart_sends_one_current_digest_not_backlog`, `test_uncertain_send_does_not_loop`, `test_provider_success_does_not_claim_human_read`, `test_missing_polza_key_does_not_stop_reminders`, `test_task_staleness_defers_wrong_notification` → PASS.
- [x] Owner dashboard показывает ожидающие/uncertain команды, последнее подтверждённое принятие MAX API, состояние/неизвестность webhook и месячный budget; проблемы не скрываются только в логах. Отсутствующие sources/counts/timestamps не подменяются нулями или зелёным health.

**Gate T10:** COMPLETE_OFFLINE_CONTRACT: 1 159 affected backend и 234 frontend PASS; typecheck/lint/build PASS, independent review closed. Только synthetic data/MockTransport, рабочие scheduler/polling/ports подключаются в T12. Реальные MAX receipts, телефоны, webhook callback/HTTPS и ручная приёмка этим не квалифицированы; T9 browser gate остаётся открытым.

## T11. Безопасный пакет HTTPS и конфигурация MAX

**Files:** создать `config/team/Caddyfile.example`, `scripts/team/check_ingress.ps1`, `docs/TEAM_MAX_SETUP.md`, `docs/TEAM_NETWORK_OPTIONS.md`; тесты `tests/test_team_proxy_contract.py`.

**Interfaces:** `check_ingress.ps1 -PublicUrl <url> -ReadOnly` → capability report без изменений DNS/router/firewall. Deployment manifest содержит только точные разрешённые routes/ports и локальные paths.

- [x] Подготовить конфигурацию proxy:443 → gateway:8766 и отказ всем прочим paths, включая encoded/traversal варианты, `/internal`, localhost API и прямую БД. Принимать forwarded headers только от loopback proxy; проверить Host, ограничения размера, частоты и времени запросов.
- [x] Документировать конкретные действия владельца: выбрать домен и подтвердить IP, настроить HTTPS, верифицировать ИП, создать бота и пройти модерацию, ввести MAX token локально, пригласить трёх участников, установить месячный лимит Polza 3 000 ₽.
- [x] Сначала выполнить все локальные тесты конфигурации; затем показать точные изменения инфраструктуры. Только после разрешённого подключения активировать webhook и публичные routes. Нет публичного IP/HTTPS — network BLOCKED, остальные offline этапы продолжаются.
- [x] `tests/test_team_proxy_contract.py` → PASS; внешняя TLS/certificate chain/443 проба и callback roundtrip → отдельный LIVE evidence. Polling режим маркируется DEV_ONLY. LIVE пробы этой карточкой не выполнены.

**Gate T11:** COMPLETE_OFFLINE_PACKAGE:499 affected backend/234 frontend PASS,72 native HTTP checks, fresh Team-only package/native config validation и independent review closed. Подготовленные diff/owner instructions в `TEAM_MAX_SETUP.md`, `TEAM_NETWORK_OPTIONS.md`, `TEAM_PUBLIC_ASSETS.md`; отчёт `TEAM_INGRESS_VALIDATION.md`. Network BLOCKED_CONFIGURATION_REQUIRED, внешний HTTPS/MAX/phone NOT_TESTED; публичная активация выключена. Продолжать независимый offline T12, сохраняя T9 browser и T13 live/manual gates.

## T12. Жизненный цикл Windows и восстановление

**Files:** создать `scripts/team/start.ps1`, `stop.ps1`, `doctor.ps1`, `supervisor.py`, `install_tasks.ps1`, `uninstall_tasks.ps1`, `backup.ps1`, `restore.ps1`; `docs/TEAM_WINDOWS_RUNBOOK.md`; тесты `tests/test_team_lifecycle.py`, `tests/test_team_backup.py`.

**Interfaces:** start/stop работают только с PID+creation time+launcher ownership; doctor выводит по компонентам healthy/degraded/not_configured, не один зелёный status. Backup manifest фиксирует schema/config/runtime versions и hashes, restore всегда сначала в новый путь с outbound disabled.

- [x] Проверить занятые чужие порты/PID reuse, абсолютные пути БД при другой cwd, незавершённую запись, restart backoff, аварийное закрытие БД, корректное завершение и отсутствие самопроизвольного capture.
- [x] Реализовать project-local компоненты и restart supervision: одна foreground boot-задача Supervisor управляет тремя фиксированными gateway/Vikunja/proxy под подходящей непривилегированной учётной записью; отдельная capture/logon-задача запускает исходный Secretary при входе его пользователя. Уточнение T12 сохраняет единые ownership, порядок запуска и maintenance pause; у каждого native entry point собственный process-owned Job Object. Не делать `sc.exe create` с неподтверждённым service binary.
- [x] Подключить T9 read ports и full-project polling каждые 30 с. Дополнительный completion+30s cadence defect исправлен: monotonic ticks без overlap, пропуск missed ticks; 111 affected PASS / historical cadence full 3 497 PASS / independent review closed. Native 500 задач в одном isolated project: cold 6,719 с / warm 8,907 с; это не production SLA. Закрыть выявленную границу внешнего неподтверждённого срока: owner-only immutable resolution preview связывает observation_id, текущую baseline revision и observed fingerprint; владелец явно выбирает дату/«Без срока» и причину. Acceptance и worker повторно проверяют ACL и fresh remote fingerprint, durable preimage/steps, applied только после final GET. Обычный set_due с устаревшим fingerprint не подменять обходом `_fresh`; scope/late-change/double-confirm/unknown-write тесты обязательны. До этого gate UI показывает degraded и не обещает принятие такого изменения.
- [x] Реализовать общий maintenance barrier по spec §9: остановить новый intake/отправки, завершить либо отметить uncertain активные операции, приостановить workers и изменения всех четырёх БД, затем снять набор с одним barrier_id. При активном capture отложить backup; не прерывать запись. Восстановить БД в fixture workspace, выполнить migrations и сверить links/receipts; старые outbox не отправляются до reconciliation.
- [x] `test_backup_barrier_prevents_publication_and_charge_between_snapshots`, `test_partial_backup_manifest_is_not_restorable`, `test_active_capture_defers_backup_without_interrupting_audio`, `test_restored_outbox_cannot_charge_before_reconciliation` → PASS.
- [x] Подготовить install/uninstall/rollback инструкции. Применение scheduled tasks, power/router/CA настроек требует отдельного конкретного шага владельца; секреты не передаются командной строкой или в лог.
- [x] `tests/test_team_lifecycle.py tests/test_team_backup.py` → PASS. Live restart/logout/reboot проверять с владельцем в согласованное окно, не перезагружать ПК автоматически.

**Gate T12:** COMPLETE_OFFLINE_PACKAGE. Исторические 754 affected backend / 257 frontend / 93 packager PASS, 9 native Job / 4 supervisor / 84 Caddy HTTP checks сохранены. Дополнительный monotonic cadence repair: 111 affected PASS / historical cadence full 3 497 PASS, independent review closed. Native 500 задач в одном isolated project: cold 6,719 с / warm 8,907 с; 30s grid корректен, пропуск overrun не обещает production SLA. Отчёт `TEAM_T12_VALIDATION.md`, exact selectors/evidence в status. Terminal history restart/recovery regression исправлен и проверен; actual Scheduler/credentials/hardware/logout/reboot/MAX/Polza/HTTPS не квалифицированы. Сохранять T9 browser и live/manual/24h gates.

## T12R. Ввод восстановленной копии в эксплуатацию

Это незавершённая часть operational recovery из spec §9/§10 и T13, сверх уже проверенного safe offline copy T12. Контракт: `docs/contracts/team-restore-reconciliation.md`. **Gate: IN_PROGRESS; ACTIVATION_NOT_IMPLEMENTED.** Не выдавать весь offline код за завершённый до R3–R6.

- [x] R0: обычный Secretary startup и нижний Database слой удерживают restored guard; диагностическое чтение не запускает migrations/recovery/old work.
- [x] R1: `reconciliation-preview` проверяет exact archive/restore/четыре sources/assets, выдаёт только hashes/counts и сохраняет guards/liabilities/outbound block. Нет `--apply`, env/default owner paths, провайдеров или запуска процессов. 108 affected tests PASS, затем 174 final affected после test-only Windows URI containment repair. Первый новый full 10 FAIL / 3532 PASS сохранён; final full 3554 PASS, 619,83 с, exit 0, 33 selected hashes без drift. Отчёт `TEAM_RESTORE_VALIDATION.md`, актуальный full в `TEAM_VALIDATION.md`.
- [x] R2: durable immutable quarantine legacy work/lineage/auth при сохранении completed receipts/Billing holds. Каталог105tables, typed collector/JSON graph, только декларативный preparer/readback для будущего R3, без activation. 122 affected / 3676 full PASS, 646,25 с, exit0; 255 selected hashes unchanged; bounded review closed. `docs/TEAM_R2_VALIDATION.md`.
- [x] R3: fresh independent local operator approval, per-authority preparations и last complete decision; partial/crash blocked. 464 affected PASS, 153.28 с; 3839 full backend/audit PASS, 683.84 с, exit 0; 265 selected hashes unchanged; bounded source review finding RED→GREEN. `docs/TEAM_R3_VALIDATION.md`. Activation остаётся OFF.
- [ ] R4 IN_PROGRESS: actual four-source preparations/readbacks и native prepared hold реализованы ([история source](../../TEAM_R4_SOURCE_PREPARATION.md), [native](../../TEAM_R4_NATIVE_HOLD_VALIDATION.md)). Добавлены retained source collector, fixed GET-only Polza/MAX issuer, independent observation journal и отдельный DPAPI business-owner purpose consent. Initial618 affected PASS; initial full19FAILED/4697PASS/9ERROR, единственный test-order canonical-reload conflict воспроизведён и исправлен test-only; ordered3PASS/9.41с. Final391 affected PASS/396.32с и4725 full PASS/1242.78с/exit0; все295 final pins unchanged:286 прежних/1 existing test-only repair/8 новых. [Текущий отчёт](../../TEAM_R4_FRESH_EVIDENCE.md). R3/V1/history/liabilities сохранены, activation/outbound OFF. `record_blocked_activation_decision` атомарно фиксирует current owner nonce и blocked decision, но production activation grant/final decision/runtime caller ещё не реализованы; native authenticated GET/advancing head, общий predicate и fresh-work/auth lineage обязательны. Новый control DB/flags не обходят guard.
- Дополнительная native диагностика R4: три historical preserved FAIL до HTTP; independent readback24/24 подтвердил all4 unchanged. [Observer](../../TEAM_JOB_OBSERVER_VALIDATION.md):79 affected PASS и actual own-child diagnostic PASS. Четвёртая [native observation](../../TEAM_NATIVE_OBSERVATION_VALIDATION.md): FAIL `unknown_owned_job_descendant` / exit1 /2,203с сохранён; collector DIAGNOSTIC_COMPLETE14records/3lifetimes/0,093с, дополнительный current-path image `conhost.exe`, terminal exit0. All4 bytes/FileID/schema/42tables/390triggers/head0/events0/grants0 и current297 inputs unchanged. Отдельный ACL preflight/native0 не считается native attempt. Authenticated GET/actual NoWrite startup denial NOT_PROVEN; ancestry/mapped/source qualification отсутствуют. `CREATE_NO_WINDOW` уже установлен. 2026-10-11 проверены production launch options и offline `tests/test_restore_native_process.py` (**47 passed** через `scripts/run_offline_tests.py`); тест подменяет `Popen`, поэтому подтверждает только настройки вызова `DETACHED_PROCESS`, но не фактические Console API вызовы pinned binary или ancestry `conhost.exe`. Console dependency остаётся открытой; нового native launch, allowlist expansion и RW fallback не было. Full4725 исторический. [Диагностика](../../TEAM_DIAGNOSTICS_2026-10-04.md).
- Дополнение 2026-10-11: статически прочитан pinned Vikunja PE с совпавшим SHA-256 и `PE subsystem=3`; прямые imports `AllocConsole`, `FreeConsole`, `AttachConsole`, `GetConsoleWindow` не найдены, но `GetProcAddress` импортирован, а строки `WriteConsoleW`/`GetStdHandle` присутствуют. Это сужает статическое свидетельство, не исключает динамический console lookup и не закрывает console dependency или `conhost.exe` ancestry. Подробности и ограничения — в [R4 feasibility report](../../TEAM_R4_NATIVE_READONLY_FEASIBILITY.md). Native binary не запускался.
- [ ] R5: `PARTIAL_OFFLINE` — исторические holds блокируются до нового поколения; backup отклоняется по root-only `restore.json` и `restore.partial.json`, проверка повторяется после freeze/write-lock, crash до первой DB-копии блокируется, restore ставит новый hold во все четыре authorities. Предыдущий полный backend/audit suite — **5 380 PASS**; новый Windows subprocess regression на blocked `SourcePreparationControl` binding — **133 targeted PASS** за 7,33 с: процессный exit после commit сохраняет candidate SHA/fence и blocked state; exit до commit оставляет rollback-журнал, который reader не принимает и не исправляет (`.runtime/team-rollout/t12r-r5-process-crash-20261012.txt`). Это не проверка power-loss/disk failure и не доказывает successor maintenance DB/readback или transfer fence. Непрерывность fence через реальную restore/reconciliation, durable successor ledger и полный process/power-loss recovery ещё не доказаны.
- [ ] R6: `PARTIAL_OFFLINE` — полный прогон шести synthetic-модулей **260 PASS / 0 FAIL** (checkpoint 2026-10-12 00:20), плюс fixture HTTP tests 7 PASS и frontend 300 PASS; отдельная actual owner/provider reconciliation и real restore в 24h пилоте не выполнены.

Следующая карточка — R4: единый проверяемый activation predicate на startup/native/SQL/recovery/final dispatch, exact source identities, текущие owner/provider evidence и защита от нового control DB/flags. R3 COMPLETE_OFFLINE: real collector/controlled SQL/OS leases/durable readback/last decision; guards/liabilities/legacy no-replay сохранены. Нет global cross-file atomic commit. [TEAM_R3_VALIDATION.md](../../TEAM_R3_VALIDATION.md). R5 generation и R6 actual/manual/24h остаются обязательными; raw MAX/subscription cutover OFF. Общий goal активен. Polza budget3000 ₽/месяц сохраняется; внешний HTTPS неизвестен и проверяется до owner/MAX пилота.

Checkpoint 2026-10-06, T12R/R4 startup slice: Vikunja-only restore guard теперь блокирует TeamRuntime и owner-side Secretary до создания control DB; общий preflight fail-closed проверяет все четыре authorities. Финальная affected выборка startup/probe/lifecycle/billing/final-dispatch — **190 PASS/42,59с/exit0**. Первый backend/audit запуск дал 5169 PASS/2 FAIL из-за старых gateway probe fixtures без четвёртого Vikunja path. После их test-only исправления полный suite повторён: **5171 PASS/1 warning/1948,51с/exit0**. Отчёт [TEAM_R4_RUNTIME_GUARD_VALIDATION.md](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md). Это не закрывает общий predicate, native authenticated GET/advancing head, final owner/provider decision, lineage, R5–R6 или manual/live gates; activation/outbound остаются OFF.

Дополнение 2026-10-11, T12R/R4 guard-integrity: восстановленный источник теперь остаётся заблокированным, если строка `maintenance_restore_guard` отсутствует, но сохранился restore-specific `backup_restore_no_*` trigger; штатная пустая Billing-таблица с `billing_restore_*` triggers не меняет поведение. Полный affected набор восьми модулей — **218 passed / 1 warning / 75.06с / exit0**. Первый полный запуск поймал только time-sensitive тестовый срок `int(time.time())+301`; изолированный повтор прошёл, границу в двух test fixtures расширили до `+600`, production expiry logic не менялась. Финальный полный suite — **5235 passed / 0 failed / 1 warning / 1542.99с / exit0**. Подробности и границы — [TEAM_R4_RUNTIME_GUARD_VALIDATION.md](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md). Общий R4 predicate, native GET/head, owner/provider decision, lineage и R5–R6 остаются открытыми; activation/outbound OFF.

Checkpoint 2026-10-11, T12R/R4 fixed runtime restore context: добавлен read-only resolver, который принимает только настройки из штатного loader, сверяет config и четыре source paths с restored `restore.json`, выводит archive/runtime paths из fixed R3 inventory и вызывает существующий collector для полной повторной проверки. Resolver не является activation authority и всегда возвращает activation/outbound OFF. RED отсутствующий модуль:3 expected FAIL; отдельный mutated-config-outside-project regression сначала поймал преждевременный file handle и прошёл после исправления scope-check order. Финальные проверки: `test_restore_activation_context.py` **27 PASS/36.54с/exit0**, `test_team_runtime.py` **50 PASS/7.10с/exit0**, `git diff --check` exit0; evidence в `.runtime/team-rollout/t12r-runtime-{context,settings}-20261011.txt`. Следующий шаг — контракт общего fail-closed R4 predicate и карта runtime sinks; native GET/head, owner/provider decision, fresh lineage, R5–R6, manual/live остаются открытыми.

Дополнительная проверка final resolver/callers: `scripts/run_offline_tests.py` по шести config/startup/native/lifecycle/maintenance модулям — **198 PASS / 1 upstream warning / 84.25с / exit0**, evidence `.runtime/team-rollout/t12r-runtime-context-callers-20261011.txt`. Счёт пересекается с отдельными27/50 targeted runs и не суммируется.

Checkpoint 2026-10-11, T12R/R4 offline egress continuation: владелец подтвердил только offline работу; 319 module names/versions с candidate SHA `f236804a4ed805f2bfdb68173db19d515bcd2c3d159aa00e3a1bc206ae8af966` не отправлялись внешним сервисам. Изолированный deny-all guard запрещает `online=True`, требует полный Go offline environment до process/Job inspection и запускает parent-ом только worker из той же isolated candidate-карточки. Worker/parent RED→GREEN regressions — **7/7**; frozen source hashes `69a1aef1…99266f4` и `e9a8842d…094e12` без изменений. Go/helper/proxy/network/SQL/native API/source initialization не запускались. Кандидат **SOURCE-ONLY / NOT LAUNCHABLE / NOT QUALIFIED**: полный launch freeze и независимое post-repair review отсутствуют. R4/activation остаются INCOMPLETE/OFF; public metadata требует отдельного решения владельца и будущего reviewed exact path/version allowlist. Полный checkpoint: [TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md).

Checkpoint 2026-10-11, T12R/R4 common predicate contract: текущие startup/SQL/recovery/claim/reserve/dispatch/native seams сверены с исходниками и описаны в [контракте сверки](../../contracts/team-restore-reconciliation.md) и [R4 runtime evidence](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md). Найдено: marker/admission/billing guards действуют локально, но общий predicate не имеет runtime callers; decision ledger создаёт только `activation_blocked`. Зафиксированы operation-bound recheck и acceptance tests. Cross-process lease/transaction primitive не выбран, поэтому это design-only checkpoint: production code не менялся, activation/outbound OFF; следующий шаг — сравнить existing maintenance admission/fence с требуемым transaction-bound lease и проверить race windows прежде чем подключать consumers.

Дополнение к R4 common predicate review: существующий `MaintenanceRepository` может дренировать runtime через durable tickets и `begin_barrier`/`freeze`, но tickets пока не bound к proof digest/source generation; standalone `api.create_app` и часть workers могут обходить owner wrapper. Поэтому эта подсистема — только lifecycle interlock, не permission. Следующий offline code card — сначала RED на unbound/stale/missing R4 ticket, затем versioned ticket binding и полное покрытие startup/SQL/recovery/claims/reservations/final dispatch; сохранять existing guards до общего commit/readback. Без соответствующего доказательства активация остаётся OFF.

Проверка текущих base fences перед следующим code card: offline runner для `test_maintenance_executor.py`, `test_team_paid_maintenance.py`, `test_team_secretary_maintenance.py`, `test_team_runtime.py` — **97 passed / 1 existing Starlette/httpx warning / 24.75с / exit0**. Это проверяет существующий drain/admission/no-replay путь, но не новый общий predicate; production code в этом checkpoint не менялся.

Checkpoint 2026-10-11, T12R/R4 owner admission audit: штатные workers запускаются только через `TeamRuntime._loop()`/`admitted_operation`; owner HTTP удерживает admission через `AdmittedASGI`/`MaintenanceMiddleware`; SQL sources повторно проверяются при подключениях; Polza имеет final four-authority check до submit. Production direct `run_once()` caller вне owner composition не обнаружен, поэтому отдельный runtime bug не подтверждён. Связанные offline suites — **334 PASS / 0 FAIL / 1 existing warning / 434.29s / exit0**; точный список в [R4 validation](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md). R4 остаётся INCOMPLETE: maintenance tickets не связаны с proof/source generation, общий predicate и required native/owner/provider/fresh-lineage evidence отсутствуют. Code changes не делались; activation/outbound OFF.

Checkpoint 2026-10-12, T12R/R4-R5 offline ticket/fence trace: `test_restore_source_epoch.py` + `test_restore_source_epoch_writer.py` — **134 passed / 0 failed / 128.70s / exit0**. Source-epoch result remains `all_prepared_blocked`, all four restore guards remain active, and both activation/outbound flags remain false. Source trace confirms durable maintenance tickets bind deployment/participant/run/kind/operation/current fence but not R4 proof/source generation; final dispatch admission is not atomic with external POST. Backup manifest records `barrier_fence`, while the prepared epoch/new control path has no high-water transfer to the next generation. No production code changed; explicit production guard-clear search found no update/delete/reset path. Keep R4 `CONTRACT DESIGN ONLY`, R5 `PARTIAL_OFFLINE`; next offline card is a contract-first versioned proof-bound ticket with synthetic RED for missing/stale/unbound evidence and lock-order/race acceptance before consumer wiring. Full backend/audit remains the prior **5 283 PASS** run; no full rerun was needed without production edits. See [restore/reconciliation contract](../../contracts/team-restore-reconciliation.md).

Checkpoint 2026-10-12, T12R/R4 offline binding proposal: `R4AdmissionBindingV1` is specified in the restore contract, with no production implementation. It binds a fixed R3/R4 source generation and final decision digest to one action/participant/run/operation and maintenance fence; unbound, stale, replayed or partial-source state blocks, while synthetic fixtures cannot grant activation. Cross-file activation remains blocked until a fixed-ledger verifier can validate all four immutable source grants and current owner/provider/native evidence. Next code card: add synthetic RED cases for absent/stale/mismatched bindings and barrier interleavings; keep activation/outbound OFF. See [R4 runtime guard validation](../../TEAM_R4_RUNTIME_GUARD_VALIDATION.md).

Checkpoint 2026-10-12, T12R/R4 operation-ID audit: focused offline maintenance/runtime batch **110 passed / 0 failed / 1 existing warning / 24.43s / exit 0**. Source trace found that `MaintenanceTicket` carries an operation ID but `assert_dispatch_admission()` does not compare it with the side effect's expected ID; common owner worker-loop tickets are created without one, while MAX/Vikunja final seams do not enforce command-to-ticket equality. Vikunja also accepts `intake` at the mutation seam. No production code changed. Next bounded card is RED for missing/mismatched IDs and zero HTTP/native effects, then operation-specific owner tickets across each worker and barrier interleavings. This remains lifecycle hardening, not R4 permission: proof/source-generation binding, fixed-ledger evaluator, native/owner/provider evidence, and cross-process interlock are still absent; activation/outbound OFF.

Checkpoint 2026-10-12, T9 offline browser smoke: реальные клики по трём scenario-кнопкам изолированного HTTP fixture теперь завершаются `POST 200` после перевода только synthetic form handlers на same-origin `fetch`; строгая проверка `Origin` не менялась, и регрессия для `Origin: null` зафиксирована тестом. Два затронутых backend UI fixture набора — **29 PASS / 0 FAIL / 64,26 с**. Новый временный стенд: `http://localhost:61127/fixture` и `/team/`, `HTTP_SYNTHETIC_UI_ONLY`, TTL до 02:37 МСК. Team UI проверен только до авторизации; код не использован, owner receipt/manual authenticated actions, public TLS/MAX WebView и production integrations не подтверждены. См. [текущее тестирование](../../TEAM_TESTING_CURRENT.md).

Checkpoint 2026-10-12, T13 local smoke handoff: добавлена отдельная процедура в [TEAM_MANUAL_ACCEPTANCE.md](../../TEAM_MANUAL_ACCEPTANCE.md#локальный-synthetic-smoke-до-owner-пилота): точный запуск из проверенной изолированной frontend сборки, две localhost вкладки, одноразовый synthetic code вводится только локально, подтверждается только mock receipt, остановка по точному `run_root`. Она закрывает подготовку локального браузерного smoke, но не закрывает owner deployment, MAX/Polza/Vikunja, телефон, 24h или T13 manual acceptance.

Checkpoint 2026-10-12, T12R/R4 offline egress guard follow-up: pure regression сначала воспроизвёл четыре обхода предыдущей subset-only policy (`GOFLAGS`, `GOCACHEPROG`, `HTTPS_PROXY`, lowercase `goproxy`); изолированный candidate теперь требует точный ключевой набор child environment, canonical uppercase keys, string values и пустой `GOFLAGS`. Candidate pure suite — **8/8 PASS**; source-only local review, без отдельного independent/formal security review. Frozen sources не менялись, Go/helper launch и сеть не запускались, 319 module paths не отправлялись. R4 всё ещё INCOMPLETE, candidate **NOT LAUNCHABLE / NOT QUALIFIED**, activation/outbound OFF; полные launch receipts и independent post-repair review остаются обязательными. Текущие pins и ограничения: [offline dependency validation](../../TEAM_NATIVE_READ_DEPENDENCIES_VALIDATION.md).

## T13. Сквозная приёмка и передача в эксплуатацию

**Files:** создать `tests/test_team_end_to_end.py`, `docs/TEAM_MANUAL_ACCEPTANCE.md`, `docs/TEAM_VALIDATION.md`; scripts для synthetic fault tests только в отдельном workspace.

- [x] Offline E2E: preview источника → публикация → Vikunja → квитанция MAX; тестовый голос → Polza mock → preview → подтверждение; перенос по доске → та же карточка в матрице; изменение срока → отмена старого напоминания; сбой после принятия внешним API → отсутствие дубля.
- [x] Нагрузка для 10 пользователей и 2 процессов: 1 000 синтетических задач с pagination, 50 конкурентных mutations, повторные webhook, 429/5xx/timeout. Критерии: 0 утечек между проектами, 0 потерянных принятых команд, 0 дублей, 0 незамеченных перезаписей и обходов бюджета. SQLite busy errors либо обработаны повтором безопасной операции, либо явно проваливают проверку.
- [x] Исторические полные backend/audit tests R0/R1: 3554 PASS, 619,83 с, exit 0; 33 selected hashes без drift; 174 affected PASS. Frozen 263 frontend / typecheck/lint/build PASS сохранены, frontend не затронут, bundle hashes rechecked. Final frozen artifact helper 52 checks / 23 files / 0 hits / exit 0; отдельно 27 card reports: 2 assessed fake URI hits, 0 unassessed. Lockfiles/licenses/env/bundle gaps и historical 3497/3482 results сохранены. R2 final full: 3676 PASS, 646,25 с, exit0; 122 affected, 255 selected hashes unchanged. Future R3–R5 production edits требуют нового соответствующего прогона.
- [x] Подготовлены приложение, ссылки и пошаговая ручная инструкция в `docs/TEAM_MANUAL_ACCEPTANCE.md` и `docs/TEAM_TESTING_CURRENT.md`. Владелец вручную подтвердил только отображение карточек проекта 7 в synthetic UI; точный run/порт не указан. Это частичная ручная проверка списка, не `MANUAL_PASS`, не проверка действий и не READY_FOR_PRODUCTION. Preview/confirm/receipt, реальный пилот, внешние сервисы, телефон и 24-часовое наблюдение остаются открытыми.
- [ ] Реальный пилот: каждый из трёх партнёров входит из своего MAX, принимает и закрывает задачу. Павел диктует, Алексей меняет колонку, Дмитрий — квадрант. Проверить неоднозначные имена и сроки, одну общую карточку, уведомление на заблокированном телефоне, отдельно native voice и audio file.
- [ ] Live Polza: первый пакет ≤100 ₽ внутри общего бюджета 3 000 ₽, со сверкой квитанций и аккаунта. Исчерпание лимита и смену месяца тестировать на mock-данных; реальный лимит провайдера проверять GET /key и настройкой без искусственного расходования 3 000 ₽.
- [ ] После ручной приёмки — 24 часа наблюдения с контролируемым сетевым разрывом/restart, проверкой webhook subscription, outbox, бюджета и backup/restore. Зафиксировать реальные задержки и ограничения; короткая успешная проба не доказывает работу 24/7.
- [x] Независимый review уже реализованных контрактов, безопасности и billing; устранить подтверждённые дефекты и повторить затронутые проверки. Исторические Voice/bootstrap/cadence/native/R0–R3/source reviews сохранены. Текущий bounded review новых owner/source/provider/store компонентов закрыт до initial full; final test-order repair отдельно reviewed, final391 affected/4725 full PASS; final green artifact closure проводится отдельно. Full R4 review ещё не выполнен; activation OFF. Владелец получает инструкции запуска, остановки, backup, обновления, rollback и статусы по spec §10.

**Gate: RESTORE_IMPLEMENTATION_IN_PROGRESS.** R0–R3 COMPLETE_OFFLINE, R4 IN_PROGRESS. Историческая source/provider/owner карточка: initial618 affected PASS, initial full19FAILED/4697PASS/9ERROR; test-only isolation repair RED→ordered GREEN3PASS; final391 affected PASS/396.32с и4725 full PASS/1242.78с/exit0, её final295 pins unchanged. После T9-only shutdown repair отдельный observer добавил два NEW файла: current297 named inputs, прежние295 unchanged;79 affected PASS и actual own-child diagnostic PASS. Последующая [native observation](../../TEAM_NATIVE_OBSERVATION_VALIDATION.md) определила дополнительный current-path image как conhost.exe; FAIL сохранён, all4 DB/current297 unchanged. [Observer](../../TEAM_JOB_OBSERVER_VALIDATION.md), [R4](../../TEAM_R4_FRESH_EVIDENCE.md). Console dependency qualification и native authenticated GET/final owner-provider grant/nonce/decision/advancing watermark/common predicate/fresh lineage обязательны, activation OFF. Frontend263 — исторический. Team NOT_READY_FOR_MANUAL_TEST. Затем R5–R6 и T9 browser/live/manual/24h. Goal ACTIVE; Polza3000 ₽/месяц сохраняется. HTTPS пока неизвестен; проверить по owner runbook до внешнего пилота.

Исторический статус до R3: R2 COMPLETE_OFFLINE: 122 affected / 3676 historical full PASS, 646,25 с, exit0; 255 selected hashes unchanged. R3 next, activation unavailable. Историческая R0/R1 приёмка: 174 final affected / 3554 full PASS, 619,83 с, exit 0; 33 selected hashes без drift. Final artifact helper 52 checks / 23 files / 0 hits / exit 0; 27 новых named reports: 2 assessed fake URI hits, 0 unassessed. Исторический cadence full: 3 497 PASS за 535,76 с, exit 0, 21 selected SHA без drift. Frozen 263 frontend / typecheck/lint/build PASS. Cadence/native review closed; normal cleanup 3600 с сохранён. Настоящий pinned FREE Vikunja: native scan 500 задач за 6,719/8,907 с, 1 017 GET/scan, один isolated project; production/two-process SLA не квалифицирована. Исторический artifact audit: 52 checks / 23 files / 0 strong hits / exit 0; 13 env values пустые. Dependency/license/CDN gaps сохранены; full formal/CVE/legal qualification не заявляется. Billing/Voice/schema9/manifest/bootstrap repairs сохранены; first-owner CLI работает только с новой DB. Restore activation R3–R6 обязательна и ещё не реализована/не выполнена; контракт `docs/contracts/team-restore-reconciliation.md`. Отчёты `TEAM_VALIDATION.md`, `TEAM_RESTORE_VALIDATION.md`, инструкции `TEAM_MANUAL_ACCEPTANCE.md`. READY_FOR_MANUAL_TEST, live MAX/Polza, телефоны и 24h открыты: внешний HTTPS, реальные accounts/configuration неизвестны. Общий goal активен.

## Общие команды будущей проверки

Checkpoint 2026-10-05, T12R/R4: отдельный [native file custody](../../TEAM_NATIVE_FILE_CUSTODY_VALIDATION.md) завершён:85 affected PASS/1,66с/exit0,60 новых+25 существующих. NoWrite→NoDelete→NoWrite overlap/same-HANDLE hash/journals refusal;8-case rootRED закрыт минимальными до-чтения identity/errorcode/fixedflags repairs. Прежние299 inputs unchanged+2 NEW/current301. Full managed coordinator/actual native GET/closedSourceEvidence/owner nonce/finaldecision/commonpredicate/advancinghead/lineage, R5–R6 и T9/live/manual/24h обязательны; activation OFF, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE, Polza3000 ₽/месяц.

Checkpoint 2026-10-05, T12R/R4: закрыто GET transport ядро ([отчёт](../../TEAM_NATIVE_GET_VALIDATION.md)):355 affected PASS/42,01с/exit0,118 новых+237 существующих; strict current token principal/project/direct membership и capped pagination, known8765/обратныйUTC repairs с actual2-case RED, controlled timeout test.297 прежних inputs unchanged +2 NEW/current299. Production native observations/managed lifetime не квалифицированы, full suite не повторялся. Следующая отдельная [managed lane](../../contracts/team-native-read-observation.md) должна exact stop/проверитьjournals/получить новую closedSourceEvidence; post-hold checkpoint/cleanup запрещены, прежний NoDelete PASS их выполнял. Полный R4/owner nonce/final decision/common predicate/advancing head/lineage, R5–R6 и browser/live/manual/24h обязательны; activation OFF, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE, Polza3000 ₽/месяц.

Уточнение 2026-10-05: [console диагностика](../../TEAM_CONSOLE_FLAGS_VALIDATION.md) дала чистый single-phase own-Python DETACHED запуск. Пятый [native DETACHED](../../TEAM_NATIVE_DETACHED_VALIDATION.md) завершился `MEASURED_NOT_QUALIFIED/NATIVE_RO_STARTUP_DENIED`: Windows sharing refusal, all4 full SQL/bytes/FileID unchanged, known cleanup/root-only true, HTTP/GET NOT_RUN. Четыре прежних FAIL сохранены. Отдельный ремонт двух launch flags Supervisor завершён:49 lifecycle PASS/2,44с/exit0 и independent review closed;296 прежних sources unchanged/только Supervisor changed. Full suite не повторялся, Caddy/nested gateway E2E остаются открытыми. Полный R4/native observational lane/owner nonce/final decision/common predicate/lineage, R5–R6 и ручные этапы обязательны; activation OFF, Team NOT_READY_FOR_MANUAL_TEST, goal ACTIVE.

Запускать только после создания соответствующих файлов/изоляции. При планировании они НЕ запускались.

```powershell
Set-Location -LiteralPath 'D:\AI\Projects\Active\Secretary'
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py tests audit/tests -q
Push-Location frontend
try {
    npm.cmd run test:processing
    npm.cmd run typecheck
    npm.cmd run lint
    npm.cmd run build
} finally { Pop-Location }
```

Исполнитель обязан проверять exit code каждой команды; PowerShell `try/finally` сам по себе не превращает ненулевой npm exit code в исключение. Полный suite нужен после общих границ/T13; после каждой узкой карточки достаточно её meaningful affected tests. Не запускать существующий live `check_lifecycle.py` в offline baseline.

## Остановка, продолжение и rollback

Остановить зависимую mutation/live часть при неподтверждённом endpoint/лицензии/hash, отсутствующем HTTPS или доступе к боту, невозможности проверить лимит, утечке секрета, неопределённом результате платной операции, недопустимом scope или риске незапланированного изменения пользовательской БД. Сформулировать один конкретный следующий шаг; продолжить независимую offline работу.

При дефекте: выключить feature flags `team_enabled`/`max_intake_enabled`/`voice_commands_enabled`; прекратить новые sends/charges, сохранить receipts/inbox/outbox. Не откатывать DB на старую копию поверх рабочей без backup и сверки внешних задач/расходов. Не удалять опубликованные задачи автоматически. Версию binary/config откатывать в отдельную папку; старый Secretary запускается с совместимой схемой либо восстановленной согласованной копией.

Каждая карточка заканчивается записью в `docs/TEAM_IMPLEMENTATION_STATUS.md`: T-id, changed files, точные команды и exit codes, местоположение evidence, статусы offline/live/manual, выявленные ограничения и следующий шаг. Закрывать чекбоксы только после доказательства. Для продолжения после compaction достаточно spec, этого плана, status и diff последней карточки.

## Готовый стартовый запрос для Sol 6.1

```text
Работай в D:\AI\Projects\Active\Secretary по:
docs/superpowers/specs/2026-10-03-secretary-vikunja-max-design.md
docs/superpowers/plans/2026-10-03-secretary-vikunja-max-sol-6-1.md

Модель исполнения gpt-6.1-sol, reasoning high; xhigh для review auth/billing/outbox.
Начни с T0, затем выполняй карточки последовательно с тестами и сохранением evidence.
Используй фактический Git status; ничего не reset/stage/commit/push без моего запроса.
Polza обязателен, общий бюджет 3000 рублей в месяц для всех процессов и функций.
Новых локальных ASR/LLM на GPU не устанавливай. Vikunja используй только FREE.
Не проксируй текущий Secretary API наружу. MAX получает отдельный gateway и ACL.
Отсутствие ручного тестирования и неизвестный внешний HTTPS не блокируют offline
реализацию. При внешнем подключении сначала подготовь конкретную конфигурацию,
покажи оставшийся шаг владельца и не выдавай synthetic PASS за live или manual PASS.
Секреты, личные записи и базы не выводи и не отправляй в Git.
После каждой карточки обновляй TEAM_IMPLEMENTATION_STATUS и продолжай следующий
доступный этап. На неопределённых платных/внешних операциях сохраняй состояние,
не повторяй их вслепую. Полную приёмку заверши по T13 и честно укажи human gates.
```

### Offline checkpoint 2026-10-12 — typed R4 ticket contract

Added a pure, non-authorizing `MaintenanceTicketV1` DTO bound to `R4AdmissionBindingV1` and the exact current `SourceGenerationV1`/`V2`. It rejects missing or legacy-unbound records, stale generations, checksum damage, and deployment/participant/run/operation/fence mismatches; known MAX/Vikunja/Polza kinds require their corresponding dispatch action. TDD: initial RED was 7 expected failures; final focused suite was **67 PASS / 0.14s / exit 0**. The affected restore/maintenance/process-crash batch was **272 PASS / 1 existing Starlette/httpx deprecation warning / 67.96s / exit 0**, evidence `.runtime/team-rollout/t12r-r4-ticket-v1-20261012.txt`; `git diff --check` exited 0 with existing LF→CRLF normalization warnings.

No repository schema or runtime consumer changed. The exact stopped-runtime maintenance registry and the absent independent fixed-ledger evaluator/current-generation reader prevent a truthful cross-process source-bound admission test today; synthetic DTO equality must not be treated as current authority. Activation/outbound stay OFF and execution stays offline-only. Next: design and test an explicit versioned maintenance-registry migration plus the independent current-generation/evaluator boundary, then add cross-process missing/stale/unbound zero-side-effect tests before wiring any consumer.

Подготовка плана завершена; владелец запросил исполнение. Продолжать с первой незавершённой карточки по TEAM_IMPLEMENTATION_STATUS.md и её фактическим evidence, сохраняя все требования T0–T13.

### Offline checkpoint 2026-10-12 — versioned maintenance registry V2

Added exact, additive maintenance registry V2: V1 (`user_version` 0/1) migrates atomically to V2 (`user_version=2`) with an append-only ticket commitment table; stopped-runtime verification remains read-only and validates V1/V2 layout, canonical record checksum, DTO fields, and lifecycle-ticket scope. TDD covered fresh schema, V1 migration, V1 read-only compatibility, malformed records, and append-only constraints. A broad affected run exposed one API compatibility regression in the preserved invalid-control-path error code; restored the stable code. Final `tests/test_team_backup.py tests/test_maintenance_registry_v2.py tests/test_restore_runtime_evidence.py` run: **133 PASS / 1 existing deprecation warning / 62.96s / exit 0**, evidence `.runtime/team-rollout/t12r-r4-registry-v2-final-affected-20261012.txt`. No real DB migration, consumer wiring, credentials, external requests, or Git mutation. Next: independent fixed-ledger/current-generation read/evaluator, then cross-process missing/stale/unbound zero-side-effect tests. Activation/outbound remain OFF; T13 and production qualification remain open.

### Offline checkpoint 2026-10-12 — current blocked activation-decision reader

Added a read-only fixed-path reader for the current blocked R4 activation decision. It resolves runtime paths from the validated loaded Team config and fixed R3 inventory, re-collects current R4 source evidence, compares the full saved commitment with current config/provider/source state, and rejects missing, stale, or expired records without creating or rewriting the ledger. TDD and the affected R4 batch passed **379 tests**; the full guarded backend/audit run passed **5,410 tests with one existing Starlette/httpx deprecation warning in 2,033.73 seconds**. Frontend processing/API/workflow tests passed **301/301**, release-manifest passed **1/1**, and typecheck/lint/build passed. Evidence: `.runtime/team-rollout/t12r-r4-current-blocked-reader-full-offline-20261012.txt`.

The local Secretary is running offline on `127.0.0.1:8765`; Team routes there return 404 because Team runtime is not mounted in this launch. A fresh isolated `HTTP_SYNTHETIC_UI_ONLY` handoff is open at `http://localhost:60787/fixture` and `http://localhost:60787/team/`; manual login/board acceptance remains pending. No live providers, real data, Go proxy/sumdb, or credentials were used. This reader is diagnostic only: it does not provide the current-generation evaluator/shared predicate, durable operation-ticket admission, or native authenticated GET/advancing head. Activation/outbound stay OFF; do not wire consumers. Next: implement and test the independent evaluator plus cross-process missing/stale/unbound zero-side-effect cases. T9 manual and T13/live gates remain open.

### Clarification 2026-10-13 — operation-ID audit in the current checkout

The older 2026-10-12 operation-ID checkpoint above describes a gap that is already closed in the current checkout. `MaintenanceRepository.assert_dispatch_admission()` rejects a missing operation ID and compares it to the active ticket; `bind_current_operation()` binds the claimed operation before Team, bot, notification, and publication worker dispatch. MAX and Vikunja final request seams call the gate before transport. Focused offline verification on 2026-10-13 passed: ticket/barrier **2/2**, worker operation binding **3/3**, and runtime MAX/Vikunja mismatched-ticket zero-side-effect checks **3/3**. No production code changed during this audit.

The next unfinished card remains the trusted current source-generation/native-head evidence contract. The existing diagnostic blocked evaluator is not a permission predicate; define and verify typed provenance, freshness, replay and zero-side-effect behavior before any consumer wiring. Activation/outbound remain OFF, and offline-only still excludes MAX, Polza, Vikunja, public Go proxy/sumdb and credentials.

### Offline checkpoint 2026-10-13 — R4 regression and Team UI build

The evaluator/decision/ledger, admission binding, maintenance registry V2, backup/barrier, workers and runtime regression batch passed **266 tests / 0 failures / 1 existing Starlette/httpx deprecation warning / 261.81s / exit 0**; evidence is `.runtime/team-rollout/current-r4-focused-20261013.log`. Frontend processing/API/workflow passed **302/302**, release-manifest **1/1**, typecheck and lint passed, and a Vite production build emitted 9 artifacts under `.runtime/team-rollout/frontend-build-current-20261013-2350` without replacing the ordinary dist. A fresh built-bundle handoff is ready at `http://localhost:65229/fixture` and `http://localhost:65229/team/`; run `t9-http-ui-adb01d3416c047458817d06c0f068235`, TTL to about 00:23 MSK on 2026-10-14, synthetic UI only. The user confirmed that synthetic cards display after login and selecting “Проект 7”; no card mutation, preview, receipt, or persistence was manually accepted. This does not qualify live providers, TLS, phone, or production. Next: typed and independently verifiable provenance/freshness/replay for a trusted native history/head; never infer authority from a synthetic or diagnostic sequence. Consumers, activation, and outbound remain OFF.

Additional native-history/observation/source-epoch audit passed **355/355 / 155.46s / exit 0**; evidence `.runtime/team-rollout/current-native-head-audit-20261013.log`. No `SourceGenerationV1/V2` producer exists in non-test backend.

### Read-only host probe 2026-10-14 — TPM availability

Around 00:00 MSK, `Get-Tpm` returned `TpmPresent=True` and `TpmReady=True`. No NV monotonic counter, trust-anchor behavior, TPM ownership/key, persistence/replay semantics, or access policy was tested. The unresolved owner decision is whether to investigate a TPM-backed local witness, an isolated service-account/ACL witness with documented admin residual risk, or retain the R4 block; no native DB/TPM/ACL state or consumer was modified.

### R5 blocked-state process-crash check 2026-10-14

`tests/test_restore_successor_process_durability.py` passed **2/2 / 0.82s / exit 0**. A child process that exits after the blocked commit leaves a readable `partial_prepared_blocked` record and no activation/transfer API; an uncommitted SQLite write retains its journal and fails closed without repair. This does not demonstrate successor high-water transfer, native trust, or actual power-loss recovery.
