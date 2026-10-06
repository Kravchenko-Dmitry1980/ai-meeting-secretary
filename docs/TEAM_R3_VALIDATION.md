# R3: подготовка восстановленных баз

Дата: 2026-10-04. Статус проверки: **COMPLETE_OFFLINE**.
Это отдельная карточка плана T12R. Активация R4, следующий generation R5,
actual owner/provider reconciliation и ручная/24h приёмка R6 ещё не выполнены.
Внешний HTTPS пока неизвестен; его проверка перед MAX/pilot остаётся обязательной.

## Что реализовано

Явная локальная команда `scripts/team/reconcile_restore.py prepare` читает
настоящий R2 inventory, связывает четыре точных источника, получает свежее
согласие независимого локального оператора и записывает immutable preparations
Secretary/Team/Billing. Native Vikunja, исходный архив, медиа и прежние business
rows остаются сохранёнными. Последняя запись — общее `complete_prepared_blocked`
в отдельной control DB. `activation_supported=false`, `outbound_enabled=false`.

Это три отдельных SQLite commit. Общей atomic cross-file transaction нет.
Crash после локального commit либо перед общим решением сохраняет квитанции и
блокировку; повторная попытка использует реальное durable readback, тот же R3 ID
и новое явное согласие, а не доверяет переданному Plan/Receipt. Удалённая control
DB не может принять уже подготовленные источники. Complete report можно повторно
прочитать после полной проверки; это не новое разрешение на запуск.

Старые queued/failed/completed jobs и receipts не переименовываются в fake success.
Billing reserved/submitted/uncertain не освобождаются. Все исторические guards
по-прежнему равны 1. Старая Team owner row, session/code/button, новый UUID и новый
пустой control DB не подтверждают текущего оператора и не разрешают replay.

## Подтверждение оператора и остановленного runtime

Отдельная регистрация текущего Windows local operator создаёт новый независимый
anchor `.runtime/team-operator` с private current-user ACL и CurrentUser DPAPI.
Она требует явного ввода свежего challenge и digest. Approval подписывает весь
текущий commitment, имеет срок 300 секунд; nonce потребляется один раз атомарной
control transaction. Current principal, pinned directory identity, MAC и срок
повторно проверяются перед финальным решением. Anchor не входит в восстановленные
источники. Частичная регистрация не перезаписывается и не исправляется автоматически.

Это явная локальная operator policy, не доказательство бизнес-роли Team owner.
Код под той же Windows account считается доверенным; защита от скомпрометированного
компьютера или malicious same-user process не квалифицирована.

Требуются точные существующие maintenance registry и lifecycle JSON оригинального
managed deployment. Проверяется закрытый admission, отсутствие active tickets,
все четыре bindings, required participants и свежие типизированные OS proofs
завершения текущих Secretary/Gateway/Supervisor/native/proxy lifetimes. Архивный
native PID сам по себе недостаточен: backup мог затем возобновить другой процесс.
TTL/Boolean/PID-only или создание нового пустого registry не принимаются.
Команда не останавливает процессы, не обнуляет tickets и не читает owner.env/config.

## SQLite и файловая согласованность

Три authorities получают реальные `BEGIN EXCLUSIVE` writer locks. При WAL это
исключает других writers, а не readers. Перед записью business schema/logical
snapshot и скрытые row IDs повторно сравниваются с первоначальной копией.
Statementwise deadline и SQLite progress handler ограничивают SQL; весь hashing,
файловый inventory и ожидание человека не имеют обещанного общего SLA.

После каждой локальной записи owned connection закрывается. SQLite выполняет
штатную очистку; наличие оставшегося journal либо невозможность получить реальный
Windows write/delete-denial handle сохраняет partial hold. Журналы не удаляются
и `journal_mode` не конвертируется. Повторное чтение осуществляется на inactive
immutable RO под удерживаемым OS lease; фиксируется текущий main-file hash.
Native использует такой lease и inactive immutable RO сразу, в том числе при WAL
header, и не получает SQL DML/DDL или изменение байтов базы.

Archive members, restore metadata и retained assets удерживаются реальными
write/delete-denial handles, а их каталоги — no-delete handles до самого decision.
Исходный retained snapshot связывается с actual R1/R2 inventory и сравнивается
повторно. Временные SQLite sidecars исключены только для четырёх точных DB paths;
они отдельно проверяются перед durable readback. Неизвестный writer/reader,
оставивший sidecars, означает отказ; liveness не обещается ценой удаления журнала.

Размеры R2 сохраняются; дополнительный persisted R3 inventory ограничен 128 MiB,
retained file inventory — 100000 файлов. Это границы приёма, не измерение peak RAM
или квалифицированная производительность больших owner данных.

## Проверки и доказательства

Final affected: **464 PASS, 153,28 с, exit 0**, одно прежнее Starlette/httpx warning.
265 selected Python/lock/config inputs до/после affected совпали.
Полный backend/audit: **3839 PASS, 683.84 с, exit 0**, одно прежнее warning.
Повторные 265 selected hashes до/после полного прогона также совпали.
Терминальные результаты прочитаны; logs: affected-final.txt / backend-final.txt.
Содержимое owner stores, env, audio и credentials не использовалось.
Основной evidence: `.runtime/team-rollout/r3-preparation-20261004/`.
Начальный RED: 18 missing-module errors; CLI RED: 8 failures.
Integration-first 13 FAIL / 15 PASS выявил неверный запрет штатных WAL headers;
integration-second выявил чтение временных SQL sidecars как assets.
Integration-third: 28 PASS, 32,46 с (до финальных регрессий).

Независимый bounded review нашёл одно окно retained-input custody: изменение после
collector могло стать baseline, а late write оставался возможен перед complete.
Две исходные baseline regressions и две late-write regressions воспроизведены RED.
Исправлены исходный snapshot и реальные retained leases до decision. Дополнительно
statementwise SQL deadline воспроизведён RED. Совместный repair: 5 PASS, 8,36 с.
Повторного широкого source audit не было; финальные affected/full проверяют изменения.

Независимые modules: Operator 36 PASS (0,82 с), Control 47 PASS (1,44 с),
stopped-runtime verifier 47 PASS (2,99 с). Source freeze/evidence:
`r3-operator-23c1d874/freeze.md`, `r3-control-20261004/freeze.json`,
`r3-runtime-evidence-a841ec73e79741baba07331c8d461c6a/evidence.md`.
Source-only review: `r3-bounded-review-642452d64b8d48a5aa835dfe1342303f/review.md`.
Часть snapshots review менялась во время разработки; отчёт отмечает это и не
подменяет окончательные frozen hashes/тесты.

Root integration использует настоящие backup/restore и три настоящие схемы SQL,
но независимые operator/runtime ports в этих fixtures синтетические. Отдельные
OS tests проверяют CurrentUser DPAPI/ACL/leases, live-current-process refusal и
естественное завершение owned child. Это не проверка остановки рабочего deployment,
реальных встреч/качества голоса, Polza/MAX/Vikunja accounts либо ручного пилота.
Frontend в R3 не менялся; прежние tests/typecheck/lint/build не запускались заново.

Точные результаты завершающих проверок артефактов: основной
`r3-preparation-20261004/card-hygiene.json` и неизменённый accepted helper
`t13-final-artifact-review-960fbde2c8994b839f631f6518abd232/with-r3-backend-20261004.json`.
Они проверяют только named synthetic reports/public bundles; отсутствие pattern
matches не является универсальным поиском секретов и не заменяет live приёмку.

## Подготовленные локальные команды — на рабочих данных не выполнялись

До применения нужно выбрать текущую Windows account и отдельные private каталоги
для копии, убедиться в complete backup, штатно закрытом original admission и
завершении точных owned процессов. Не переходить к подготовке при active capture,
unknown writer, active ticket или отсутствующем lifecycle/maintenance proof.
Существующие shared каталоги команда не ремонтирует; нельзя удалять guards/journals.

В PowerShell, из `D:\AI\Projects\Active\Secretary`:

```powershell
& '.venv\Scripts\python.exe' -B scripts/team/reconcile_restore.py enroll-operator
```

Оператор читает показанный commitment и вводит свежий challenge вместе с digest
в локальном терминале. Это действие нельзя автоматически подтверждать за человека.
Для просмотренных абсолютных путей в private копии:

```powershell
& '.venv\Scripts\python.exe' -B scripts/team/reconcile_restore.py prepare `
  --backup $R3BackupDirectory --restore $R3RestoredDirectory `
  --maintenance $R3OriginalMaintenanceDatabase --lifecycle $R3OriginalLifecycleJson
```

Четыре `$R3...` переменные нужно явно заполнить абсолютными выбранными путями.
Нет `--activate`, `--apply`, root/operator override, env fallback или secret arguments.
Если команда вернула blocked, сохранять причины/receipts; не повторять внешние POST.
Неполные operator/control artifacts не удалять ради PASS: отдельная проверка причины
нужна до нового explicit setup. Нормальный запуск restored приложения пока запрещён.

Polza обязателен; общий лимит 3000 ₽/месяц сохраняется. Здесь не было paid calls,
проверки баланса/receipt у провайдера или освобождения unknown reservations. MAX
raw ingress/subscription cutover остаётся OFF. После R3 следующая карточка R4
вводит единый activation predicate; owner/provider/HTTPS/phone/24h gates остаются
открытыми. Общий goal не завершён, Team NOT_READY_FOR_MANUAL_TEST.
