# Secretary Team: запуск, остановка и ограничения приёмки

Текущий результат: исправлена остановка изолированного UI-сервера; проверка
восстановления Vikunja выявила неустановленный дополнительный процесс при
запуске. **T9 browser NOT_RUN; R4 IN_PROGRESS; Team NOT_READY_FOR_MANUAL_TEST.**
Goal остаётся ACTIVE. Это отчёт отдельной диагностической карточки, а не
результат полной эксплуатационной приёмки приложения.

## Исправление тестового сервера

После запроса остановки прежний собственный fixture потерял listener, но его
процесс продолжал работать с открытыми соединениями. В конфигурации Uvicorn
отсутствовал `timeout_graceful_shutdown`; ожидание соединений/запросов было
неограниченным. Это подтверждённый дефект настройки. Полная причина старого
состояния соединений и прежних Windows TLS callback errors не установлена.

В `scripts/team/probe_team_ui.py` установлен таймаут drain **5 секунд**.
`tests/test_team_ui_probe.py` дополнен двумя параметризованными случаями:
зависшее соединение и незавершённый request task. Они вызывают настоящий
`uvicorn.Server.shutdown` с конфигурацией из `_serve`. До cleanup самого теста
проверяются отмена request task, завершение worker, закрытие fixture и manifest
`stopped`. Это проверка поведения, а не сравнение переданного аргумента.

| Проверка | Фактический результат |
|---|---|
| До исправления | 2 FAIL, 13,81 с, exit 1; превышены 6,5 с ожидания |
| Финальные два случая | 2 PASS, 11,01 с, exit 0 |
| Полный affected fixture suite | 22 PASS, 70,16 с, exit 0 |
| Новый собственный TLS-сервер с открытым проверенным TLS socket | Самостоятельная остановка: 6,469 с; manifest `stopped`, child завершён |

Одно прежнее Starlette/httpx deprecation warning. Counts пересекаются и не
складываются. Пять секунд ограничивают drain Uvicorn; это не гарантия общего
пятосекундного завершения, независимо зависшего lifespan или worker cleanup.

Actual TLS run: `t9-ui-f5a076e1e8e34619824ae65179218b99`, PID 12044. Launcher
выполнил один readiness GET. Проверенное TLS-соединение удерживалось открытым
до фактического выхода child; отдельно наблюдалось закрытие listener до выхода.
Затем собственный socket закрыт. Браузер и провайдеры в этом прогоне не
использовались.

Прежний зависший run `t9-ui-7434c5371e6348ed94288610412a6bd7`, PID 58200,
завершён root через проверенный retained Win32 HANDLE: точные argv, creation
time и mapped venv base image проверены дважды, один TerminateProcess, exit
129. Это **forced cleanup**, а не graceful PASS. Исходный manifest `ready`
сохранён как устаревшая запись; он не переписан в фиктивный `stopped`.

Evidence:
`.runtime/team-rollout/t9-browser-acceptance-316454ceb8734eddb67dbafad7f7c3af/`.
`FINAL_REPORT.json` SHA256:
`3cb605c83b52d671f3d7a401f3d8f7046332a3c7dec23f3152567ebd9551be14`.
В этой папке сохранены RED, промежуточные GREEN, финальные strengthened logs,
actual TLS receipt, forced-cleanup contract/receipt и artifact hashes.

Завершённые команды:

```powershell
.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests\test_team_ui_probe.py -q --tb=short -k bounds_actual_uvicorn_shutdown_drain
.venv\Scripts\python.exe -B scripts\run_offline_tests.py tests\test_team_ui_probe.py -q --tb=short
```

## Браузер и текущий bundle

Production `frontend/dist/team.html` включает внешний MAX SDK. Строгий
локальный asset allowlist fixture отклоняет такой entry:
`fixture_asset_reference_invalid`. Для проверки подготовлена отдельная scratch
копия: удалён только один SDK tag, три локальных JS/CSS asset byte-identical.
Production entry не изменён. Scope: **OFFLINE_UI_WITHOUT_MAX_SDK**; запуск
требует `--frontend-dist`. [Инструкция](TEAM_UI_PROBE.md) исправлена.

Chrome показал `ERR_CERT_AUTHORITY_INVALID`. Управляемый инструмент не смог
подключиться к внутренней странице предупреждения; human handoff не привёл к
открытию `/fixture`, владелец сообщил «Перейти не удаётся». Обхода предупреждения
и изменения trust-store не было. Старые тестовые адреса уже остановлены.

**NOT_RUN:** desktop/narrow login, pagination, large IDs, кнопки Kanban,
матрица, task details, queued/applied card, ACL и logout. Screenshot отсутствует.
Fixture также не содержит notification scheduler и meeting-origin seeds;
отмена старого напоминания и ссылки на meeting sources требуют отдельной
подготовки, даже после доступа браузера. MAX SDK/WebView, телефон и внешний
HTTPS остаются непроверенными. Локальная проверка сертификата TLS-клиентом не
подменяет эти результаты.

## Native Vikunja: три сохранённых отказа

Все попытки использовали точный закреплённый Vikunja 2.7.0 executable:
SHA256 `e485792c33f537124fb187658a84099a74ab0f1b948a44dee9049fede1ad66b6`.
Только новые synthetic GUID; рабочие данные, credentials и обычный Supervisor
не использовались.

| Run suffix | Достигнутая стадия | Результат |
|---|---|---|
| `20bf70c5befb4f739cb5cf61fa6d527b` | Два CLI seed, seed web | FAIL `unknown_owned_job_descendant`; известные children остановлены, полная Job cleanup не подтверждена |
| `b4e8900622844db2bde6ff8c158c74b2` | То же, с фактическими Job snapshots | FAIL; настоящий дополнительный PID при живом seed web; final Job root-only |
| `e1b57707785f45c89086e24a354dc383` | Без seed web/login/tokens; candidate под NoWrite | FAIL; дополнительный PID при живом candidate; final Job root-only |

Первая и вторая папки:
`.runtime/team-rollout/r4-native-ro-compatibility-<suffix>/`.
Третья: `.runtime/team-rollout/r4-native-ro-startup-only-<suffix>/`.
Root фактически наблюдал exit 1 всех трёх helpers; root logs сохранены в
`r4-native-ro-root-63bf30ad1df447718003983816deca3a`.

Третья попытка действительно достигла полного pre-Popen baseline: 42 business
tables, 390 triggers, head/events/grants 0; четыре NoWrite/NoDelete leases,
попытки write-open отклонены Windows error 32. При readiness kernel Job list
содержал root 25032, candidate 46284 и неизвестный 23928. Уже завершённые
CLI seeds в списке отсутствовали. Гипотеза о задержке удаления известных
завершённых PID не объясняет это наблюдение.

Наблюдение прервано до HTTP. **Authenticated GET NOT_RUN**, startup denial
также не квалифицирован: нельзя объявлять доказанным отказ SQLite по NoWrite
из результата `unknown_owned_job_descendant`. Неизвестный PID не запрашивался
и не использовался для отдельной команды завершения. Установленная причина
его появления отсутствует; четвёртый повтор того же сценария не выполнен.

Независимая сверка третьей попытки: **24/24 checks PASS**, при этом эксперимент
остаётся FAIL. Четыре actual SQL файла совпали с baseline по bytes/size/FileID;
закрытый reader под новыми NoWrite leases подтвердил всю schema, metadata
bodies, 42 business hidden-rowid digests и 390/head0/events0/grants0. Sidecars
до/после отсутствуют; checkpoint/repair не выполнялись. Предыдущие 14 артефактов
и 13 достигнутых артефактов третьей попытки сохранены. Отсутствующие original
afterstop файлы не созданы задним числом. Новый reviewer readback имеет отдельное
имя и не является R3/V1/SourceEvidence authority.

Independent receipt:
`.runtime/team-rollout/r4-native-ro-startup-independent-00f1c87d041d41948608431d4903650b/final-receipt.json`,
SHA256 `ba8aecf653cedf1716f858b16140afdece1ed8089e0eeca34c17d3b2607766f7`.

## Что показал аудит API и исходников

Pinned official source указывает на GET `/api/v2/user` и permission
`{"other":["user"]}` для свежей token-owner identity. Project-only scope и
исторический token-creation receipt не заменяют эту проверку. Missing scope
может дать generic 401, поэтому 403 не следует обещать. Это статический вывод,
actual GET под диагностической custody ещё не выполнен.

Source audit обычного startup выявил RW open/migrations и возможную запись
`license_status` даже до free-mode return. В отдельно прочитанных
[web.go](https://github.com/go-vikunja/vikunja/blob/a16be96aa454671fdf213b0fbe411dd38a098418/pkg/cmd/web.go),
[init.go](https://github.com/go-vikunja/vikunja/blob/a16be96aa454671fdf213b0fbe411dd38a098418/pkg/initialize/init.go) и
[license.go](https://github.com/go-vikunja/vikunja/blob/a16be96aa454671fdf213b0fbe411dd38a098418/pkg/license/license.go)
прямой subprocess call не найден. Go goroutine не объясняет новый Windows PID.
Transitive call graph не исчерпан; compiled binary/source correspondence
**NOT_PROVEN**. Эти сведения не разрешают RW fallback или снятие guards.

Frozen audit:
`.runtime/team-rollout/r4-native-startup-source-observer-9e64608e-6d91-40c0-b929-7b0564bf3b7d/audit.md`,
SHA256 `47bd7ab21bada37b38c660ab2258d433793100548f20404d6779855628a8947c`.
API identity audit:
`.runtime/team-rollout/r4-native-token-identity-781072b3-2b52-4c15-8cb7-3c40ecbe6ef9/audit.md`.

## Оценка и следующий шаг

Узкий дефект drain исправлен и проверен автоматически и настоящим TLS-сервером.
Автоматическая диагностика сохранила неуспешный запуск и подтвердила отсутствие
изменения synthetic SQL. Браузерное удобство, успешное восстановление,
реальная STT/диаризация, MAX delivery, стоимость реальных встреч и 24h
надёжность этим не измерены; production effectiveness пока не квалифицирована.

Root сверил прежние 295 source/test/config/lock inputs: **293 unchanged и два
явных T9 изменения**. Удаление только нового timeout и appended tests/import
восстанавливает точные прежние hashes обоих файлов. Production frontend/backend
не изменялись. Прежние **4725 full PASS** относятся к принятой предыдущей
карточке; после этой isolated fixture-only правки выполнен affected suite 22,
полный suite заново не запускался.

Observation-only collector завершён в отдельной карточке:
**79 affected PASS/0,16с/exit0** у root, actual own Python child diagnostic PASS,
фактические HANDLE rights `0x101000`, Wait-signaled exit259, final Job root-only
и полная очистка. Четыре найденных дефекта исправлены, независимые source и
receipt reviews завершены. Current297 named inputs: прежние295 unchanged +2 NEW;
full4725 остаётся историческим. V1 NOT_RUN и V2 preflight FAIL/child0 сохранены.
[Подробный отчёт observer](TEAM_JOB_OBSERVER_VALIDATION.md).

Последующая отдельная native observation завершена: **FAIL / exit1 / 2,203с**,
sticky `unknown_owned_job_descendant` сохранён. Collector успешно зафиксировал
current-path image `conhost.exe`,14 records/3 lifetimes и terminal exit0;
его ancestry/mapped image/source не квалифицированы. Все четыре synthetic DB
полностью unchanged;297 source/test inputs unchanged. Предыдущий ACL preflight
завершился до native Popen и не является ещё одним native attempt.
`CREATE_NO_WINDOW` уже установлен; причина появления console dependency ещё
требует отдельной проверки. HTTP/authenticated GET и actual NoWrite startup
denial не доказаны; автоматического allowlist expansion/RW fallback нет.
[Подробный отчёт](TEAM_NATIVE_OBSERVATION_VALIDATION.md).

После причины native startup остаются native current-principal GET, fresh
owner/provider grant с atomic consent nonce/final decision, advancing native
head, общий predicate/fresh lineage, R5/R6, T9 browser и T13 live/manual/24h.
Внешний HTTPS ещё неизвестен. Полный порядок сохраняется в
[плане Sol 6.1](superpowers/plans/2026-10-03-secretary-vikunja-max-sol-6-1.md).

Polza budget **3000 ₽/месяц** сохранён; платных/provider calls на этой карточке
нет. Owner stores/env/audio/credentials/accounts, host trust/power/network и
Git не изменялись. Root completion evidence:
`.runtime/team-rollout/r4-native-ro-root-63bf30ad1df447718003983816deca3a/completed-diagnostic-card.json`.
