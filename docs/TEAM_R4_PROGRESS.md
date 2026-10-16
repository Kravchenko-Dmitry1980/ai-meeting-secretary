# R4: диагностика восстановленных authorities и дальнейшая активация

## Checkpoint 2026-10-06 15:55 МСК: UPSTREAM_READONLY_MODE_NOT_AVAILABLE

Повторно проверены актуальные официальные releases/configuration/security-hardening страницы. GitHub всё ещё помечает Vikunja v2.7.0 как Latest; published config описывает `database.path`, но не read-only setting. Это согласуется с pinned source, который требует `O_RDWR|O_CREATE` и WAL. Нового официального бинарника, снимающего контрактный конфликт, нет. [Подробный evidence и границы отрицательного поиска](TEAM_R4_NATIVE_READONLY_FEASIBILITY.md). Никакие версии/DB/guards не менялись; R4 activation остаётся OFF до отдельно согласованного архитектурного пути.

## Checkpoint 2026-10-06 15:41 МСК: NATIVE_READONLY_CONTRACT_BLOCKER_CONFIRMED

Сверены Microsoft process flags, исходник pinned Vikunja v2.7.0 и закрытый detached native receipt. Stock SQLite initialization выполняет `os.OpenFile(path, O_RDWR|O_CREATE)` и затем задаёт WAL; под строгой NoWrite custody Windows закономерно возвращает WinError 32 до HTTP. `CREATE_NO_WINDOW` игнорируется вместе с `DETACHED_PROCESS`; текущий launcher использует только detached, но успешный native startup этим не доказан. Никаких новых запусков/изменений источников не было. Решение: сохранить guards и pinned binary, не делать RW fallback/clone/allowlist expansion; зависимый native GET остаётся заблокирован до отдельного утверждённого read-only/backend решения. [Feasibility report](TEAM_R4_NATIVE_READONLY_FEASIBILITY.md). Independent offline tasks continue; R4 activation remains OFF.

Текущее продолжение 2026-10-04: [four-source epoch preparations](TEAM_R4_SOURCE_PREPARATION.md), 646 affected / 4462 full PASS, 287 named hashes unchanged. Эта часть реализована с blocked readbacks; ниже сохранена история предварительного SQL-opening исправления. Полная R4 и live/manual qualification ещё обязательны.

Дата: 2026-10-04. **R4 IN_PROGRESS; activation не реализована.** Исправлена предварительная часть R4: открытие восстановленных SQL authorities и диагностические probes. Это не завершённая карточка R4, разрешение нового work, ввод восстановленной копии в эксплуатацию или ручная приёмка.

Связанные документы: [контракт R0–R6](contracts/team-restore-reconciliation.md), [R3 COMPLETE_OFFLINE](TEAM_R3_VALIDATION.md), [план Sol 6.1](superpowers/plans/2026-10-03-secretary-vikunja-max-sol-6-1.md).

## Проверенные изменения

- Team и Billing проверяют guard до write-capable opening, WAL, каталогов и migrations. Guarded constructors открывают диагностику, сохраняют историческую схему и ничего не мигрируют. Обычные невосстановленные базы продолжают использовать свои штатные transactions/migrations.
- Secretary, Team и Billing используют `mode=ro` для guarded SQL handles. Снятие обычного caller authorizer не превращает основной файл в writer. `RestoredDiagnosticConnection` удерживает запрет ATTACH/DETACH, структурного SQL и mutating PRAGMA при `set_authorizer(None)` и при caller callback, возвращающем `SQLITE_OK`: диагностическое соединение не получает вторую writable authority через ATTACH.
- Guarded Billing transactions допускают чтение, но не обычную settlement/release/account refresh. Произвольный переданный receipt dictionary не является проверенным актуальным provider reconciliation. Исторические reserved/submitted/uncertain и их amounts остаются неизменными. Данные не переводятся в fake confirmed/applied/sent.
- `MonthlyBudget` читает и проверяет уже существующий approved policy без повторной записи. Отсутствующий policy в guarded базе не создаётся; несовпадающий не исправляется автоматически. Общий действующий бюджет Polza 3000 ₽/месяц сохранён, прежние тарифные ceilings не возвращаются.
- Gateway и recovery используют общий диагностический guard probe. Inactive WAL-header source без journals читается без создания WAL/SHM. При существующем WAL применяется journal-aware read-only SQLite: guard, находящийся в WAL, учитывается; main DB и WAL сохраняются побайтно. SQLite может обновить volatile SHM reader marks; это не доказательство остановки writer и не изменение business rows.

Эта защита относится к штатным repository/connection API приложения. Она не является OS sandbox для произвольного same-user Python/native кода. Установку restore guard в работающий оригинал она не делает допустимой: supported restore создаёт отдельную inactive copy. Transaction-bound полная R4 проверка перед work/dispatch ещё обязательна.

## Доказательства текущего шага

Evidence root: `.runtime/team-rollout/r4-sql-opening-20261004/`. Команды выполняются через неизменённый `scripts/run_offline_tests.py`, запрещающий рабочие stores/credentials, внешний HTTP и live lifecycle.

- New Team opening: actual RED 20 FAIL / 2 PASS → 22 PASS до общей интеграции.
- New Billing opening: actual RED 14 FAIL / 6 PASS → 20 PASS до общей интеграции.
- Root probes: первый прогон 5 FAIL / 5 PASS. Две ошибки воспроизвели временное создание WAL/SHM; три относились к ошибочному требованию неизменности volatile SHM. После исправления этого test expectation — 2 FAIL / 8 PASS; затем production repair — 10 PASS. Первые логи сохранены.
- Root cross-authority ATTACH: 3 actual FAIL с изменением второго synthetic restored файла → запрет и сохранность во всех трёх repositories. Финальный вариант также проверяет caller allow-all callback: 6 PASS.
- Root budget diagnostic reopen: 1 actual FAIL / 2 PASS → 3 PASS. Промежуточный fixture AttributeError исправлен на фактическое поле `approved_limit_micro`; лог сохранён.
- Старое T12 ожидание произвольной settlement после restore заменено проверкой отказа, сохранением `reserved` и `confirmed_micro=null`. Старый FAIL и два обнаруженных factory failures сохранены; factories после диагностического budget reopen снова дают штатный отказ до paid POST.
- Первый общий opening run: 86 PASS / 1 test expectation FAIL: структурный SQL теперь получает `SQLITE_AUTH`, тогда как main UPDATE после callback clear всё ещё доказывает OS `SQLITE_READONLY`. Исправлено только ожидание CREATE case.
- Финальный opening + идентификационный backup: **91 PASS, 10.47 с, exit 0**. Более длинный agent scratch ранее дал backup copy `FileNotFoundError`; тот лог сохранён, причина длины пути остаётся hypothesis. Штатный неизменённый runner с обычным scratch прошёл тот же test. Backup implementation не менялся.
- Финальный affected run, 16 test files: **407 PASS, 105.14 с, exit 0**; один прежний Starlette/httpx warning. Все **270** выбранных Python/lock/config hashes до/после совпали. Пересекающиеся counts не складываются.
- Первый полный backend/audit: **3885 PASS / 15 FAIL, 707.77 с, exit 1**. Все failures находятся в `test_team_runtime_health.py`: прежний test-only parser `file:///D:/...` давал ложный отказ разрешённому временно́му SQLite path. Production code и runner для исправления не менялись. Fixture использует уже проверенный `sqlite_test_paths.sqlite_path` с тем же обязательным `tmp_path` containment. Новые два positive URI cases сначала дали RED; отрицательный parent-escape case остался запрещён. 17 FAIL / 2 PASS → 31 PASS с shared parser/runner negative tests, 3.02 с.
- После fixture repair: **438 affected PASS, 105.76 с, exit 0**, 19 test files, все 270 named source hashes совпали. Это финальный affected результат; предыдущие 407 сохранены как история до repair. Final evidence: `.runtime/team-rollout/r4-sql-opening-final-20261004/`.
- Второй полный backend/audit: **3902 PASS / 1 FAIL, 707.17 с, exit 1**; все 270 named source hashes до/после совпали. Единственный FAIL — нагрузочный fixture: вероятностная пауза 35 мс не обеспечила фактическое пересечение provider attempts двух процессов. Все проверки количества команд/эффектов до этого assertion прошли; требование реального пересечения сохранено.
- Load fixture repair: opt-in first-mutation rendezvous с новым run UUID и двумя фактическими PID из fresh helper ready markers; actual attempt записывается до ожидания, complete marker публикуется атомарно, peer проверяется по durable attempt. Ожидание вне SQL transaction, bounded timeout; одиночные requests и повторные mutations не ожидают peer. Новые negative/owned-child cases сначала дали **5 FAIL**. Первый GREEN attempt дал **4 PASS / 1 fixture FAIL**: Windows venv launcher PID отличается от actual child interpreter PID. Fixture сохраняет фактические helper PID и не приравнивает их к launcher PID; затем **5 PASS, 6.47 с**. Первые логи сохранены в `.runtime/team-rollout/r4-load-rendezvous-20261004/`. Добавлен negative case для fabricated peer marker; финальные результаты приведены ниже.

## Финальная offline проверка предварительного исправления

- **446 affected PASS, 250.11 с, exit 0**, 20 test files.
- **3909 full backend/audit PASS, 713.63 с, exit 0**; одно прежнее Starlette/httpx warning.
- 270 named source hashes совпали до/после focused load и в окне final affected/full. 64 новых opening/URI cases и 6 load rendezvous cases — всего 70 новых постоянных тестов.
- Load focused: **8 PASS, 70.58 с**; actual load evidence `t13-load-71d16108ff284ababe67a3439b066820`: 50 accepted, 42 applied/5 conflict/1 rejected/2 uncertain, 44 effects, 8 overlapping cross-process attempt pairs, 0 reported violations. MAX/Polza requests здесь симулированные, деньги не списывались.
- Два bounded source-only review: исходные 13 source pins без material findings; последний review двух load fixture файлов без material findings.
- Historical artifact helper SHA неизменён. Его исходный load context ожидаемо дал два stale source hash failures после repair; оба сохранены. Новый explicit context переиспользует тот же набор 52 checks с фактически успешным fresh load: **52 PASS / 24 scanned text files / 0 matches**. SHA/helper/контекст и byte-identical accepted backend alias проверены, прежние свидетельства не переписаны.
- Evidence: `.runtime/team-rollout/r4-load-rendezvous-20261004/`. R4 остаётся IN_PROGRESS; activation OFF; Team NOT_READY_FOR_MANUAL_TEST; goal ACTIVE.
- Отдельная bounded hygiene: 36 явно выбранных reports/docs/logs этой карточки, 0 matches по неизменённым strong patterns. Это не универсальная проверка всех секретов. Следующий R4 шаг и source-only native research: [next-r4-contract.md](../.runtime/team-rollout/r4-load-rendezvous-20261004/next-r4-contract.md). Настоящий isolated native experiment выполняется отдельным evidence root; результаты не подменяют SQL PASS или полный activation contract.

Final affected command:

```powershell
.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests/test_secretary_restore_guards.py tests/test_team_restore_sql_opening.py tests/test_billing_restore_sql_opening.py tests/test_restore_budget_diagnostics.py tests/test_restore_guard_probe_custody.py tests/test_restore_sql_attachment.py tests/test_monthly_budget.py tests/test_cloud_budget_boundary.py tests/test_budget_dispatch_boundary.py tests/test_team_paid_maintenance.py tests/test_team_secretary_maintenance.py tests/test_team_backup.py tests/test_team_runtime.py tests/test_team_runtime_recovery.py tests/test_team_repository.py tests/test_team_auth_repository.py tests/test_team_runtime_health.py tests/test_sqlite_test_paths.py tests/test_offline_sqlite_uri.py tests/test_team_load.py -q --tb=short
```

Full command:

```powershell
.venv\Scripts\python.exe -B scripts/run_offline_tests.py tests audit/tests -q --tb=short
```

Frontend не менялся. Ранее frozen 263 frontend tests/typecheck/lint/build остаются историческими проверками, а не новым прогоном этой карточки.

## Дополнительный LOCAL_NATIVE_SYNTHETIC эксперимент

После закрытия SQL prerequisite выполнен один isolated experiment: `.runtime/team-rollout/r4-native-marker-ac6d523ff85047f0a3757c8a1c45c5a6/`. Скрипт `exit 0`, 7.75 с; точный binary SHA Vikunja 2.7.0 совпал с manifest. Два обычных web starts сохранили все 16 candidate triggers на `projects`/`tasks` и metadata. Actual loopback API CRUD продвинул историю 0 → 14 → 22. В SQL rollback было 15 events внутри transaction и 14 после; business state также откатился.

Настоящие native `dump`/`restore` CLI дали `exit 0` на отдельной новой synthetic copy. Незарегистрированные marker/history остались, 12 attached business triggers исчезли. Кандидатный validator отказал с `candidate_schema_changed`; web этой восстановленной копии после restore не стартовал. Пять собственных lifetimes проверены на Windows Job membership и остановлены; отдельная PID+creation-time проверка подтверждает, что исходные процессы не живы. Web processes получили `exit 1` от явного owned cleanup через TerminateProcess; это не успешный graceful shutdown и не ошибка раннего startup. Native error signals отсутствовали, readiness/CRUD подтверждены до cleanup. CLI и harness завершились `exit 0`.

Пять named source hashes до/после совпали. Эксперимент подтверждает compatibility candidate только для двух native business tables, не реализует общий runtime predicate, независимый durable watermark, full native catalogue, in-place FileID rollback или R5. Только harness HTTP ограничен direct loopback; optional native services отключены и задан неслушающий loopback proxy. OS outbound sandbox не заявлен; host firewall/trust/services не менялись. Рабочие MAX/Polza/данные владельца не использовались. R4 по-прежнему IN_PROGRESS, actual activation не выполнена.

## Обязательная оставшаяся реализация R4

Следующее действие R4 — implementation независимого ledger/epoch с четырьмя фактическими preparations/readbacks, trusted fresh owner/provider issuers и qualified native adapter; затем единый predicate и fresh authorization lineage во всех execution boundaries. Compatibility experiment не заменяет эту реализацию.

1. Явный полный source context четырёх authorities, независимый activation ledger и неизменяемый epoch. DTO, `outbound_enabled`, новая normal control DB и R3 `complete_prepared_blocked` не являются разрешениями.
2. Контролируемый переход от R3 actual baseline к append-only epoch receipts; последний global activation decision только после durable readback всех источников. Не удалять/обнулять guard=1 и не менять старые R2/R3 receipts. Legitimate новые записи меняют DB bytes, поэтому R3 file/logical hashes нельзя сравнивать с live DB бесконечно: это baseline, не perpetual invariant.
3. Отдельное свежее owner consent, связанное с конкретным scope/epoch/current grants. Текущий Windows operator и архивная business owner row не взаимозаменяемы. Provider observations получает trusted pipeline через реальные фиксированные GET adapters; arbitrary JSON или подпись произвольного документа не доказывают внешний результат. Unknown liabilities удерживаются.
4. Durable freshness и propagation authorization lineage до admission/claim/recovery/reserve/final dispatch. Новый UUID, expiry lease и отсутствие R2 deny сами по себе не дают fresh grant. Старые jobs/handoffs/publications/commands/buttons/voice/reminders/auth roots исключаются из execution, сохраняют свою историю и не порождают новые paid attempts.
5. Тот же полный predicate перед обычным startup, native Popen/restart/resume, lower SQL, recovery и final Polza/Vikunja/MAX dispatch. Native effective config/database/assets/binary должны связываться с фактическим source. Native marker/history policy требует отдельной проверенной совместимости; path/FileID сами по себе не доказывают отсутствие in-place content rollback.
6. Raw MAX text/media intake и automatic subscription reconciliation остаются отдельными выключенными capabilities до реализованного actual cutover/current ownership protocol. Старый bot event не выпускает новый desktop login code.

Затем R5 — следующий backup/restore generation с новым hold; R6 — fault/no-replay и отдельные actual owner/provider/manual/24h gates. Whole goal ACTIVE, Team NOT_READY_FOR_MANUAL_TEST. Ответ владельца про HTTPS сохранён: внешний адрес пока неизвестен, нужна проверка по runbook до MAX/phone pilot.

Рабочие stores/env/config/audio/credentials/providers/MAX accounts/messages/processes, Scheduled Tasks/power/trust/router/NAT/VPN, глобальные установки и Git state в этом шаге не изменялись. Paid calls не выполнялись.


## Следующая реализованная часть R4

**633 affected PASS, 238.06 с; 4028 full backend/audit PASS, 798.86 с, exit 0**. Все 278 named hashes до/после совпали; одно прежнее Starlette/httpx warning.

Actual source baseline, independent blocked ledger, resumable staging и full pinned native catalogue precursor: [TEAM_R4_ACTIVATION_PREPARATION.md](TEAM_R4_ACTIVATION_PREPARATION.md). Четыре записи ledger описывают R3 baselines; actual R4 source-epoch writer/final decision/predicate/fresh grants и lineage ещё не реализованы. Full R4/R5/R6 и общий goal остаются незавершёнными.
