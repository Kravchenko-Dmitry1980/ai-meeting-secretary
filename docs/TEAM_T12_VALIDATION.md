# T12: запуск Windows, наблюдаемость и восстановление

Состояние: **COMPLETE_OFFLINE_PACKAGE**. Заключительный прогон: **754 PASS, 195,34 с, exit 0**. Это отчёт по локальной реализации; он не подтверждает эксплуатацию MAX/телефонов, внешний HTTPS или работу 24/7. Общий план остаётся активным до полной приёмки T13.

## Что реализовано

- Отдельный Gateway собирает существующие Team/Auth/MAX/Voice/Notification/Vikunja компоненты по защищённому явному JSON. Импорт не читает `.env` и не запускает сервисы. Базы, проекты, identity бота и привязки участников заданы явно; отсутствующая настройка видна как `not_configured`.
- Исходный Secretary использует тот же control DB и общий Billing ledger. Бюджет Polza — 3 000 ₽ в месяц за вычетом утверждённых внешних расходов, с учётом более низкого проверенного лимита провайдера. Платные POST, account checks и неопределённые квитанции не подменяются успешным ответом.
- Общий maintenance admission охватывает HTTP, workers, capture, публикацию, фоновые потоки и платные запросы. Принятые операции заканчивают запись квитанций; новая запись/отправка при draining запрещена. Повторная отмена HTTP или Worker не освобождает работающий поток.
- Закрытие устройства освобождает микрофон, но сохраняет admission до callback receipt и передачи обработки защищённому executor. Активная запись откладывает резервную копию.
- При аварии сначала проверяется точный прежний Windows process lifetime. Scoped source/Billing checkpoints сохраняют неопределённые операции без повторного POST; только после этого завершаются точные старые tickets. Новый запуск не принимает чужой PID, socket или неподтверждённые дочерние процессы под управление.
- Проверка старых Gateway runs выполняется read-only до открытия источников и миграций. Она отличает активные claims от сохранённой истории: обычный перезапуск после завершённого события MAX разрешён, terminal state/worker/fence/lease и receipts сохраняются. Активная строка без соответствующего ticket по-прежнему требует ручной проверки.
- Full-project polling Vikunja выполняется с pagination. Дополнительный аудит после первоначальной T12 приёмки выявил completion+30s delay; исправленный runtime использует monotonic 30s ticks без overlap, пропускает missed ticks и сохраняет failure backoff. Узкий repair: 111 affected PASS / independent source review, evidence `.runtime/team-rollout/goal-sync-audit-a2877c73-b83b-4a43-b29a-1a1053a8d2a9/repair-final.md`; исторический cadence full на 3 497 PASS сохранён; исторический R0/R1 full на 3554 PASS сохранён; текущая R2 приёмка на 3676 PASS указана в `TEAM_VALIDATION.md`. Внешний неподтверждённый срок принимается только через owner preview с observation/revision/fingerprint и отдельное подтверждение даты или «Без срока». После записи обязателен final GET.
- Backup копирует четыре SQLite и настроенные assets под одним barrier, с остановкой собственного Vikunja и одновременными SQLite locks. Restore идёт в новый каталог, сохраняет immutable evidence и блокирует платные вызовы/старые отправки до reconciliation.
- Retention preview выбирает 7 daily + 4 weekly по проверенным архивам. Удаление и настройка расписания в этой проверке не выполнялись.

## Уточнение Windows-архитектуры

Две Scheduled Task definitions разделяют режимы запуска: boot-задача удерживает foreground Supervisor для трёх фиксированных компонентов Gateway/Vikunja/Caddy, logon-задача запускает исходный Secretary под его пользователем. Три независимые boot-задачи не создаются: Supervisor сохраняет общий порядок запуска, ownership, restart backoff и maintenance pause. Это уточнение способа исполнения T12 внутри прежнего набора компонентов. Capture task не включает микрофон.

Каждый рабочий native entry point устанавливает собственный Windows Job Object до создания компонентов. Это охватывает аварийное завершение одного Gateway/Secretary, пока общий Supervisor остаётся жив. Старый/direct factory запуск без подтверждённого containment не считается эквивалентным проверенному launcher.

## Проверки и evidence

Результаты ниже частично пересекаются и не суммируются. Финальный общий прогон 22 файлов выполнен после заморозки всех исправлений. Промежуточные 731/738 PASS сохранены в evidence, но не используются вместо финального результата.

| Проверка | Результат | Evidence в `.runtime/team-rollout` |
|---|---:|---|
| Финальный общий affected backend run, 22 файла | **754 PASS, 195,34 с** | `t12-accepted-final-v3-backend.txt`; selectors `t12-accepted-selectors.json` |
| Recovery: history preservation и все поддерживаемые active claims | 52 PASS, 41,88 с | `t12-recovery-historical-final-green.txt` — запись завершённого tool result |
| Независимый runtime/recovery review | 9 PASS, 4,84 с | `t12-runtime-review/final-evidence-9.txt`; итоговый review рядом |
| SQL/assets binding, backup/restore и recovery integration | 84 PASS | `t12-root-source-binding-final.txt` |
| Worker cancellation, исходный backend и speaker jobs | 139 PASS | `t12-worker-cancel-green.txt` |
| Executor, capture/HTTP admission и enrollment | 67 PASS | `t12-owned-http-thread-final.txt` |
| Backup/restore, assets, binding, native containment и смена run перед barrier | 38 PASS | `t12-backup-containment-final-38.txt` |
| Независимый lifecycle/HTTP/capture/Worker review | 12 PASS | `t12-lifecycle-review/final-combined-evidence.txt` |
| Независимый порядок Job installation и отказ запуска | 9 PASS | `t12-lifecycle-review/process-job-wiring-final.txt` |
| Настоящие PowerShell scripts с fake Scheduler | 5 PASS | `t12-lifecycle-review/scheduler-final-evidence.json` |
| Изолированные Windows child containment и kernel identity proof | 9 PASS | `t12-process-jobs-d3336f9fbcee416ea124a23cd3992390/evidence.json` |
| Supervisor с собственными synthetic child components | 4 PASS | `t12-lifecycle-ab736ad8-d835-4a75-bd58-87371eb9eb24/evidence.json` |
| Реальный Caddy на новом высоком loopback-порту | 84 checks PASS | `t11-native-proxy with spaces-f7aedcd72e4a422aab57e10a61ffdfa8/evidence.json` |
| Frontend, lint, production build | 257 PASS; exit 0 | `t12-ui-final-a161ae2441ef43ccafb6b69d1d209680/` |
| Public packager | 93 PASS | тот же каталог, `pack-tests.txt` |
| Caddy adapt с окончательным public package | exit 0 | `t12-final-public-adapt-7018d6e5e5b94ca9ae4a96e849093a58/evidence.json` |

Synthetic fixtures используют новые SQLite, файлы, порты и только собственные дочерние процессы. Caddy-проверка не слушала настоящий 443 и не проверяла ACME/TLS внешнего адреса. Реальный Scheduler, рабочие credentials/данные/аудио, MAX/Polza вызовы и Git mutations не выполнялись.

Точная заключительная команда PowerShell:

```powershell
$taskTests = @(Get-Content -LiteralPath '.runtime\team-rollout\t12-accepted-selectors.json' -Raw | ConvertFrom-Json)
& '.\.venv\Scripts\python.exe' -B scripts/run_offline_tests.py @taskTests -q --tb=short
```

Все четыре плановых backup acceptance tests включены в этот прогон. Независимые lifecycle/runtime/backup reviews закрыты в своих проверенных областях. Исправлены фактически воспроизведённые ошибки ownership/Scheduler, отмены HTTP/worker/executor/capture, регистрации source binding/containment, порядка recovery перед миграцией, смены run перед backup barrier и перезапуска с завершённой историей. Единственное предупреждение общего прогона — прежнее upstream Starlette/httpx deprecation; зависимости ради него не менялись. `git diff --check` завершился с exit 0.

Отдельная запись 52 recovery tests создана после вызова из его captured tool result и содержит точную команду, session/chunk IDs и exit code; это не исходный tee-log. Все эти тесты также включены в сохранённый полный stdout заключительного прогона `t12-accepted-final-v3-backend.txt`.

Native Job tests подтверждают остановку собственных synthetic writer children при смерти родителя. Они не запускают весь рабочий app factory с credentials. Supervisor tests используют synthetic child components; Caddy HTTP checks отдельно используют настоящий закреплённый Caddy. Эти границы учитываются при следующей ручной приёмке.

## Инструкции и следующие gates

1. [TEAM_RUNTIME.md](TEAM_RUNTIME.md): подготовить explicit runtime config и проверить `doctor`/health по компонентам.
2. [TEAM_WINDOWS_RUNBOOK.md](TEAM_WINDOWS_RUNBOOK.md): просмотреть manifest и XML; применение заданий, реальные учётные записи и окно restart/logout/reboot остаются отдельным шагом владельца.
3. [TEAM_BACKUP_RESTORE.md](TEAM_BACKUP_RESTORE.md): задать точные четыре DB/asset roots и current participant descriptors; проверить копию/restore на отдельном каталоге и доступ DPAPI исходного пользователя.
4. [TEAM_NETWORK_OPTIONS.md](TEAM_NETWORK_OPTIONS.md) и [TEAM_MAX_SETUP.md](TEAM_MAX_SETUP.md): выяснить HTTPS/WAN/CGNAT, зарегистрировать/настроить собственный MAX bot и проверить реальные callback, CDN и доверенную подписку.
5. Закрыть незавершённую browser-приёмку T9; выполнить offline E2E/load/full tests T13, затем пилот трёх партнёров, ограниченную live Polza проверку и 24-часовую приёмку.

До этих проверок нельзя заявлять `PRODUCTION_READY`. Пройденные mocks, HTTP fixtures и native child tests не заменяют ручное испытание телефона, микрофона и доставки уведомлений.

## Дополнение 2026-10-04: restore guard и read-only reconciliation preview

Исторический package result этой страницы не изменяется. Новая обязательная operational recovery карточка T12R реализует R0/R1: ordinary Secretary/effective Billing startup guard, lower diagnostic SQLite RO без migrations/recovery, explicit four-source/assets preview без apply и final changed-input checks. Проверка завершённого архива не создаёт WAL/SHM. Копия остаётся заблокированной, Billing holds/receipts сохраняются.

Корневая приёмка: 108 affected PASS до fixture URI repair, затем 174 final affected PASS, 108,35 с, exit 0. Первый новый full: 10 FAIL / 3532 PASS вследствие двух test-only Windows SQLite URI decoders; сохранён, parser исправлен с root containment. Конечный full 3554 PASS, 619,83 с, exit 0; 33 выбранных source hashes без drift. Frozen artifact helper 52 checks / 23 files / 0 hits / exit 0, frontend263/build snapshot сохранён и bundle hashes rechecked. Актуальный итог и exact evidence: [TEAM_RESTORE_VALIDATION.md](TEAM_RESTORE_VALIDATION.md), общий отчёт [TEAM_VALIDATION.md](TEAM_VALIDATION.md).

Контракт [team-restore-reconciliation.md](contracts/team-restore-reconciliation.md) отдельно фиксирует R2/R3 COMPLETE_OFFLINE ([TEAM_R3_VALIDATION.md](TEAM_R3_VALIDATION.md)) и обязательные незавершённые R4–R6. Они нужны для активации только нового подтверждённого work с immutable legacy quarantine и сохранением liabilities/guards. Current schema9 означает точный migration set `{1..9}`, а не поддержку копии, остановленной на schema7/8. Actual owner/provider reconciliation, MAX cutover, DPAPI/lifetimes и restore в 24h пилоте не квалифицированы.
